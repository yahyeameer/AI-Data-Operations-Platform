-- =============================================================================
-- Phase 2 / the Hermes seam.
--
-- The agent does not live in the web app. It is a long-running worker on its
-- own host (PRD section 8: "Hermes orchestrates"), and this migration is the
-- contract between the two. Everything the dashboard asks for and everything
-- the agent produces passes through these tables.
--
-- Why a queue table rather than the web app calling the agent over HTTP:
--
--   * Parsing a 50 MB workbook and profiling it takes minutes, not the seconds
--     a request handler can hold. A queue turns that into a status the UI polls
--     instead of a connection that times out.
--   * The agent host needs no inbound port and no public hostname. It dials
--     out to Postgres and nothing dials in, which removes the entire class of
--     "who else can reach the agent" questions from section 13.
--   * A crashed or restarted worker loses nothing. The job is still queued; its
--     lease simply expires and another claim picks it up.
--   * Every job carries org_id, so the tenant boundary the rest of the schema
--     enforces does not stop at the agent's edge.
--
-- The same reasoning that put Week 1's writes behind SECURITY DEFINER RPCs
-- applies here: a job and its audit row are written in one transaction, so a
-- job that ran without an audit trail is unreachable rather than merely
-- unlikely.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Enums
-- -----------------------------------------------------------------------------

-- 'cancelled' is terminal and set by a human; 'failed' is terminal and set by
-- the worker once attempts are exhausted. A job that failed with attempts left
-- returns to 'queued', which is what makes retries invisible to the UI.
create type agent_job_status as enum ('queued', 'running', 'succeeded', 'failed', 'cancelled');

-- The tool contract of PRD section 9, as far as it is implemented. An enum
-- rather than free text so an unknown job kind is refused by the database at
-- enqueue time instead of by the worker three minutes later.
create type agent_job_kind as enum (
  'parse_workbook',      -- raw upload   -> interpretation + typed dataset version
  'profile_dataset',     -- version      -> column statistics and quality signals
  'propose_cleaning',    -- profile      -> explained, evidenced change proposals
  'apply_cleaning',      -- approvals    -> new immutable version, Parquet written
  'query_dataset',       -- NL question  -> executed SQL + provenance
  'reconcile_sources',   -- two versions -> matched / unmatched with materiality
  'generate_report'      -- version      -> a period report in the exports bucket
);

create type proposed_change_status as enum ('pending', 'approved', 'rejected', 'applied', 'superseded');

-- Section 5.1's confidence tiers, which decide what is auto-applied, what is
-- queued for review, and what is refused outright.
create type change_confidence as enum ('high', 'medium', 'low');

-- -----------------------------------------------------------------------------
-- Worker registry.
--
-- Exists so the dashboard can answer "is my agent actually up?" without the
-- accountant having to SSH anywhere. A worker upserts a heartbeat every few
-- seconds; the UI treats a stale row as offline rather than deleting it, so a
-- worker that died is visibly dead instead of merely absent.
--
-- Not tenant-scoped: one self-hosted worker serves the whole deployment. It is
-- readable by any authenticated user because "the agent is online" carries no
-- customer data, and hiding it would only make the UI lie during an outage.
-- -----------------------------------------------------------------------------

create table agent_workers (
  id            text primary key check (length(btrim(id)) between 1 and 200),
  hostname      text,
  version       text,
  capabilities  agent_job_kind[] not null default '{}',
  started_at    timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  jobs_claimed  bigint not null default 0 check (jobs_claimed >= 0),
  metadata      jsonb not null default '{}'::jsonb
);

create index agent_workers_seen_idx on agent_workers (last_seen_at desc);

-- -----------------------------------------------------------------------------
-- The queue.
--
-- lease_expires_at rather than a bare claimed_at: the recovery rule is then a
-- property of the row that any reader can evaluate, instead of a timeout
-- constant the worker and the dashboard each have to agree on separately.
-- -----------------------------------------------------------------------------

