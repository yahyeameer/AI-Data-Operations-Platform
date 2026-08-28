-- =============================================================================
-- Recipes, mapping tables and deviations (PRD section 4 and 5).
--
-- This is the migration behind MVP criteria 6 and 9, which the PRD calls the
-- product: "A second month's file auto-matches the recipe and replays it", and
-- "A human resolution of an ambiguous match writes back to the mapping table
-- and does not recur next month". Everything before this cleans one file well.
-- This is what makes the second month cheaper than the first.
--
-- Three shapes, and the distinction between them is the whole design:
--
--   cleaning_recipes   what we have learned to do to this recurring file
--   mapping_tables     what we have learned about this client's vocabulary
--   recipe_runs        what actually happened on one particular month
--
-- The middle one is easy to get wrong. Section 4 is explicit that vendor and
-- account-code mappings are "shared, growable tables scoped to the workspace,
-- not parameters frozen inside a step" -- because a mapping frozen into recipe
-- v3 means every new supplier needs a recipe v4, and the automation rate never
-- climbs past the first month's vocabulary.
-- =============================================================================

-- Replaying a recipe is a new kind of work for the queue.
--
-- Added here rather than by editing the migration that created the enum: that
-- one has shipped, and rewriting an applied migration means the schema a fresh
-- database builds differs from the one an existing database has. ALTER TYPE
-- ADD VALUE is safe inside a transaction as long as nothing in the same
-- transaction uses the new value, and nothing here does.
alter type agent_job_kind add value if not exists 'replay_recipe';

create type recipe_run_status as enum (
  'running',
  'succeeded',      -- replayed cleanly, output version written
  'needs_review',   -- replayed, but deviations need a human before it counts
  'blocked',        -- an invariant failed; output deliberately not written
  'failed'
);

create type deviation_type as enum (
  'unmapped_value',      -- a value the mapping table has never seen
  'ambiguous_match',     -- close to something known, not close enough to assume
  'new_column',          -- the file grew a column the recipe does not know
  'missing_column',      -- the file lost a column a step needs
  'type_drift',          -- a column changed what kind of thing it holds
  'invariant_failure',   -- section 5.3: the run looks wrong in aggregate
  'step_failed'          -- a step could not execute at all
);

create type deviation_severity as enum ('auto', 'review', 'block');

create type deviation_resolution as enum ('pending', 'accepted', 'rejected', 'mapped', 'ignored');

-- -----------------------------------------------------------------------------
-- Mapping tables.
--
-- Scoped to the workspace, not to a recipe: one client's supplier vocabulary is
-- the same vocabulary whether it arrives in the sales export or the purchase
-- ledger, and making each recipe keep its own copy would mean resolving the
-- same name twice.
-- -----------------------------------------------------------------------------

create table mapping_tables (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces (id) on delete cascade,
  name         text not null check (length(btrim(name)) between 1 and 200),
  -- What the mapping is *about* ('supplier', 'account_code', 'product'), used
  -- to offer the right table when a new recipe step needs one.
  kind         text not null default 'entity',
  created_by   uuid references auth.users (id) on delete set null,
  created_at   timestamptz not null default now(),
  unique (workspace_id, name)
);

create index mapping_tables_workspace_idx on mapping_tables (workspace_id);

create table mapping_entries (
  id               uuid primary key default gen_random_uuid(),
  mapping_table_id uuid not null references mapping_tables (id) on delete cascade,
  -- Normalised for lookup: what arrives in the file varies by whitespace and
  -- case, and a mapping keyed on the raw text would miss half its own entries.
  source_key       text not null,
  -- What actually arrived, kept for the audit trail and for showing the
  -- accountant what they decided about.
  source_value     text not null,
  canonical_value  text not null,
  -- Whether a person decided this or the system inferred it. Section 5.4 wants
  -- automation measured honestly, and an entry nobody confirmed is weaker
  -- evidence than one somebody did.
  confirmed_by     uuid references auth.users (id) on delete set null,
  confirmed_at     timestamptz,
  -- How often it has resolved something. This is the number that shows the
  -- mapping earning its keep.
  hit_count        bigint not null default 0 check (hit_count >= 0),
  created_at       timestamptz not null default now(),
  unique (mapping_table_id, source_key)
);

create index mapping_entries_table_idx on mapping_entries (mapping_table_id);

-- -----------------------------------------------------------------------------
-- Recipes.
--
-- source_signature is what makes month 2 automatic. It is a fingerprint of the
-- file's shape -- column names, types, header position -- deliberately not of
-- its contents, which change every month by design.
--
-- Unique per workspace so two recipes cannot both claim the same incoming file.
-- Without that constraint the match becomes "whichever row came back first",
-- which is a bug that only appears once a client has two similar reports.
-- -----------------------------------------------------------------------------

