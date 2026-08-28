-- =============================================================================
-- The recipe write path.
--
-- Same split as everywhere else: the worker's functions are service_role only,
-- the accountant's are granted to authenticated and re-check membership
-- themselves.
--
-- The one function here that is the actual product is `resolve_deviation`. MVP
-- criterion 9 is that a human resolution of an ambiguous match writes back to
-- the mapping table and does not recur next month, and that write-back is what
-- takes automation from the first month's vocabulary to the client's whole
-- vocabulary. Everything else is scaffolding around it.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Mapping tables.
--
-- get-or-create, because every caller wants the same thing: the supplier
-- mapping for this workspace, whether or not one exists yet.
-- -----------------------------------------------------------------------------

create or replace function ensure_mapping_table(
  p_workspace_id uuid,
  p_name         text,
  p_kind         text default 'entity',
  p_created_by   uuid default null
)
returns mapping_tables
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_table mapping_tables;
begin
  if not exists (select 1 from workspaces where id = p_workspace_id) then
    raise exception 'workspace % not found', p_workspace_id;
  end if;

  insert into mapping_tables (workspace_id, name, kind, created_by)
  values (p_workspace_id, btrim(p_name), coalesce(p_kind, 'entity'), p_created_by)
  on conflict (workspace_id, name) do update set name = excluded.name
  returning * into v_table;

  return v_table;
end;
$fn$;

revoke all on function ensure_mapping_table(uuid, text, text, uuid) from public, anon, authenticated;
grant execute on function ensure_mapping_table(uuid, text, text, uuid) to service_role;

-- -----------------------------------------------------------------------------
-- Adding what someone decided to the vocabulary.
--
-- Existing entries are left alone rather than overwritten. A mapping a person
-- confirmed last month is better evidence than one this month's run inferred,
-- and silently replacing it would let a fuzzy guess undo a human decision.
-- -----------------------------------------------------------------------------

create or replace function upsert_mapping_entries(
  p_mapping_table_id uuid,
  p_entries          jsonb,
  p_confirmed_by     uuid default null
)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_count integer;
begin
  insert into mapping_entries (
    mapping_table_id, source_key, source_value, canonical_value, confirmed_by, confirmed_at
  )
  select
    p_mapping_table_id,
    lower(btrim(item ->> 'source_key')),
    item ->> 'source_value',
    item ->> 'canonical_value',
    p_confirmed_by,
    case when p_confirmed_by is null then null else now() end
  from jsonb_array_elements(coalesce(p_entries, '[]'::jsonb)) as item
  where nullif(btrim(item ->> 'source_key'), '') is not null
    and nullif(btrim(item ->> 'canonical_value'), '') is not null
  on conflict (mapping_table_id, source_key) do update
    -- Only fill in a confirmation that was previously absent; never change the
    -- canonical value an entry already has.
    set confirmed_by = coalesce(mapping_entries.confirmed_by, excluded.confirmed_by),
        confirmed_at = coalesce(mapping_entries.confirmed_at, excluded.confirmed_at);

  get diagnostics v_count = row_count;
  return v_count;
end;
$fn$;

revoke all on function upsert_mapping_entries(uuid, jsonb, uuid) from public, anon, authenticated;
grant execute on function upsert_mapping_entries(uuid, jsonb, uuid) to service_role;

create or replace function record_mapping_hits(p_mapping_table_id uuid, p_source_keys text[])
returns void
language sql
security definer
set search_path = public, pg_temp
as $fn$
  update mapping_entries
     set hit_count = hit_count + 1
   where mapping_table_id = p_mapping_table_id
     and source_key = any (p_source_keys);
$fn$;

revoke all on function record_mapping_hits(uuid, text[]) from public, anon, authenticated;
grant execute on function record_mapping_hits(uuid, text[]) to service_role;

-- -----------------------------------------------------------------------------
-- Capture: the approved session becomes a recipe (criterion 5).
--
-- Called at the end of apply_cleaning. Creates the recipe on the first run and
-- adds a version on later ones -- but only when the steps actually changed.
-- Without that check every month would mint an identical version, and "v14"
-- would say nothing about how much the recipe has really moved.
-- -----------------------------------------------------------------------------

create or replace function capture_recipe(
  p_workspace_id     uuid,
  p_dataset_id       uuid,
  p_source_signature text,
  p_name             text,
  p_steps            jsonb,
  p_invariants       jsonb default '[]'::jsonb,
  p_change_note      text default null,
  p_learned_from     uuid default null,
  p_created_by       uuid default null
)
returns recipe_versions
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_org      uuid;
  v_recipe   cleaning_recipes;
  v_current  recipe_versions;
  v_next     integer;
  v_version  recipe_versions;
