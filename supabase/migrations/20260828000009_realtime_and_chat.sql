-- =============================================================================
-- Realtime on the job row, and a job kind for a chat turn.
--
-- Two changes that belong together, because both exist to stop the browser
-- waiting on anything.
--
-- 1. The dashboard polled /api/analyze/:id every two seconds. The row is
--    already the source of truth and Postgres already knows the moment it
--    changes, so publishing it is strictly better than asking repeatedly: the
--    answer arrives when it happens rather than up to two seconds later, and an
--    idle dashboard stops generating requests.
--
--    RLS still governs it. Realtime evaluates `agent_jobs_select_members` per
--    subscriber, so a client cannot subscribe its way around the tenant
--    boundary -- the same policy that decides what a SELECT returns decides
--    what a subscription delivers.
--
-- 2. A chat turn becomes a queued job like any other. It was the last thing
--    still running inside a request, and it is the *worst* candidate for that:
--    a multi-round tool loop against a free-tier model has no bounded duration
--    at all. Queueing it means a slow model delays an answer instead of
--    tripping the platform's function cap.
-- =============================================================================

-- A chat turn: message in, reply out, with whatever tool calls the model made
-- along the way recorded on the job.
alter type agent_job_kind add value if not exists 'chat_turn';

-- -----------------------------------------------------------------------------
-- Publish agent_jobs for Realtime.
--
-- `add table` errors if the table is already published, and this migration must
-- be re-runnable against a database where it is -- so check first.
-- -----------------------------------------------------------------------------

do $$
begin
  if not exists (
    select 1
    from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'agent_jobs'
  ) then
    alter publication supabase_realtime add table agent_jobs;
  end if;
end
$$;

-- Realtime sends the changed columns of the new row. Without a replica identity
-- an UPDATE carries only the primary key plus the columns that changed, which
-- is fine for a status flip but drops `result` from the payload when the worker
-- writes status and result in one statement. FULL makes the whole row travel,
-- so a subscriber gets the findings in the same event that tells it the job
-- succeeded rather than having to turn round and fetch them.
alter table agent_jobs replica identity full;
