-- =============================================================================
-- The queue protocol.
--
-- Five functions define the whole conversation between the dashboard and the
-- agent host. Everything else either of them does is a plain SELECT.
--
--   dashboard : enqueue_agent_job, decide_proposed_change
--   worker    : agent_worker_heartbeat, claim_agent_job, heartbeat_agent_job,
--               finish_agent_job
--
-- The split of privileges is the point. `enqueue_agent_job` is granted to
-- authenticated and checks membership itself, so a signed-in accountant can
-- ask for work on their own workspaces and nothing else. Every worker-side
-- function is granted to service_role alone -- the agent holds the secret key,
-- so it could write these tables directly, and routing it through functions is
-- what keeps the state machine in one reviewable place rather than spread
-- across whatever the worker happened to do.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Enqueue.
--
-- Membership is re-checked here rather than trusted from the caller, for the
-- same reason create_workspace re-checks it: SECURITY DEFINER steps outside
-- RLS, so a function that assumed its caller was authorised would be a hole
-- straight through the tenant boundary.
--
-- Deduplication is deliberate. An accountant who clicks "Analyse" three times
-- while nothing visibly happens should get one parse, not three. Returning the
-- existing job makes the second click a no-op that still navigates to the right
-- place.
-- -----------------------------------------------------------------------------

create or replace function enqueue_agent_job(
  p_workspace_id       uuid,
  p_kind               agent_job_kind,
  p_payload            jsonb default '{}'::jsonb,
  p_dataset_id         uuid default null,
  p_dataset_version_id uuid default null,
  p_raw_upload_id      uuid default null,
  p_priority           smallint default 100
)
returns agent_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_user uuid := auth.uid();
  v_org  uuid;
  v_job  agent_jobs;
begin
  if v_user is null then
    raise exception 'not authenticated' using errcode = 'insufficient_privilege';
  end if;

  if not has_workspace_access(p_workspace_id) then
    -- Same wording whether the workspace is absent or someone else's. The API
    -- must not confirm that another tenant's id is real.
    raise exception 'workspace % not found', p_workspace_id using errcode = 'insufficient_privilege';
  end if;

  select org_id into v_org from workspaces where id = p_workspace_id;

  -- Referenced entities must live in the same workspace. Without this a caller
  -- with access to workspace A could aim a job at workspace B's dataset, and
  -- the worker -- holding the service key -- would happily comply.
  if p_dataset_id is not null
     and not exists (select 1 from datasets d
                     where d.id = p_dataset_id and d.workspace_id = p_workspace_id) then
    raise exception 'dataset % is not in workspace %', p_dataset_id, p_workspace_id
      using errcode = 'insufficient_privilege';
  end if;

  if p_raw_upload_id is not null
     and not exists (select 1 from raw_uploads u
                     where u.id = p_raw_upload_id and u.workspace_id = p_workspace_id) then
    raise exception 'upload % is not in workspace %', p_raw_upload_id, p_workspace_id
      using errcode = 'insufficient_privilege';
  end if;

  if p_dataset_version_id is not null
     and not exists (select 1 from dataset_versions dv
                     join datasets d on d.id = dv.dataset_id
                     where dv.id = p_dataset_version_id and d.workspace_id = p_workspace_id) then
    raise exception 'dataset version % is not in workspace %', p_dataset_version_id, p_workspace_id
      using errcode = 'insufficient_privilege';
  end if;

  -- An identical job already waiting or running is returned as-is.
  select * into v_job
  from agent_jobs j
  where j.workspace_id = p_workspace_id
    and j.kind = p_kind
    and j.status in ('queued', 'running')
    and j.dataset_version_id is not distinct from p_dataset_version_id
    and j.raw_upload_id is not distinct from p_raw_upload_id
    and j.payload = coalesce(p_payload, '{}'::jsonb)
  order by j.created_at
  limit 1;

  if found then
    return v_job;
  end if;

  insert into agent_jobs (
    org_id, workspace_id, dataset_id, dataset_version_id, raw_upload_id,
    kind, payload, priority, requested_by
  )
  values (
    v_org, p_workspace_id, p_dataset_id, p_dataset_version_id, p_raw_upload_id,
    p_kind, coalesce(p_payload, '{}'::jsonb), coalesce(p_priority, 100::smallint), v_user
  )
  returning * into v_job;

  perform write_audit(
    v_org, p_workspace_id, 'agent.job.enqueued', 'agent_job', v_job.id::text,
    jsonb_build_object('kind', p_kind, 'dataset_version_id', p_dataset_version_id,
                       'raw_upload_id', p_raw_upload_id)
  );

  return v_job;
