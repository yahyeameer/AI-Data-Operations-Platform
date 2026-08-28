-- =============================================================================
-- The job queue.
--
-- Why this exists: the dashboard used to call the parser over HTTP and wait for
-- the answer. That put three independent clocks in series on one request --
-- Vercel's function cap, the parser's cold start, and the analysis itself --
-- and the shortest of them decided whether the user saw a result or an error.
-- On the Hobby plan that clock is 60 seconds, and a cold start on a container
-- carrying polars, duckdb and pyarrow can spend all of it before our own code
-- runs. The user got "the analysis is taking longer than this plan allows" for
-- work that had not yet started.
--
-- A queue removes the clock rather than enlarging it. The dashboard writes a
-- row and returns; the worker claims it whenever it is awake and writes the
-- result back. A sleeping worker now delays a job instead of failing it, and
-- the analysis may take as long as it takes.
--
--   dashboard  ──▶  agent_jobs  ◀── claims ──  worker (anywhere, no inbound port)
--   polls      ◀────────┼───────── writes ───▶  result
--
-- The same rule as every other write in this schema: a job and its audit row
-- are written in one transaction, so a job that ran without a trail is
-- unreachable rather than merely unlikely.
-- =============================================================================

create type agent_job_status as enum ('queued', 'running', 'succeeded', 'failed', 'cancelled');

-- One kind for now: read the workbook, profile it, and report what should be
-- fixed. Deliberately one job rather than a chain -- the whole point of this
-- phase is that the user presses Analyze once and reads an answer, and a
-- pipeline of four queued steps is four chances to strand them halfway.
create type agent_job_kind as enum ('analyze_workbook');

-- -----------------------------------------------------------------------------
-- Worker registry.
--
-- So the dashboard can say "the engine is awake" without anyone having to SSH
-- anywhere. A stale row is treated as offline rather than deleted: a worker
-- that died should be visibly dead, not silently absent.
--
-- Not tenant-scoped -- one worker serves the deployment -- and readable by any
-- authenticated user because liveness carries no customer data. It exposes the
-- id, version and heartbeat only; hostname is host inventory, not liveness, and
-- is deliberately not stored here.
-- -----------------------------------------------------------------------------

create table agent_workers (
  id            text primary key check (length(btrim(id)) between 1 and 200),
  version       text,
  started_at    timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  jobs_claimed  bigint not null default 0 check (jobs_claimed >= 0)
);

create index agent_workers_seen_idx on agent_workers (last_seen_at desc);

-- -----------------------------------------------------------------------------
-- The queue.
--
-- lease_expires_at rather than a bare claimed_at: the recovery rule becomes a
-- property of the row that any reader can evaluate, instead of a timeout
-- constant the worker and the dashboard have to agree on separately.
-- -----------------------------------------------------------------------------

