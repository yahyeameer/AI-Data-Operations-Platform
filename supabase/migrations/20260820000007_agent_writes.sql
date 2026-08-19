-- =============================================================================
-- The agent's write path.
--
-- The worker holds the service key and could write these tables directly. It
-- does not, for the reason established in Week 1: an entity and its audit row
-- belong in one transaction. A parse that produced a dataset version but
-- crashed before logging it would leave a version nobody can account for, and
-- section 13 asks for an immutable trail, not a mostly-complete one.
--
-- These are granted to service_role only. Nothing here is callable by a
-- signed-in user, because everything here is a consequence of a job the user
-- already asked for through enqueue_agent_job.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Job chaining.
--
-- A parse leads to a profile leads to proposals. The worker enqueues each
-- follow-on step rather than doing all three inside one job, so that a failure
-- in profiling does not discard a parse that took four minutes, and so the
-- dashboard can show the pipeline advancing instead of one opaque long job.
--
-- Separate from enqueue_agent_job because that one requires auth.uid() and the
-- worker has no session. p_requested_by carries the original human through the
-- chain, so the audit trail still names the person whose click caused all of
-- it rather than attributing half the pipeline to a machine.
-- -----------------------------------------------------------------------------

create or replace function enqueue_agent_job_internal(
  p_workspace_id       uuid,
  p_kind               agent_job_kind,
  p_payload            jsonb default '{}'::jsonb,
  p_dataset_id         uuid default null,
  p_dataset_version_id uuid default null,
  p_raw_upload_id      uuid default null,
  p_requested_by       uuid default null,
  p_priority           smallint default 100
)
returns agent_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_org uuid;
  v_job agent_jobs;
begin
  select org_id into v_org from workspaces where id = p_workspace_id;
  if v_org is null then
    raise exception 'workspace % not found', p_workspace_id;
  end if;

  insert into agent_jobs (
    org_id, workspace_id, dataset_id, dataset_version_id, raw_upload_id,
    kind, payload, priority, requested_by
  )
  values (
    v_org, p_workspace_id, p_dataset_id, p_dataset_version_id, p_raw_upload_id,
    p_kind, coalesce(p_payload, '{}'::jsonb), coalesce(p_priority, 100::smallint), p_requested_by
  )
  returning * into v_job;

  perform write_audit(
    v_org, p_workspace_id, 'agent.job.enqueued', 'agent_job', v_job.id::text,
    jsonb_build_object('kind', p_kind, 'chained', true)
  );

  return v_job;
end;
$fn$;

revoke all on function enqueue_agent_job_internal(uuid, agent_job_kind, jsonb, uuid, uuid, uuid, uuid, smallint)
  from public, anon, authenticated;
grant execute on function enqueue_agent_job_internal(uuid, agent_job_kind, jsonb, uuid, uuid, uuid, uuid, smallint)
  to service_role;

-- -----------------------------------------------------------------------------
-- Dataset versions.
--
-- The version number is allocated here, inside the transaction, rather than
-- read-then-written by the worker. Two jobs finishing at once would otherwise
-- both read max(version_no) = 3 and both try to write 4; the unique constraint
-- would catch it, but as a failed job rather than a correct one.
--
-- `p_parent_version_id` defaults to the dataset's current highest version, so
-- the lineage chain is continuous by construction.
-- -----------------------------------------------------------------------------

create or replace function record_dataset_version(
  p_dataset_id        uuid,
  p_kind              dataset_version_kind,
  p_parquet_path      text default null,
  p_row_count         bigint default null,
  p_column_hash       text default null,
  p_raw_upload_id     uuid default null,
  p_parent_version_id uuid default null,
  p_produced_by_job   uuid default null,
  p_created_by        uuid default null,
  p_metadata          jsonb default '{}'::jsonb
)
returns dataset_versions
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_workspace uuid;
  v_org       uuid;
  v_next      integer;
  v_parent    uuid := p_parent_version_id;
  v_version   dataset_versions;