end;
$fn$;

revoke all on function enqueue_agent_job(uuid, agent_job_kind, jsonb, uuid, uuid, uuid, smallint)
  from public, anon;
grant execute on function enqueue_agent_job(uuid, agent_job_kind, jsonb, uuid, uuid, uuid, smallint)
  to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- Worker registration / liveness.
-- -----------------------------------------------------------------------------

create or replace function agent_worker_heartbeat(
  p_worker_id    text,
  p_hostname     text default null,
  p_version      text default null,
  p_capabilities agent_job_kind[] default '{}',
  p_metadata     jsonb default '{}'::jsonb
)
returns agent_workers
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_worker agent_workers;
begin
  insert into agent_workers (id, hostname, version, capabilities, metadata)
  values (p_worker_id, p_hostname, p_version, coalesce(p_capabilities, '{}'),
          coalesce(p_metadata, '{}'::jsonb))
  on conflict (id) do update
    set hostname     = coalesce(excluded.hostname, agent_workers.hostname),
        version      = coalesce(excluded.version, agent_workers.version),
        capabilities = excluded.capabilities,
        metadata     = excluded.metadata,
        last_seen_at = now()
  returning * into v_worker;

  return v_worker;
end;
$fn$;

revoke all on function agent_worker_heartbeat(text, text, text, agent_job_kind[], jsonb)
  from public, anon, authenticated;
grant execute on function agent_worker_heartbeat(text, text, text, agent_job_kind[], jsonb)
  to service_role;

-- -----------------------------------------------------------------------------
-- Claim.
--
-- `for update skip locked` is what makes running two workers safe: concurrent
-- claims step over each other's locked rows instead of blocking or, worse, both
-- returning the same job.
--
-- The candidate set includes running jobs whose lease has expired. That single
-- `or` is the entire crash-recovery story: kill the VPS mid-parse and the job
-- becomes claimable again once the lease runs out, with attempts already
-- incremented so a job that reliably kills its worker eventually stops being
-- retried rather than looping forever.
-- -----------------------------------------------------------------------------

-- Returns a set rather than a single record, so "nothing to do" is an empty
-- result. A plain `returns agent_jobs` would be the obvious choice, but a NULL
-- composite reaches PostgREST as an object with every field set to null --
-- indistinguishable at a glance from a real job, and a caller that forgets to
-- check `id` would claim a job that does not exist. An empty array cannot be
-- misread.
create or replace function claim_agent_job(
  p_worker_id      text,
  p_kinds          agent_job_kind[] default null,
  p_lease_seconds  integer default 300
)
returns setof agent_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_job agent_jobs;
begin
  with candidate as (
    select j.id
    from agent_jobs j
    where (
            j.status = 'queued'
            or (j.status = 'running' and j.lease_expires_at < now())
          )
      and j.attempts < j.max_attempts
      and (p_kinds is null or j.kind = any (p_kinds))
    order by j.priority, j.created_at
    limit 1
    for update skip locked
  )
  update agent_jobs j
     set status           = 'running',
         claimed_by       = p_worker_id,
         claimed_at       = now(),
         lease_expires_at = now() + make_interval(secs => greatest(p_lease_seconds, 30)),
         attempts         = j.attempts + 1,
         started_at       = coalesce(j.started_at, now()),
         error            = null
    from candidate c
   where j.id = c.id
  returning j.* into v_job;

  if not found then
    return;
  end if;

  update agent_workers
     set jobs_claimed = jobs_claimed + 1,
         last_seen_at = now()
   where id = p_worker_id;

  return next v_job;