create table cleaning_recipes (
  id                 uuid primary key default gen_random_uuid(),
  workspace_id       uuid not null references workspaces (id) on delete cascade,
  dataset_id         uuid references datasets (id) on delete set null,
  name               text not null check (length(btrim(name)) between 1 and 200),
  source_signature   text,
  current_version_id uuid,
  template_origin_id uuid references cleaning_recipes (id) on delete set null,
  enabled            boolean not null default true,
  created_by         uuid references auth.users (id) on delete set null,
  created_at         timestamptz not null default now(),
  unique (workspace_id, source_signature)
);

create index cleaning_recipes_workspace_idx on cleaning_recipes (workspace_id);

-- -----------------------------------------------------------------------------
-- Recipe versions. Immutable, because recipe_runs point at them.
--
-- Section 4: "recipe_runs pins a recipe_version, never a recipe. Editing a
-- recipe must never retroactively change what a historical run claims to have
-- done -- that is an audit-integrity requirement, not a nicety."
-- -----------------------------------------------------------------------------

create table recipe_versions (
  id             uuid primary key default gen_random_uuid(),
  recipe_id      uuid not null references cleaning_recipes (id) on delete cascade,
  version_no     integer not null check (version_no >= 1),
  -- The ordered step list of section 4. Order is part of the meaning: trimming
  -- whitespace before mapping vendor names finds matches the reverse misses.
  steps          jsonb not null default '[]'::jsonb,
  -- Post-run checks (section 5.3) that can fail a run which had no deviations.
  invariants     jsonb not null default '[]'::jsonb,
  change_note    text,
  -- The run whose approvals taught us this version.
  learned_from   uuid,
  created_by     uuid references auth.users (id) on delete set null,
  created_at     timestamptz not null default now(),
  unique (recipe_id, version_no)
);

create index recipe_versions_recipe_idx on recipe_versions (recipe_id, version_no desc);

alter table cleaning_recipes
  add constraint cleaning_recipes_current_version_fk
  foreign key (current_version_id) references recipe_versions (id) on delete set null;

-- -----------------------------------------------------------------------------
-- Runs. One row per attempt to replay a recipe against a month's file.
--
-- dataset_version_out is nullable on purpose: a blocked run has no output, and
-- that absence is the record that nothing was produced. Writing the output and
-- then marking the run blocked would leave a cleaned version sitting there for
-- something downstream to pick up.
-- -----------------------------------------------------------------------------

create table recipe_runs (
  id                  uuid primary key default gen_random_uuid(),
  workspace_id        uuid not null references workspaces (id) on delete cascade,
  recipe_version_id   uuid not null references recipe_versions (id) on delete restrict,
  dataset_version_in  uuid not null references dataset_versions (id) on delete cascade,
  dataset_version_out uuid references dataset_versions (id) on delete set null,
  job_id              uuid references agent_jobs (id) on delete set null,

  rows_processed      bigint not null default 0 check (rows_processed >= 0),
  rows_matched        bigint not null default 0 check (rows_matched >= 0),
  auto_corrections    integer not null default 0 check (auto_corrections >= 0),
  deviations_count    integer not null default 0 check (deviations_count >= 0),
  -- Stored rather than derived so a historical run keeps the number it actually
  -- scored, even after the definition of the metric changes.
  automation_rate     numeric(5, 4) check (automation_rate between 0 and 1),
  invariant_status    text,
  status              recipe_run_status not null default 'running',
  summary             jsonb not null default '{}'::jsonb,
  started_at          timestamptz not null default now(),
  finished_at         timestamptz,

  constraint recipe_runs_output_ck check (
    -- Only a clean run produces an output version.
    dataset_version_out is null or status = 'succeeded'
  )
);

create index recipe_runs_recipe_idx on recipe_runs (recipe_version_id, started_at desc);
create index recipe_runs_workspace_idx on recipe_runs (workspace_id, started_at desc);
create index recipe_runs_input_idx on recipe_runs (dataset_version_in);

-- -----------------------------------------------------------------------------
-- Deviations: everything the recipe could not handle on its own.
--
-- Section 5 keeps these separate from auto-applied fixes for a reason. A run
-- that reports "93 corrections and 31 deviations" is legible; one that reports
-- "124 changes" hides the 31 that need a person.
-- -----------------------------------------------------------------------------