create table agent_jobs (
  id               uuid primary key default gen_random_uuid(),
  org_id           uuid not null references organizations (id) on delete cascade,
  workspace_id     uuid not null references workspaces (id) on delete cascade,
  dataset_id       uuid references datasets (id) on delete cascade,
  raw_upload_id    uuid references raw_uploads (id) on delete set null,

  kind             agent_job_kind not null,
  status           agent_job_status not null default 'queued',
  priority         smallint not null default 100 check (priority between 0 and 1000),

  payload          jsonb not null default '{}'::jsonb,
  result           jsonb,
  error            text,

  attempts         integer not null default 0 check (attempts >= 0),
  max_attempts     integer not null default 3 check (max_attempts between 1 and 10),

  claimed_by       text references agent_workers (id) on delete set null,
  claimed_at       timestamptz,
  lease_expires_at timestamptz,
  -- {stage, pct, detail} -- whatever the worker last reported. The UI reads it
  -- so "Analyzing…" can say which part it is on.
  progress         jsonb not null default '{}'::jsonb,

  requested_by     uuid references auth.users (id) on delete set null,
  created_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz,

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
-- because finished jobs are what accumulate and they are never claimed again.
create index agent_jobs_claim_idx
  on agent_jobs (priority, created_at)
  where status in ('queued', 'running');

create index agent_jobs_workspace_idx on agent_jobs (workspace_id, created_at desc);

-- =============================================================================
-- RLS. Same shape as the rest of the schema: members read, nobody writes
-- directly, every write goes through a SECURITY DEFINER function below.
-- =============================================================================

alter table agent_jobs    enable row level security;
alter table agent_workers enable row level security;

create policy agent_jobs_select_members
  on agent_jobs for select to authenticated
  using (has_workspace_access(workspace_id));

-- Liveness only. No workspace or customer data lives on this table.
create policy agent_workers_select_authenticated
  on agent_workers for select to authenticated
  using (true);

grant select on agent_jobs, agent_workers to authenticated;
grant all    on agent_jobs, agent_workers to service_role;

-- =============================================================================
-- The queue protocol.
--
--   dashboard : enqueue_agent_job, cancel_agent_job
--   worker    : agent_worker_heartbeat, claim_agent_job,
--               heartbeat_agent_job, finish_agent_job
--
-- The split of privilege is the point. `enqueue_agent_job` is granted to
-- authenticated and re-checks membership itself, so a signed-in accountant can
-- ask for work on their own workspaces and nothing else. Every worker-side
-- function is service_role only -- the worker holds the secret key and could
-- write these tables directly, and routing it through functions keeps the state
-- machine in one reviewable place.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Enqueue.
--
-- Membership is re-checked here rather than trusted from the caller: SECURITY
-- DEFINER steps outside RLS, so a function that assumed its caller was
-- authorised would be a hole straight through the tenant boundary.
--
-- Deduplication is deliberate. Someone who presses Analyze three times while
-- nothing visibly happens should get one analysis, not three.
-- -----------------------------------------------------------------------------

create or replace function enqueue_agent_job(
  p_workspace_id  uuid,
  p_kind          agent_job_kind,
  p_raw_upload_id uuid default null,
  p_dataset_id    uuid default null,
  p_payload       jsonb default '{}'::jsonb
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

  -- Referenced entities must live in the same workspace. Without this, a caller
  -- with access to workspace A could aim a job at workspace B's upload, and the
  -- worker -- holding the service key -- would happily comply.
  if p_raw_upload_id is not null
     and not exists (select 1 from raw_uploads u
                     where u.id = p_raw_upload_id and u.workspace_id = p_workspace_id) then
    raise exception 'upload % is not in workspace %', p_raw_upload_id, p_workspace_id
      using errcode = 'insufficient_privilege';
  end if;

  if p_dataset_id is not null
     and not exists (select 1 from datasets d
                     where d.id = p_dataset_id and d.workspace_id = p_workspace_id) then
    raise exception 'dataset % is not in workspace %', p_dataset_id, p_workspace_id
      using errcode = 'insufficient_privilege';
  end if;

  -- An identical job already waiting or running is returned as-is. dataset_id
  -- is part of the comparison: without it, two analyses of different datasets
  -- in one workspace would collapse into one.
  select * into v_job
  from agent_jobs j
  where j.workspace_id = p_workspace_id
    and j.kind = p_kind
    and j.status in ('queued', 'running')
    and j.raw_upload_id is not distinct from p_raw_upload_id
    and j.dataset_id is not distinct from p_dataset_id
    and j.payload = coalesce(p_payload, '{}'::jsonb)
  order by j.created_at
  limit 1;

  if found then
    return v_job;
  end if;

  insert into agent_jobs (org_id, workspace_id, dataset_id, raw_upload_id, kind, payload, requested_by)
  values (v_org, p_workspace_id, p_dataset_id, p_raw_upload_id, p_kind,
          coalesce(p_payload, '{}'::jsonb), v_user)
  returning * into v_job;

  perform write_audit(
    v_org, p_workspace_id, 'agent.job.enqueued', 'agent_job', v_job.id::text,
    jsonb_build_object('kind', p_kind, 'raw_upload_id', p_raw_upload_id)
  );

  return v_job;
end;
$fn$;

revoke all on function enqueue_agent_job(uuid, agent_job_kind, uuid, uuid, jsonb) from public, anon;
grant execute on function enqueue_agent_job(uuid, agent_job_kind, uuid, uuid, jsonb)
  to authenticated, service_role;

-- -----------------------------------------------------------------------------
-- Worker liveness.
-- -----------------------------------------------------------------------------

create or replace function agent_worker_heartbeat(p_worker_id text, p_version text default null)
returns agent_workers
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_worker agent_workers;
begin
  insert into agent_workers (id, version)
  values (p_worker_id, p_version)
  on conflict (id) do update
    set version      = coalesce(excluded.version, agent_workers.version),
        last_seen_at = now()
  returning * into v_worker;

  return v_worker;
end;
$fn$;

revoke all on function agent_worker_heartbeat(text, text) from public, anon, authenticated;
grant execute on function agent_worker_heartbeat(text, text) to service_role;

-- -----------------------------------------------------------------------------
-- Reap.
--
-- A job whose worker died is recovered by its lease expiring -- but only while
-- it has attempts left. Once they are spent the row would otherwise sit at
-- 'running' for ever: never claimed again because attempts are exhausted, never
-- failed because only a worker reports failure, and never re-queued by the user
-- because the dedup above matches 'running'. The dataset becomes permanently
-- unanalysable behind a spinner that never resolves.
--
-- So the queue closes them itself. Called at the top of every claim, which is
-- the one moment we know a worker is alive to do it.
-- -----------------------------------------------------------------------------

create or replace function reap_expired_agent_jobs()
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_count integer;
begin
  with dead as (
    update agent_jobs
       set status           = 'failed',
           finished_at      = now(),
           lease_expires_at = null,
           error            = coalesce(
             error,
             'The engine stopped responding while running this analysis, and the job ran out '
             || 'of retries. Press Analyze to try again.'
           )
     where status = 'running'
       and lease_expires_at < now()
       and attempts >= max_attempts
    returning org_id, workspace_id, id, kind
  )
  select count(*) into v_count from dead;

  return v_count;
end;
$fn$;

revoke all on function reap_expired_agent_jobs() from public, anon, authenticated;
grant execute on function reap_expired_agent_jobs() to service_role;

-- -----------------------------------------------------------------------------
-- Claim.
--
-- `for update skip locked` is what makes two workers safe: concurrent claims
-- step over each other's locked rows rather than blocking or both returning the
-- same job.
--
-- The candidate set includes running jobs whose lease has expired. That single
-- `or` is the whole crash-recovery story: kill the host mid-analysis and the
-- job becomes claimable again once the lease runs out, with attempts already
-- incremented so a job that reliably kills its worker eventually stops.
--
-- Returns a set rather than a single record so "nothing to do" is an empty
-- result. A NULL composite reaches PostgREST as an object with every field
-- null, which is indistinguishable at a glance from a real job.
-- -----------------------------------------------------------------------------

create or replace function claim_agent_job(p_worker_id text, p_lease_seconds integer default 300)
returns setof agent_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_job agent_jobs;
begin
  perform reap_expired_agent_jobs();

  with candidate as (
    select j.id
    from agent_jobs j
    where (j.status = 'queued' or (j.status = 'running' and j.lease_expires_at < now()))
      and j.attempts < j.max_attempts
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

revoke all on function claim_agent_job(text, integer) from public, anon, authenticated;
grant execute on function claim_agent_job(text, integer) to service_role;

-- -----------------------------------------------------------------------------
-- Lease renewal.
--
-- A long analysis extends its own lease as it goes and reports progress in the
-- same call, so the progress bar and the liveness signal can never disagree --
-- they are the same write.
--
-- The claimed_by guard matters: a worker whose lease expired and was stolen
-- must not renew it and start writing results for a job somebody else owns.
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

revoke all on function heartbeat_agent_job(uuid, text, jsonb, integer) from public, anon, authenticated;
grant execute on function heartbeat_agent_job(uuid, text, jsonb, integer) to service_role;

-- -----------------------------------------------------------------------------
-- Finish.
--
-- One function for both outcomes, because the interesting logic is the decision
-- between "failed for good" and "failed, try again", and that belongs in one
-- place. A worker reporting failure with attempts remaining puts the job back in
-- the queue; the audit row still records the attempt, so a job that succeeded on
-- its third try leaves evidence of the first two.
--
-- p_retryable is how the worker says "do not bother". A dropped connection is
-- worth another attempt; "this file is a legacy .xls" will fail identically
-- three times and only delays the message the user needs to read.
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
  v_job   agent_jobs;
  v_retry boolean;
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

  v_retry := (not p_success) and coalesce(p_retryable, true) and v_job.attempts < v_job.max_attempts;

  update agent_jobs
     set status           = case
                              when p_success then 'succeeded'::agent_job_status
                              when v_retry   then 'queued'::agent_job_status
                              else                'failed'::agent_job_status
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
-- Cancel.
--
-- A queued job can be withdrawn. A running one cannot be killed from here --
-- the worker owns that process -- so this refuses rather than pretending, and
-- the lease is what eventually cleans up a worker that has gone away.
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