end;
$fn$;

revoke all on function claim_agent_job(text, agent_job_kind[], integer)
  from public, anon, authenticated;
grant execute on function claim_agent_job(text, agent_job_kind[], integer) to service_role;

-- -----------------------------------------------------------------------------
-- Lease renewal.
--
-- A long parse extends its own lease as it goes and reports progress in the
-- same call, so the dashboard's progress bar and the queue's liveness signal
-- can never disagree -- they are the same write.
--
-- The claimed_by guard matters: a worker whose lease already expired and was
-- stolen must not be able to renew it and start writing results for a job
-- somebody else now owns.
-- -----------------------------------------------------------------------------

create or replace function heartbeat_agent_job(
  p_job_id        uuid,
  p_worker_id     text,
  p_progress      jsonb default null,
  p_lease_seconds integer default 300
)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_updated integer;
begin
  update agent_jobs
     set lease_expires_at = now() + make_interval(secs => greatest(p_lease_seconds, 30)),
         progress         = coalesce(p_progress, progress)
   where id = p_job_id
     and claimed_by = p_worker_id
     and status = 'running';

  get diagnostics v_updated = row_count;

  update agent_workers set last_seen_at = now() where id = p_worker_id;

  return v_updated = 1;
end;
$fn$;

revoke all on function heartbeat_agent_job(uuid, text, jsonb, integer)
  from public, anon, authenticated;
grant execute on function heartbeat_agent_job(uuid, text, jsonb, integer) to service_role;

-- -----------------------------------------------------------------------------
-- Finish.
--
-- One function for both outcomes, because the interesting logic is the decision
-- between "failed for good" and "failed, try again", and that belongs in one
-- place. A worker reporting failure with attempts remaining puts the job back
-- in the queue; the audit row still records the attempt, so a job that
-- succeeded on its third try leaves evidence of the first two.
--
-- p_retryable is how the worker says "do not bother". Most failures are worth
-- another attempt -- a dropped connection, a storage timeout. Some are not:
-- "this file is a legacy .xls" and "the blocking issue is unresolved" will
-- fail identically three times, and retrying them only delays the message the
-- accountant needs to read and makes the job look flaky when it is simply
-- answering.
-- -----------------------------------------------------------------------------

create or replace function finish_agent_job(
  p_job_id    uuid,
  p_worker_id text,
  p_success   boolean,
  p_result    jsonb default null,
  p_error     text default null,
  p_retryable boolean default true
)
returns agent_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_job    agent_jobs;
  v_retry  boolean;
begin
  select * into v_job from agent_jobs where id = p_job_id for update;

  if not found then
    raise exception 'job % not found', p_job_id;
  end if;

  if v_job.claimed_by is distinct from p_worker_id then
    raise exception 'job % is claimed by %, not %', p_job_id, v_job.claimed_by, p_worker_id
      using errcode = 'insufficient_privilege';
  end if;

  if v_job.status <> 'running' then
    -- Already terminal. A duplicate completion is a retry of the report, not an
    -- error; return the row unchanged rather than corrupting a finished job.
    return v_job;
  end if;

  v_retry := (not p_success) and coalesce(p_retryable, true)
             and v_job.attempts < v_job.max_attempts;

  update agent_jobs
     set status           = case
                              when p_success then 'succeeded'::agent_job_status
                              when v_retry   then 'queued'::agent_job_status
                              else 'failed'::agent_job_status
                            end,
         result           = coalesce(p_result, result),
         error            = case when p_success then null else p_error end,
         finished_at      = case when p_success or not v_retry then now() else null end,
         claimed_by       = case when v_retry then null else claimed_by end,
         lease_expires_at = null
   where id = p_job_id
  returning * into v_job;

  perform write_audit(
    v_job.org_id, v_job.workspace_id,
    case when p_success then 'agent.job.succeeded'
         when v_retry   then 'agent.job.retrying'
         else                'agent.job.failed' end,
    'agent_job', v_job.id::text,
    jsonb_build_object('kind', v_job.kind, 'worker', p_worker_id,
                       'attempt', v_job.attempts, 'error', p_error)
  );

  return v_job;
