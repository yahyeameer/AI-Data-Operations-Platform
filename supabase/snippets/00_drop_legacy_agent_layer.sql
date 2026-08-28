-- =============================================================================
-- One-off: remove the hand-built phase-1 agent layer.
--
-- Context: this project's schema was applied ad hoc, never through
-- `supabase db push` -- supabase_migrations.schema_migrations is empty. What
-- landed in the database was an early agent design whose job-kind enum is
-- ('analyze_workbook', 'chat_turn'), while migrations 005-009 in this repo
-- define the real Hermes pipeline (parse_workbook, profile_dataset,
-- propose_cleaning, apply_cleaning, replay_recipe, query_dataset,
-- reconcile_sources, generate_report).
--
-- The two cannot coexist: the enum values differ, agent_workers lacks
-- hostname/capabilities/metadata, agent_jobs lacks dataset_version_id, and the
-- RPC signatures conflict (legacy enqueue_agent_job takes 5 args, the repo's
-- takes 7) -- leaving both would make PostgREST overload resolution ambiguous.
--
-- Safe to run: agent_jobs holds 0 rows and agent_workers holds only a worker's
-- own heartbeat registration, which any worker recreates on its next tick. No
-- foundation table (organizations, workspaces, datasets, raw_uploads,
-- dataset_versions, audit_logs) is touched, and nothing outside the agent layer
-- has a foreign key into it -- dataset_versions.produced_by_run_id is a bare
-- uuid with no constraint.
--
-- Run this ONCE, before applying migrations 005-009.
-- =============================================================================

begin;

drop function if exists enqueue_agent_job(uuid, agent_job_kind, uuid, uuid, jsonb);
drop function if exists claim_agent_job(text, integer);
drop function if exists heartbeat_agent_job(uuid, text, jsonb, integer);
drop function if exists finish_agent_job(uuid, text, boolean, jsonb, text, boolean);
drop function if exists agent_worker_heartbeat(text, text);
drop function if exists cancel_agent_job(uuid);
drop function if exists reap_expired_agent_jobs();

drop table if exists agent_jobs;
drop table if exists agent_workers;

drop type if exists agent_job_kind;
drop type if exists agent_job_status;

commit;