begin
  select org_id into v_org from workspaces where id = p_workspace_id;
  if v_org is null then
    raise exception 'workspace % not found', p_workspace_id;
  end if;

  -- One recipe per signature per workspace. Two months of the same report must
  -- land on the same recipe or the whole mechanism is pointless.
  select * into v_recipe
  from cleaning_recipes
  where workspace_id = p_workspace_id
    and source_signature is not distinct from p_source_signature;

  if not found then
    insert into cleaning_recipes (workspace_id, dataset_id, name, source_signature, created_by)
    values (p_workspace_id, p_dataset_id, btrim(p_name), p_source_signature, p_created_by)
    returning * into v_recipe;

    perform write_audit(
      v_org, p_workspace_id, 'recipe.created', 'cleaning_recipe', v_recipe.id::text,
      jsonb_build_object('name', v_recipe.name, 'source_signature', p_source_signature)
    );
  end if;

  if v_recipe.current_version_id is not null then
    select * into v_current from recipe_versions where id = v_recipe.current_version_id;

    -- Identical steps mean nothing was learned; hand back what already exists.
    if found and v_current.steps = coalesce(p_steps, '[]'::jsonb) then
      return v_current;
    end if;
  end if;

  select coalesce(max(version_no) + 1, 1) into v_next
  from recipe_versions where recipe_id = v_recipe.id;

  insert into recipe_versions (
    recipe_id, version_no, steps, invariants, change_note, learned_from, created_by
  )
  values (
    v_recipe.id, v_next, coalesce(p_steps, '[]'::jsonb), coalesce(p_invariants, '[]'::jsonb),
    p_change_note, p_learned_from, p_created_by
  )
  returning * into v_version;

  update cleaning_recipes set current_version_id = v_version.id where id = v_recipe.id;

  perform write_audit(
    v_org, p_workspace_id, 'recipe.version.created', 'recipe_version', v_version.id::text,
    jsonb_build_object('recipe_id', v_recipe.id, 'version_no', v_next,
                       'steps', jsonb_array_length(coalesce(p_steps, '[]'::jsonb)),
                       'change_note', p_change_note)
  );

  return v_version;
end;
$fn$;

revoke all on function capture_recipe(uuid, uuid, text, text, jsonb, jsonb, text, uuid, uuid)
  from public, anon, authenticated;
grant execute on function capture_recipe(uuid, uuid, text, text, jsonb, jsonb, text, uuid, uuid)
  to service_role;

-- -----------------------------------------------------------------------------
-- Match: does this incoming file look like something we already know?
--
-- The lookup criterion 6 turns on. Deliberately exact on the signature rather
-- than fuzzy: a near-match that replays the wrong recipe would apply the wrong
-- transformations confidently, which is worse than asking the accountant to
-- review a file from scratch.
-- -----------------------------------------------------------------------------

create or replace function match_recipe(p_workspace_id uuid, p_source_signature text)
returns table (
  recipe_id          uuid,
  recipe_name        text,
  recipe_version_id  uuid,
  version_no         integer,
  steps              jsonb,
  invariants         jsonb,
  run_count          bigint
)
language sql
security definer
set search_path = public, pg_temp
as $fn$
  select
    r.id,
    r.name,
    v.id,
    v.version_no,
    v.steps,
    v.invariants,
    (select count(*) from recipe_runs rr where rr.recipe_version_id = v.id)
  from cleaning_recipes r
  join recipe_versions v on v.id = r.current_version_id
  where r.workspace_id = p_workspace_id
    and r.source_signature = p_source_signature
    and r.enabled
  limit 1;
$fn$;

revoke all on function match_recipe(uuid, text) from public, anon;
grant execute on function match_recipe(uuid, text) to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- Run lifecycle.
-- -----------------------------------------------------------------------------

create or replace function start_recipe_run(
  p_workspace_id      uuid,
  p_recipe_version_id uuid,
  p_dataset_version_in uuid,
  p_job_id            uuid default null
)
returns recipe_runs
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_org uuid;
  v_run recipe_runs;
begin
  select org_id into v_org from workspaces where id = p_workspace_id;
  if v_org is null then
    raise exception 'workspace % not found', p_workspace_id;
  end if;

  insert into recipe_runs (workspace_id, recipe_version_id, dataset_version_in, job_id)
  values (p_workspace_id, p_recipe_version_id, p_dataset_version_in, p_job_id)
  returning * into v_run;

  perform write_audit(
    v_org, p_workspace_id, 'recipe.run.started', 'recipe_run', v_run.id::text,
    jsonb_build_object('recipe_version_id', p_recipe_version_id,
                       'dataset_version_in', p_dataset_version_in)
  );

  return v_run;