end;
$fn$;

revoke all on function finish_agent_job(uuid, text, boolean, jsonb, text, boolean)
  from public, anon, authenticated;
grant execute on function finish_agent_job(uuid, text, boolean, jsonb, text, boolean) to service_role;

-- -----------------------------------------------------------------------------
-- Approval.
--
-- Section 5's whole premise is that a human decides. This is where that
-- decision is recorded, and it is the one write in the agent path that a user
-- performs directly rather than by asking the worker to do it.
--
-- Approving a group by key rather than a single id is not a convenience: the
-- PRD asks for grouped, materiality-ranked batches, and an interface that made
-- 400 identical decisions individually would not be used twice.
-- -----------------------------------------------------------------------------

create or replace function decide_proposed_changes(
  p_dataset_version_id uuid,
  p_group_keys         text[],
  p_approve            boolean,
  p_note               text default null
)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_user      uuid := auth.uid();
  v_workspace uuid;
  v_org       uuid;
  v_count     integer;
begin
  if v_user is null then
    raise exception 'not authenticated' using errcode = 'insufficient_privilege';
  end if;

  select d.workspace_id, w.org_id
    into v_workspace, v_org
  from dataset_versions dv
  join datasets d on d.id = dv.dataset_id
  join workspaces w on w.id = d.workspace_id
  where dv.id = p_dataset_version_id;

  if v_workspace is null or not has_workspace_access(v_workspace) then
    raise exception 'dataset version % not found', p_dataset_version_id
      using errcode = 'insufficient_privilege';
  end if;

  update proposed_changes
     set status        = case when p_approve then 'approved'::proposed_change_status
                                             else 'rejected'::proposed_change_status end,
         decided_by    = v_user,
         decided_at    = now(),
         decision_note = p_note
   where dataset_version_id = p_dataset_version_id
     and group_key = any (p_group_keys)
     and status = 'pending';

  get diagnostics v_count = row_count;

  perform write_audit(
    v_org, v_workspace,
    case when p_approve then 'agent.changes.approved' else 'agent.changes.rejected' end,
    'dataset_version', p_dataset_version_id::text,
    jsonb_build_object('group_keys', to_jsonb(p_group_keys), 'count', v_count, 'note', p_note)
  );

  return v_count;
end;
$fn$;

revoke all on function decide_proposed_changes(uuid, text[], boolean, text) from public, anon;
grant execute on function decide_proposed_changes(uuid, text[], boolean, text)
  to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- Cancel.
--
-- A queued job can be withdrawn. A running one cannot be killed from here --
-- the worker owns that process -- so this refuses rather than pretending, and
-- the lease is what eventually cleans up a worker that has genuinely gone away.
-- -----------------------------------------------------------------------------

create or replace function cancel_agent_job(p_job_id uuid)
returns agent_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_job agent_jobs;
begin
  select * into v_job from agent_jobs where id = p_job_id for update;

  if not found or not has_workspace_access(v_job.workspace_id) then
    raise exception 'job % not found', p_job_id using errcode = 'insufficient_privilege';
  end if;

  if v_job.status <> 'queued' then
    raise exception 'job % is %, only queued jobs can be cancelled', p_job_id, v_job.status
      using errcode = 'restrict_violation';
  end if;

  update agent_jobs
     set status = 'cancelled', finished_at = now(), lease_expires_at = null
   where id = p_job_id
  returning * into v_job;

  perform write_audit(
    v_job.org_id, v_job.workspace_id, 'agent.job.cancelled', 'agent_job', v_job.id::text,
    jsonb_build_object('kind', v_job.kind)
  );

  return v_job;
end;
$fn$;

revoke all on function cancel_agent_job(uuid) from public, anon;
grant execute on function cancel_agent_job(uuid) to authenticated, service_role;
