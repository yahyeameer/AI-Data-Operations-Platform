# Cutover: bring the live database up to the Hermes schema

## Why this is needed

The live Supabase project (`jweclsvkndyvltchnbcl`) was built by hand, not by
`supabase db push`. `supabase_migrations.schema_migrations` is **empty**, and
what actually landed is an early agent design that no longer matches this repo.

Verified state of the live DB as of 2026-08-28:

| | Live DB | This repo |
|---|---|---|
| `agent_job_kind` | `analyze_workbook`, `chat_turn` | 8 kinds (`parse_workbook` … `generate_report`) |
| Agent tables | `agent_jobs`, `agent_workers` (older shape) | + `dataset_profiles`, `proposed_changes`, `analysis_runs` |
| Recipe tables | none | `mapping_tables`, `mapping_entries`, `cleaning_recipes`, `recipe_versions`, `recipe_runs`, `deviations` |
| `enqueue_agent_job` | 5 args | 7 args, different order |
| `agent_workers` cols | id, version, started_at, last_seen_at, jobs_claimed | + hostname, capabilities, metadata |
| `agent_jobs` cols | no `dataset_version_id` | has `dataset_version_id` |

Consequences today:

- Every job kind the Hermes worker handles is rejected by the database at
  enqueue time — the enum does not contain them.
- `apps/web/src/app/app/workspaces/[id]/page.tsx` reads `proposed_changes`,
  which does not exist.
- `apps/web/src/app/api/agent/changes/route.ts` calls `decide_proposed_changes`,
  which does not exist.
- `apps/web/src/components/agent-panel.tsx` enqueues `parse_workbook`, which is
  not a valid enum value.

The foundation layer (migrations 001–004) matches this repo exactly, so all
existing data — 1 org, 3 workspaces, 3 datasets, 11 raw uploads, 3 dataset
versions, 23 audit log entries — is unaffected by this cutover.

## Steps

Run these from the repo root. Steps 1 and 3 prompt for input, so run them
yourself rather than through an agent.

### 1. Link the CLI to the project

```
npx supabase link --project-ref jweclsvkndyvltchnbcl
```

Prompts for the database password (Supabase dashboard → Settings → Database).

### 2. Drop the legacy agent layer

```
npx supabase db execute --file supabase/snippets/00_drop_legacy_agent_layer.sql
```

This removes the phase-1 queue: 7 RPCs, `agent_jobs` (0 rows), `agent_workers`
(1 disposable heartbeat row), and the two conflicting enum types. It touches no
foundation table. Read the file's header comment before running — it explains
each drop.

Any phase-1 worker still pointed at this database stops working here. That is
the intended cutover point; bring up the Hermes worker in step 4.

### 3. Mark 001–004 as already applied, then push 005–009

Because the history table is empty, `db push` would otherwise try to replay the
foundation migrations against objects that already exist.

```
npx supabase migration repair --status applied 20260819000001 20260819000002 20260819000003 20260819000004
npx supabase db push
```

`db push` then applies 005 through 011 in order. Note that 008 adds
`replay_recipe` to `agent_job_kind` with `alter type … add value` and
deliberately does not use the new value in the same transaction, so it is safe
as a single migration. 011 adds `export_dataset` the same way.

Migration 010 is the privilege hardening described at the bottom of this file.
It is ordinary DDL and needs no special handling, but it is the step that closes
the anon-executable RPCs, so do not skip it.

### 4. Regenerate types and start the worker

```
npx supabase gen types typescript --linked > apps/web/src/lib/database.types.ts
```

`export_dataset` was added to the committed `database.types.ts` by hand, because
migration 011 introduced the enum value before there was a database to generate
against. Regenerating should leave that file unchanged — if `git diff` shows the
value disappearing, migration 011 did not apply.

Then start the Hermes worker per `services/hermes/README.md`. It registers
itself in the new `agent_workers` table on its first heartbeat.

### 5. Verify

```
npx tsx scripts/agent-smoke.ts
npx tsx scripts/agent-e2e.ts
```

## Loose ends in the working tree

Two migrations and their code are untracked — commit them before or with this
cutover, or the next clone will not reproduce the schema you just pushed:

- `supabase/migrations/20260820000008_recipes.sql`
- `supabase/migrations/20260820000009_recipe_rpcs.sql`
- `services/hermes/hermes/tools/recipe.py`
- `services/hermes/tests/test_recipe.py`

Modified but uncommitted: `services/hermes/hermes/jobs.py` (+443 lines, the
recipe handlers) and `apps/web/src/lib/database.types.ts` (+621 lines, already
regenerated against the target schema).

## Security advisories

From `get_advisors` against the live project, all WARN level.

**Fixed by migration 010** (applied as part of step 3):

- `create_organization`, `create_workspace`, `has_workspace_access`,
  `is_org_member`, `org_of_workspace`, `org_role_of` and `workspace_of_dataset`
  are `SECURITY DEFINER` and executable by the **anon** role, because Postgres
  grants EXECUTE to PUBLIC by default and migrations 001–004 never revoked it.
  `create_organization` and `create_workspace` being callable without signing in
  is the one that matters — an unauthenticated caller could write an org or
  workspace row. Migrations 006, 007 and 009 already got this right, so the drift
  is confined to the foundation layer.
- `reject_mutation`, `raw_uploads_guard` and `try_uuid` have a mutable
  `search_path`. All three are pure logic with no object references, so pinning
  them to `''` is free.

This exposure is live until step 3 runs. It could not be fixed ahead of the
cutover from an agent session — writes to the database are gated — so it ships
with the push.

**Still outstanding, dashboard only:**

- Leaked-password protection is disabled in Auth (Authentication → Policies).
  Not fixable from a migration.