begin
  select d.workspace_id, w.org_id
    into v_workspace, v_org
  from datasets d
  join workspaces w on w.id = d.workspace_id
  where d.id = p_dataset_id;

  if v_workspace is null then
    raise exception 'dataset % not found', p_dataset_id;
  end if;

  -- Serialise version allocation for this dataset. Contending writers wait
  -- rather than collide; the lock is released with the transaction.
  perform pg_advisory_xact_lock(hashtextextended(p_dataset_id::text, 0));

  select coalesce(max(version_no) + 1, 0) into v_next
  from dataset_versions where dataset_id = p_dataset_id;

  if v_next > 0 and v_parent is null then
    select id into v_parent
    from dataset_versions
    where dataset_id = p_dataset_id
    order by version_no desc
    limit 1;
  end if;

  insert into dataset_versions (
    dataset_id, parent_version_id, version_no, kind, raw_upload_id,
    parquet_path, row_count, column_hash, produced_by_run_id, created_by
  )
  values (
    p_dataset_id, v_parent, v_next, p_kind, p_raw_upload_id,
    p_parquet_path, p_row_count, p_column_hash, p_produced_by_job, p_created_by
  )
  returning * into v_version;

  perform write_audit(
    v_org, v_workspace, 'dataset.version.created', 'dataset_version', v_version.id::text,
    coalesce(p_metadata, '{}'::jsonb)
      || jsonb_build_object('version_no', v_next, 'kind', p_kind, 'row_count', p_row_count)
  );

  return v_version;
end;
$fn$;

revoke all on function record_dataset_version(uuid, dataset_version_kind, text, bigint, text, uuid, uuid, uuid, uuid, jsonb)
  from public, anon, authenticated;
grant execute on function record_dataset_version(uuid, dataset_version_kind, text, bigint, text, uuid, uuid, uuid, uuid, jsonb)
  to service_role;

-- -----------------------------------------------------------------------------
-- Profiles. One per version, and the version is immutable, so a re-profile of
-- the same version is a no-op returning what is already there rather than an
-- error the worker has to special-case.
-- -----------------------------------------------------------------------------

create or replace function record_dataset_profile(
  p_dataset_version_id uuid,
  p_row_count          bigint,
  p_column_count       integer,
  p_columns            jsonb,
  p_signals            jsonb,
  p_job_id             uuid default null
)
returns dataset_profiles
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_profile dataset_profiles;
begin
  select * into v_profile from dataset_profiles where dataset_version_id = p_dataset_version_id;
  if found then
    return v_profile;
  end if;

  insert into dataset_profiles (
    dataset_version_id, row_count, column_count, columns, signals, produced_by_job_id
  )
  values (
    p_dataset_version_id, p_row_count, p_column_count,
    coalesce(p_columns, '[]'::jsonb), coalesce(p_signals, '{}'::jsonb), p_job_id
  )
  returning * into v_profile;

  return v_profile;
end;
$fn$;

revoke all on function record_dataset_profile(uuid, bigint, integer, jsonb, jsonb, uuid)
  from public, anon, authenticated;
grant execute on function record_dataset_profile(uuid, bigint, integer, jsonb, jsonb, uuid) to service_role;

-- -----------------------------------------------------------------------------
-- Proposals.
--
-- Re-proposing supersedes what is still pending rather than adding to it. Two
-- overlapping sets of proposals for one version would let the same change be
-- approved twice, and the second approval would apply to a dataset the first
-- had already altered.
--
-- Decisions already made are untouched -- the guard on the table would refuse
-- anyway, which is the point of putting it there.
-- -----------------------------------------------------------------------------

create or replace function replace_proposed_changes(
  p_dataset_version_id uuid,
  p_job_id             uuid,
  p_proposals          jsonb
)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_workspace uuid;
  v_org       uuid;
  v_count     integer;
begin
  select d.workspace_id, w.org_id
    into v_workspace, v_org
  from dataset_versions dv
  join datasets d on d.id = dv.dataset_id
  join workspaces w on w.id = d.workspace_id
  where dv.id = p_dataset_version_id;

  if v_workspace is null then
    raise exception 'dataset version % not found', p_dataset_version_id;
  end if;

  -- decided_at is set because the check constraint requires a non-pending row
  -- to carry one; decided_by stays null, which is how a superseded proposal is
  -- distinguished from one a person actually ruled on.
  update proposed_changes
     set status = 'superseded', decided_at = now(),
         decision_note = 'superseded by a newer analysis'
   where dataset_version_id = p_dataset_version_id
     and status = 'pending';

  insert into proposed_changes (
    workspace_id, dataset_version_id, job_id, group_key, step_type, column_name,
    title, rationale, operation, evidence, confidence, affected_rows, materiality_gbp
  )
  select
    v_workspace,
    p_dataset_version_id,
    p_job_id,
    item ->> 'group_key',
    item ->> 'step_type',
    nullif(item ->> 'column_name', ''),
    item ->> 'title',
    item ->> 'rationale',
    coalesce(item -> 'operation', '{}'::jsonb),
    coalesce(item -> 'evidence', '{}'::jsonb),
    (item ->> 'confidence')::change_confidence,
    coalesce((item ->> 'affected_rows')::bigint, 0),
    nullif(item ->> 'materiality_gbp', '')::numeric
  from jsonb_array_elements(coalesce(p_proposals, '[]'::jsonb)) as item;

  get diagnostics v_count = row_count;

  perform write_audit(
    v_org, v_workspace, 'agent.changes.proposed', 'dataset_version', p_dataset_version_id::text,
    jsonb_build_object('count', v_count, 'job_id', p_job_id)
  );

  return v_count;