create table deviations (
  id               uuid primary key default gen_random_uuid(),
  run_id           uuid not null references recipe_runs (id) on delete cascade,
  workspace_id     uuid not null references workspaces (id) on delete cascade,
  type             deviation_type not null,
  severity         deviation_severity not null,
  -- Groups identical deviations so 31 unknown suppliers is one screen, not 31.
  group_key        text not null,
  title            text not null,
  detail           text,
  column_name      text,
  -- The value that could not be resolved, where there is one. This is what a
  -- mapping resolution keys on.
  source_value     text,
  suggested_value  text,
  affected_rows    bigint not null default 0 check (affected_rows >= 0),
  materiality_gbp  numeric(18, 2),
  evidence         jsonb not null default '{}'::jsonb,

  resolution       deviation_resolution not null default 'pending',
  resolved_value   text,
  resolved_by      uuid references auth.users (id) on delete set null,
  resolved_at      timestamptz,
  resolution_note  text,

  created_at       timestamptz not null default now(),

  constraint deviations_resolution_ck check (
    (resolution = 'pending' and resolved_at is null)
    or (resolution <> 'pending' and resolved_at is not null)
  )
);

create index deviations_run_idx
  on deviations (run_id, severity, materiality_gbp desc nulls last);
create index deviations_open_idx
  on deviations (workspace_id, resolution) where resolution = 'pending';

-- -----------------------------------------------------------------------------
-- Immutability.
--
-- recipe_versions and recipe_runs are evidence. A deviation's *decision* fields
-- change once, like a proposed change's.
-- -----------------------------------------------------------------------------

create trigger recipe_versions_immutable
  before update or delete on recipe_versions
  for each row execute function reject_mutation();

create or replace function recipe_runs_guard()
returns trigger
language plpgsql
as $fn$
begin
  if tg_op = 'DELETE' then
    raise exception 'recipe_runs is append-only: rows may not be deleted'
      using errcode = 'restrict_violation';
  end if;

  if old.status <> 'running' then
    raise exception 'run % already finished as %; a completed run is immutable', old.id, old.status
      using errcode = 'restrict_violation';
  end if;

  if new.id <> old.id
     or new.recipe_version_id <> old.recipe_version_id
     or new.dataset_version_in <> old.dataset_version_in
     or new.started_at <> old.started_at then
    raise exception 'run % identity columns are immutable', old.id
      using errcode = 'restrict_violation';
  end if;

  return new;
end;
$fn$;

create trigger recipe_runs_guard
  before update or delete on recipe_runs
  for each row execute function recipe_runs_guard();

create or replace function deviations_guard()
returns trigger
language plpgsql
as $fn$
begin
  if tg_op = 'DELETE' then
    raise exception 'deviations is append-only: rows may not be deleted'
      using errcode = 'restrict_violation';
  end if;

  if old.resolution <> 'pending' then
    raise exception 'deviation % is already %; resolutions are final', old.id, old.resolution
      using errcode = 'restrict_violation';
  end if;

  if new.id <> old.id
     or new.run_id <> old.run_id
     or new.type <> old.type
     or new.source_value is distinct from old.source_value
     or new.evidence is distinct from old.evidence
     or new.created_at <> old.created_at then
    raise exception 'deviation % may only have its resolution updated', old.id
      using errcode = 'restrict_violation';
  end if;

  return new;
end;
$fn$;

create trigger deviations_guard
  before update or delete on deviations
  for each row execute function deviations_guard();

-- =============================================================================
-- RLS. Same shape as everywhere else: members read, nobody writes directly.
-- =============================================================================

alter table mapping_tables    enable row level security;
alter table mapping_entries   enable row level security;
alter table cleaning_recipes  enable row level security;
alter table recipe_versions   enable row level security;
alter table recipe_runs       enable row level security;
alter table deviations        enable row level security;

create policy mapping_tables_select_members
  on mapping_tables for select to authenticated
  using (has_workspace_access(workspace_id));

create policy mapping_entries_select_members
  on mapping_entries for select to authenticated
  using (has_workspace_access(
    (select mt.workspace_id from mapping_tables mt where mt.id = mapping_table_id)
  ));

create policy cleaning_recipes_select_members
  on cleaning_recipes for select to authenticated
  using (has_workspace_access(workspace_id));

create policy recipe_versions_select_members
  on recipe_versions for select to authenticated
  using (has_workspace_access(
    (select r.workspace_id from cleaning_recipes r where r.id = recipe_id)
  ));

create policy recipe_runs_select_members
  on recipe_runs for select to authenticated
  using (has_workspace_access(workspace_id));

create policy deviations_select_members
  on deviations for select to authenticated
  using (has_workspace_access(workspace_id));

grant select on
  mapping_tables, mapping_entries, cleaning_recipes,
  recipe_versions, recipe_runs, deviations
  to authenticated;

grant all on
  mapping_tables, mapping_entries, cleaning_recipes,
  recipe_versions, recipe_runs, deviations
  to service_role;