create table agent_jobs (
  id                 uuid primary key default gen_random_uuid(),
  org_id             uuid not null references organizations (id) on delete cascade,
  workspace_id       uuid not null references workspaces (id) on delete cascade,
  dataset_id         uuid references datasets (id) on delete cascade,
  dataset_version_id uuid references dataset_versions (id) on delete set null,
  raw_upload_id      uuid references raw_uploads (id) on delete set null,

  kind               agent_job_kind not null,
  status             agent_job_status not null default 'queued',
  -- Lower runs first. Interactive work (a question typed into the dashboard)
  -- should not sit behind a batch reparse of last quarter.
  priority           smallint not null default 100 check (priority between 0 and 1000),

  payload            jsonb not null default '{}'::jsonb,
  result             jsonb,
  error              text,

  attempts           integer not null default 0 check (attempts >= 0),
  max_attempts       integer not null default 3 check (max_attempts between 1 and 10),

  claimed_by         text references agent_workers (id) on delete set null,
  claimed_at         timestamptz,
  lease_expires_at   timestamptz,
  progress           jsonb not null default '{}'::jsonb,

  requested_by       uuid references auth.users (id) on delete set null,
  created_at         timestamptz not null default now(),
  started_at         timestamptz,
  finished_at        timestamptz,

  -- A terminal job must say when it finished; a queued one must not hold a
  -- lease. Cheap to state, and it makes a stuck-looking queue diagnosable from
  -- the row alone.
  constraint agent_jobs_terminal_ck check (
    (status in ('succeeded', 'failed', 'cancelled') and finished_at is not null)
    or (status in ('queued', 'running') and finished_at is null)
  ),
  constraint agent_jobs_lease_ck check (
    status <> 'running' or (claimed_by is not null and lease_expires_at is not null)
  )
);

-- The claim query's index: oldest highest-priority runnable job. Partial,
-- because finished jobs are the ones that accumulate and they are never
-- claimed again.
create index agent_jobs_claim_idx
  on agent_jobs (priority, created_at)
  where status in ('queued', 'running');

create index agent_jobs_workspace_idx on agent_jobs (workspace_id, created_at desc);
create index agent_jobs_dataset_idx on agent_jobs (dataset_id, created_at desc)
  where dataset_id is not null;

-- -----------------------------------------------------------------------------
-- Profiles (PRD section 8: profile statistics are what the model is allowed to
-- see, in place of rows).
--
-- One row per dataset version, immutable for the same reason the versions are:
-- a profile that changed after a proposal was made would invalidate the
-- evidence attached to that proposal.
-- -----------------------------------------------------------------------------