end;
$fn$;

revoke all on function start_recipe_run(uuid, uuid, uuid, uuid) from public, anon, authenticated;
grant execute on function start_recipe_run(uuid, uuid, uuid, uuid) to service_role;

create or replace function finish_recipe_run(
  p_run_id              uuid,
  p_status              recipe_run_status,
  p_dataset_version_out uuid default null,
  p_rows_processed      bigint default 0,
  p_rows_matched        bigint default 0,
  p_auto_corrections    integer default 0,
  p_deviations_count    integer default 0,
  p_automation_rate     numeric default null,
  p_invariant_status    text default null,
  p_summary             jsonb default '{}'::jsonb
)
returns recipe_runs
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_run recipe_runs;
  v_org uuid;
begin
  update recipe_runs
     set status              = p_status,
         dataset_version_out = p_dataset_version_out,
         rows_processed      = coalesce(p_rows_processed, 0),
         rows_matched        = coalesce(p_rows_matched, 0),
         auto_corrections    = coalesce(p_auto_corrections, 0),
         deviations_count    = coalesce(p_deviations_count, 0),
         automation_rate     = p_automation_rate,
         invariant_status    = p_invariant_status,
         summary             = coalesce(p_summary, '{}'::jsonb),
         finished_at         = now()
   where id = p_run_id
  returning * into v_run;

  if not found then
    raise exception 'run % not found', p_run_id;
  end if;

  select org_id into v_org from workspaces where id = v_run.workspace_id;

  perform write_audit(
    v_org, v_run.workspace_id, 'recipe.run.' || p_status::text, 'recipe_run', v_run.id::text,
    jsonb_build_object('rows_processed', p_rows_processed,
                       'deviations', p_deviations_count,
                       'automation_rate', p_automation_rate,
                       'invariant_status', p_invariant_status)
  );

  return v_run;
end;
$fn$;

revoke all on function finish_recipe_run(uuid, recipe_run_status, uuid, bigint, bigint, integer, integer, numeric, text, jsonb)
  from public, anon, authenticated;
grant execute on function finish_recipe_run(uuid, recipe_run_status, uuid, bigint, bigint, integer, integer, numeric, text, jsonb)
  to service_role;

create or replace function record_deviations(p_run_id uuid, p_deviations jsonb)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_workspace uuid;
  v_count     integer;
begin
  select workspace_id into v_workspace from recipe_runs where id = p_run_id;
  if v_workspace is null then
    raise exception 'run % not found', p_run_id;
  end if;

  insert into deviations (
    run_id, workspace_id, type, severity, group_key, title, detail, column_name,
    source_value, suggested_value, affected_rows, materiality_gbp, evidence
  )
  select
    p_run_id,
    v_workspace,
    (item ->> 'type')::deviation_type,
    (item ->> 'severity')::deviation_severity,
    item ->> 'group_key',
    item ->> 'title',
    item ->> 'detail',
    nullif(item ->> 'column_name', ''),
    nullif(item ->> 'source_value', ''),
    nullif(item ->> 'suggested_value', ''),
    coalesce((item ->> 'affected_rows')::bigint, 0),
    nullif(item ->> 'materiality_gbp', '')::numeric,
    coalesce(item -> 'evidence', '{}'::jsonb)
  from jsonb_array_elements(coalesce(p_deviations, '[]'::jsonb)) as item;

  get diagnostics v_count = row_count;
  return v_count;
end;
$fn$;

revoke all on function record_deviations(uuid, jsonb) from public, anon, authenticated;
grant execute on function record_deviations(uuid, jsonb) to service_role;

-- -----------------------------------------------------------------------------
-- Resolving a deviation. MVP criterion 9, and the reason the rest exists.
--
-- Two things happen in one transaction, and they have to: the deviation is
-- marked resolved, and -- when the resolution names a canonical value -- the
-- mapping table learns it. If those were separate statements, a crash between
-- them would leave a question answered on screen that comes back next month,
-- which is precisely the failure criterion 9 rules out.
--
-- Granted to authenticated because this is the human's decision, and it
-- re-checks workspace access for the same reason every other SECURITY DEFINER
-- function here does.
-- -----------------------------------------------------------------------------

create or replace function resolve_deviation(
  p_deviation_id   uuid,
  p_resolution     deviation_resolution,
  p_resolved_value text default null,
  p_note           text default null
)
returns deviations
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_user      uuid := auth.uid();
  v_deviation deviations;
  v_org       uuid;
  v_table     mapping_tables;
  v_learned   boolean := false;