end;
$fn$;

revoke all on function replace_proposed_changes(uuid, uuid, jsonb) from public, anon, authenticated;
grant execute on function replace_proposed_changes(uuid, uuid, jsonb) to service_role;

-- -----------------------------------------------------------------------------
-- Marking approvals as applied, once the run that used them has written its
-- output version. Separate from the apply itself so a crash between the two
-- leaves the proposals approved-but-not-applied -- recoverable -- rather than
-- applied against a version that was never written.
-- -----------------------------------------------------------------------------

create or replace function mark_changes_applied(
  p_dataset_version_id uuid,
  p_group_keys         text[]
)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_count integer;
begin
  update proposed_changes
     set status = 'applied'
   where dataset_version_id = p_dataset_version_id
     and group_key = any (p_group_keys)
     and status = 'approved';

  get diagnostics v_count = row_count;
  return v_count;
end;
$fn$;

revoke all on function mark_changes_applied(uuid, text[]) from public, anon, authenticated;
grant execute on function mark_changes_applied(uuid, text[]) to service_role;

-- -----------------------------------------------------------------------------
-- The recipe-matching fingerprint (PRD section 3). Written by the parser once
-- it knows the file's shape; read next month to decide whether this upload is
-- the same recurring report.
-- -----------------------------------------------------------------------------

create or replace function set_dataset_signature(
  p_dataset_id uuid,
  p_signature  text
)
returns datasets
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_dataset datasets;
begin
  update datasets set source_signature = p_signature
   where id = p_dataset_id
  returning * into v_dataset;

  if not found then
    raise exception 'dataset % not found', p_dataset_id;
  end if;

  return v_dataset;
end;
$fn$;

revoke all on function set_dataset_signature(uuid, text) from public, anon, authenticated;
grant execute on function set_dataset_signature(uuid, text) to service_role;

-- -----------------------------------------------------------------------------
-- Analysis runs. Recorded through a function so the executed SQL and the audit
-- entry cannot come apart -- section 7's drill-down is only as good as the
-- guarantee that the SQL was stored at the moment it ran.
-- -----------------------------------------------------------------------------

create or replace function record_analysis_run(
  p_dataset_version_id uuid,
  p_question           text,
  p_executed_sql       text,
  p_result             jsonb,
  p_row_refs           jsonb default '[]'::jsonb,
  p_model_used         text default null,
  p_duration_ms        integer default null,
  p_job_id             uuid default null,
  p_created_by         uuid default null
)
returns analysis_runs
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_workspace uuid;
  v_org       uuid;
  v_run       analysis_runs;
begin
  select d.workspace_id, w.org_id
    into v_workspace, v_org
  from dataset_versions dv
  join datasets d on d.id = dv.dataset_id
  join workspaces w on w.id = d.workspace_id
  where dv.id = p_dataset_version_id;

  if v_workspace is null then
    raise exception 'dataset version % not found', p_dataset_version_id;
  end if;

  insert into analysis_runs (
    workspace_id, dataset_version_id, job_id, question, executed_sql,
    result, row_refs, model_used, duration_ms, created_by
  )
  values (
    v_workspace, p_dataset_version_id, p_job_id, p_question, p_executed_sql,
    coalesce(p_result, '{}'::jsonb), coalesce(p_row_refs, '[]'::jsonb),
    p_model_used, p_duration_ms, p_created_by
  )
  returning * into v_run;

  perform write_audit(
    v_org, v_workspace, 'agent.analysis.ran', 'analysis_run', v_run.id::text,
    jsonb_build_object('question', p_question, 'model', p_model_used, 'duration_ms', p_duration_ms)
  );

  return v_run;
end;
$fn$;

revoke all on function record_analysis_run(uuid, text, text, jsonb, jsonb, text, integer, uuid, uuid)
  from public, anon, authenticated;
grant execute on function record_analysis_run(uuid, text, text, jsonb, jsonb, text, integer, uuid, uuid)
  to service_role;