create table dataset_profiles (
  id                 uuid primary key default gen_random_uuid(),
  dataset_version_id uuid not null unique references dataset_versions (id) on delete cascade,
  row_count          bigint not null check (row_count >= 0),
  column_count       integer not null check (column_count >= 0),
  -- Per column: inferred type, null and distinct counts, min/max, a sample of
  -- offending values. Never whole rows.
  columns            jsonb not null default '[]'::jsonb,
  -- Whole-dataset signals: duplicate key candidates, date-range coverage, and
  -- the numeric totals that post-run invariants (section 5.3) check against.
  signals            jsonb not null default '{}'::jsonb,
  produced_by_job_id uuid references agent_jobs (id) on delete set null,
  created_at         timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- Proposed changes (section 5: the deviation engine's output, and the thing the
-- accountant actually approves).
--
-- A proposal is data, not an action. It records what the agent would do, why,
-- the evidence behind it, and what it is worth in money -- which is what
-- section 5.2's materiality ranking sorts on. Applying it is a separate,
-- audited step.
-- -----------------------------------------------------------------------------

create table proposed_changes (
  id                 uuid primary key default gen_random_uuid(),
  workspace_id       uuid not null references workspaces (id) on delete cascade,
  dataset_version_id uuid not null references dataset_versions (id) on delete cascade,
  job_id             uuid references agent_jobs (id) on delete set null,

  -- Grouped approval (section 5.2) works on this: 400 parenthesised negatives
  -- are one decision, not 400.
  group_key          text not null,
  step_type          text not null,
  column_name        text,
  title              text not null,
  rationale          text not null,
  -- The deterministic operation to run if approved. The model proposes this
  -- shape; it never executes it and never computes its result.
  operation          jsonb not null,
  evidence           jsonb not null default '{}'::jsonb,

  confidence         change_confidence not null,
  affected_rows      bigint not null default 0 check (affected_rows >= 0),
  -- Nullable: a column rename has no monetary weight, and 0 would sort it
  -- alongside genuinely immaterial money changes.
  materiality_gbp    numeric(18, 2),

  status             proposed_change_status not null default 'pending',
  decided_by         uuid references auth.users (id) on delete set null,
  decided_at         timestamptz,
  decision_note      text,
  created_at         timestamptz not null default now(),

  constraint proposed_changes_decision_ck check (
    (status = 'pending' and decided_at is null)
    or (status <> 'pending' and decided_at is not null)
  )
);

create index proposed_changes_version_idx
  on proposed_changes (dataset_version_id, status, materiality_gbp desc nulls last);
create index proposed_changes_group_idx on proposed_changes (dataset_version_id, group_key);

-- -----------------------------------------------------------------------------
-- Analysis runs (section 7: every displayed number traces to SQL, a version and
-- a row-id set).
--
-- The executed SQL is stored verbatim. If it is not recorded at execution time
-- it cannot be reconstructed afterwards, and "show me where this came from"
-- becomes a promise the product cannot keep.
-- -----------------------------------------------------------------------------

create table analysis_runs (
  id                 uuid primary key default gen_random_uuid(),
  workspace_id       uuid not null references workspaces (id) on delete cascade,
  dataset_version_id uuid not null references dataset_versions (id) on delete cascade,
  job_id             uuid references agent_jobs (id) on delete set null,
  question           text,
  executed_sql       text not null,
  result             jsonb not null default '{}'::jsonb,
  -- Row identifiers behind the figure, so the drill-down is a lookup rather
  -- than a re-run that might disagree.
  row_refs           jsonb not null default '[]'::jsonb,
  model_used         text,
  duration_ms        integer check (duration_ms >= 0),
  created_by         uuid references auth.users (id) on delete set null,
  created_at         timestamptz not null default now()
);

create index analysis_runs_version_idx on analysis_runs (dataset_version_id, created_at desc);
create index analysis_runs_workspace_idx on analysis_runs (workspace_id, created_at desc);

-- -----------------------------------------------------------------------------
-- Immutability. Same rule as Week 1, same reason: these rows are evidence.
--
-- agent_jobs is deliberately not in this list -- a job row is workflow state and
-- has to move through its lifecycle. Its trail comes from the RPCs below, which
-- write to audit_logs at each transition.
-- -----------------------------------------------------------------------------

create trigger dataset_profiles_immutable
  before update or delete on dataset_profiles
  for each row execute function reject_mutation();

create trigger analysis_runs_immutable
  before update or delete on analysis_runs
  for each row execute function reject_mutation();

-- A proposal's decision fields change exactly once, pending -> decided, with a
-- single further approved -> applied step once the run that used it completes.
-- Everything describing what was proposed is frozen, so an approval can never
-- be re-pointed at a different operation after the fact.
create or replace function proposed_changes_guard()
returns trigger
language plpgsql
as $fn$
begin
  if tg_op = 'DELETE' then
    raise exception 'proposed_changes is append-only: rows may not be deleted'
      using errcode = 'restrict_violation';
  end if;

  if old.status <> 'pending' and not (old.status = 'approved' and new.status = 'applied') then
    raise exception 'proposed change % is already %; decisions are final', old.id, old.status
      using errcode = 'restrict_violation';
  end if;

  if new.id <> old.id
     or new.workspace_id <> old.workspace_id
     or new.dataset_version_id <> old.dataset_version_id
     or new.operation is distinct from old.operation
     or new.evidence is distinct from old.evidence
     or new.title <> old.title
     or new.rationale <> old.rationale
     or new.confidence <> old.confidence
     or new.created_at <> old.created_at then
    raise exception 'proposed change % may only have its decision updated', old.id
      using errcode = 'restrict_violation';
  end if;

  return new;
end;
$fn$;

create trigger proposed_changes_guard
  before update or delete on proposed_changes
  for each row execute function proposed_changes_guard();

-- =============================================================================
-- RLS. Identical shape to Week 1: members read, nobody writes directly.
-- =============================================================================

alter table agent_jobs        enable row level security;
alter table dataset_profiles  enable row level security;
alter table proposed_changes  enable row level security;
alter table analysis_runs     enable row level security;
alter table agent_workers     enable row level security;

create policy agent_jobs_select_members
  on agent_jobs for select to authenticated
  using (has_workspace_access(workspace_id));

create policy dataset_profiles_select_members
  on dataset_profiles for select to authenticated
  using (has_workspace_access(workspace_of_dataset(
    (select dv.dataset_id from dataset_versions dv where dv.id = dataset_version_id)
  )));

create policy proposed_changes_select_members
  on proposed_changes for select to authenticated
  using (has_workspace_access(workspace_id));

create policy analysis_runs_select_members
  on analysis_runs for select to authenticated
  using (has_workspace_access(workspace_id));

-- Liveness only. No workspace or customer data lives on this table.
create policy agent_workers_select_authenticated
  on agent_workers for select to authenticated
  using (true);

grant select on
  agent_jobs, dataset_profiles, proposed_changes, analysis_runs, agent_workers
  to authenticated;

grant all on
  agent_jobs, dataset_profiles, proposed_changes, analysis_runs, agent_workers
  to service_role;