begin
  if v_user is null then
    raise exception 'not authenticated' using errcode = 'insufficient_privilege';
  end if;

  select * into v_deviation from deviations where id = p_deviation_id for update;

  if not found or not has_workspace_access(v_deviation.workspace_id) then
    raise exception 'deviation % not found', p_deviation_id using errcode = 'insufficient_privilege';
  end if;

  if p_resolution = 'pending' then
    raise exception 'a deviation cannot be resolved as pending' using errcode = 'check_violation';
  end if;

  -- 'mapped' is the resolution that teaches. The others record a judgement
  -- without adding to the vocabulary -- 'ignored' on a one-off, 'rejected' on
  -- a suggestion that was wrong.
  if p_resolution = 'mapped' then
    if nullif(btrim(coalesce(p_resolved_value, '')), '') is null then
      raise exception 'mapping a deviation needs the value to map it to'
        using errcode = 'check_violation';
    end if;
    if v_deviation.source_value is null then
      raise exception 'deviation % has no source value to map', p_deviation_id
        using errcode = 'check_violation';
    end if;

    v_table := ensure_mapping_table(
      v_deviation.workspace_id,
      coalesce(v_deviation.column_name, 'entity') || ' mappings',
      'entity',
      v_user
    );

    perform upsert_mapping_entries(
      v_table.id,
      jsonb_build_array(jsonb_build_object(
        'source_key', lower(btrim(v_deviation.source_value)),
        'source_value', v_deviation.source_value,
        'canonical_value', btrim(p_resolved_value)
      )),
      v_user
    );

    v_learned := true;
  end if;

  update deviations
     set resolution      = p_resolution,
         resolved_value  = nullif(btrim(coalesce(p_resolved_value, '')), ''),
         resolved_by     = v_user,
         resolved_at     = now(),
         resolution_note = p_note
   where id = p_deviation_id
  returning * into v_deviation;

  select org_id into v_org from workspaces where id = v_deviation.workspace_id;

  perform write_audit(
    v_org, v_deviation.workspace_id, 'deviation.resolved', 'deviation', v_deviation.id::text,
    jsonb_build_object('type', v_deviation.type, 'resolution', p_resolution,
                       'source_value', v_deviation.source_value,
                       'resolved_value', p_resolved_value,
                       'learned', v_learned)
  );

  return v_deviation;
end;
$fn$;

revoke all on function resolve_deviation(uuid, deviation_resolution, text, text) from public, anon;
grant execute on function resolve_deviation(uuid, deviation_resolution, text, text)
  to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- Editing a recipe by hand (section 4: "Users must be able to read it, reorder
-- it, disable a step").
--
-- Writes a new version rather than mutating the current one, because
-- recipe_versions is immutable and historical runs point at it.
-- -----------------------------------------------------------------------------

create or replace function update_recipe_steps(
  p_recipe_id   uuid,
  p_steps       jsonb,
  p_change_note text default null
)
returns recipe_versions
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_user    uuid := auth.uid();
  v_recipe  cleaning_recipes;
  v_org     uuid;
  v_next    integer;
  v_version recipe_versions;
  v_invariants jsonb := '[]'::jsonb;
begin
  if v_user is null then
    raise exception 'not authenticated' using errcode = 'insufficient_privilege';
  end if;

  select * into v_recipe from cleaning_recipes where id = p_recipe_id;
  if not found or not has_workspace_access(v_recipe.workspace_id) then
    raise exception 'recipe % not found', p_recipe_id using errcode = 'insufficient_privilege';
  end if;

  if v_recipe.current_version_id is not null then
    select invariants into v_invariants from recipe_versions where id = v_recipe.current_version_id;
  end if;

  select coalesce(max(version_no) + 1, 1) into v_next
  from recipe_versions where recipe_id = p_recipe_id;

  insert into recipe_versions (recipe_id, version_no, steps, invariants, change_note, created_by)
  values (p_recipe_id, v_next, coalesce(p_steps, '[]'::jsonb),
          coalesce(v_invariants, '[]'::jsonb), p_change_note, v_user)
  returning * into v_version;

  update cleaning_recipes set current_version_id = v_version.id where id = p_recipe_id;

  select org_id into v_org from workspaces where id = v_recipe.workspace_id;

  perform write_audit(
    v_org, v_recipe.workspace_id, 'recipe.version.edited', 'recipe_version', v_version.id::text,
    jsonb_build_object('recipe_id', p_recipe_id, 'version_no', v_next, 'change_note', p_change_note)
  );

  return v_version;
end;
$fn$;

revoke all on function update_recipe_steps(uuid, jsonb, text) from public, anon;
grant execute on function update_recipe_steps(uuid, jsonb, text) to authenticated, service_role;
