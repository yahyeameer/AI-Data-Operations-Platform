# AI Data Operations Platform

A workflow-learning data-operations copilot for accounting practices. The product learns a
client's recurring data workflow once, turns it into a versioned executable recipe, and from then
on surfaces only the exceptions. See [`AI_Data_Operations_PRD_v2.md`](./AI_Data_Operations_PRD_v2.md)
for the full specification.

**Current state: Phase 2 — the agent works end to end.** A firm can sign up, create client
workspaces and upload a real messy workbook. The Hermes agent then reads it, profiles it, and
returns a grouped, materiality-ranked list of what should be fixed and why; the accountant
approves or rejects; approved changes are applied into a new immutable version. Questions and
month-end reports run off that version, and every figure traces back to its source rows.

Recipes — capturing an approved session and replaying it against next month's file (PRD section 4)
— are the next thing to build, and they are what turn this from a good cleaning tool into the
product the PRD describes.

## Layout

```
apps/web/           Next.js 16 App Router application
services/hermes/    the agent: parser, profiler, deviation engine, analytics
supabase/           config + SQL migrations
scripts/            test suites and fixture generation
fixtures/messy/     deliberately messy workbooks (PRD section 6)
```

## How the agent connects

The dashboard never calls the agent. It writes a row to `agent_jobs`; the agent, running wherever
you put it, claims the row and writes the result back.

```
  Dashboard                     Supabase                    Hermes agent
  ─────────                     ────────                    ────────────
  "Analyse this file"  ──────▶  agent_jobs  ◀──── claims ───── worker loop
  polls for status     ◀────────────┼───────── writes ────────▶ results
  review queue         ◀──── proposed_changes                  (any host)
```

That indirection is what makes the agent host disposable. It needs no inbound port, no domain and
no TLS certificate; it dials out to Supabase and nothing dials in. A reboot or a redeploy delays
work rather than losing it, because a claimed job whose lease expires simply becomes claimable
again.

See [`services/hermes/README.md`](./services/hermes/README.md) for what it does and how to run it
24/7 on a VPS.

## Running it

Requires Node 22+, Docker Desktop, and Python 3 (for the fixture generator only).

```bash
npm install                 # root tooling
npm --prefix apps/web install

npm run db:start            # local Supabase stack, applies all migrations
npm run db:seed             # demo firm + two client workspaces to sign into
npm run dev                 # http://127.0.0.1:3100
```

Sign in as `demo@example.test` / `demo-password-123`, or create your own account at `/signup`
(local email confirmation is off, so signup is immediate). Re-run `npm run db:seed` after any
`npm run db:reset` to put the demo account back — it refuses to run against anything other than a
local stack.

`npm run db:start` prints the local keys. They are already in `apps/web/.env.local`; if you reset
the stack and they change, copy them across from that output.

The local stack runs on ports `544xx` rather than the Supabase defaults, so it can coexist with
another project's stack on the same machine.

## Tests

```bash
npm run test:isolation      # cross-tenant isolation + append-only guarantees
npm run test:agent          # agent tenancy, worker privilege, queue protocol
npm run test:e2e            # full upload flow against the running dev server
npm run test:agent:e2e      # the agent seam over real HTTP, worker included

cd services/hermes && .venv/Scripts/python -m pytest    # the agent's own tools
```

`test:isolation` and `test:agent` are the two that matter most. Two accounting firms sharing one
database is the entire risk model of this product (PRD section 13), and between them these suites
are what prove the separation holds. They also verify the append-only triggers using the
service-role key — the most privileged client in the system. **If either goes red, nothing else
about a release matters.**

`test:agent` covers the surface the agent added. The agent holds the service-role key, so a job
accepted across the tenant boundary would also be *executed* across it — the suite proves a user
cannot queue work against another firm's data, that the worker-side RPCs are unreachable from a
browser session, and that the queue itself claims each job exactly once and recovers a job whose
worker died.

`test:e2e` needs the dev server running. It drives the real HTTP routes rather than the database,
so it covers the session handling, the authorization checks in the route handlers and the signed
upload URL.

`test:agent:e2e` is the one that proves the two halves are actually joined up. It signs up a firm,
uploads the messy fixture through the real routes, asks the agent to analyse it, and waits — so it
fails if the dashboard, the queue or the worker stop agreeing with each other. With no worker
running it checks everything up to the hand-off and reports the rest as skipped rather than passed.

The agent's pytest suite runs against `fixtures/messy/acme-sales-2026-08.xlsx` with no database at
all, and is the start of the eval harness PRD section 8 asks for in week 2 rather than week 8.

## What the agent actually does with a messy file

Run `npm run fixtures` and upload the result. Against that file — header on row 5, merged title
block, an embedded subtotal, a trailing TOTAL, footnotes, a second sheet, parenthesised negatives,
a currency symbol, thousands separators, one duplicated invoice, two suppliers each spelled two
ways, and one date that is ambiguous between DD/MM and MM/DD — the agent produces this queue:

| Tier | Finding | Affected | Value |
|---|---|---|---|
| **Blocks the run** | Totals do not reconcile in 2 columns | — | £0.25 |
| Needs review | 1 date falls outside 2026-08 and may be month-first | 1 row | £2,015.75 |
| Needs review | Remove 1 exact duplicate row | 1 row | £1,200.00 |
| Needs review | Merge 2 spelling groups in supplier | 2 rows | £1,030.50 |
| Routine | Read Net Sales as a number | 9 rows | — |
| Routine | Read VAT as a number | 9 rows | — |
| Routine | Normalise Date to ISO dates | 9 rows | — |

Two of those are worth dwelling on, because they are the difference between a cleaning script and
something an accountant would trust.

**The blocking item is real.** The fixture's own TOTAL row claims £10,361.35; its transaction rows
add up to £10,361.10. The agent reconciles what it computed against what the file declares about
itself and refuses to let the run proceed until a human resolves the 25p. Section 5.3 calls this
the difference between an automation tool and a liability.

**The date finding needs two signals at once.** Every date in the column could be read either way,
so the parser reads them all day-first and records that the reading was ambiguous. That puts eight
rows in August and one in March. Neither fact is suspicious alone; together they say the March row
is almost certainly 3 August misread. Nothing about that row looks wrong on inspection.

Approving everything except the blocker takes the dataset from 9 rows to 8, merges the supplier
spellings, and writes it all as version 1 — with version 0 left exactly as it was.

## What Phase 1 built



| Area | Detail |
|---|---|
| **Tenancy** | Organizations (firms) → client workspaces. Workspaces are the unit the pricing model meters (section 14). |
| **Isolation** | Supabase RLS *plus* independent server-side authorization on every path (section 13). Neither is trusted alone. |
| **Immutability** | `dataset_versions` and `audit_logs` are append-only, enforced by database triggers rather than application code. `raw_uploads` permits exactly one `pending → stored` transition and nothing else. |
| **Storage** | Private `raw` / `parquet` / `exports` buckets, keyed `{org_id}/{workspace_id}/{YYYY-MM}/{upload_id}__{filename}`. |
| **Uploads** | Browser → storage directly via a signed upload URL; the Next.js server never handles the bytes. |
| **Audit** | Every action written in the same transaction as the change that caused it. |

### Design decisions worth knowing

**Writes go through `SECURITY DEFINER` RPCs, not direct inserts.** `create_organization` and
`create_workspace` write the entity and its audit row in one transaction. If application code did
that in two statements, any crash or early return between them would leave an action with no audit
record — and section 13 asks for an immutable trail, not a mostly-complete one.

**Authenticated users hold `SELECT` and nothing else.** There is no `INSERT`/`UPDATE`/`DELETE`
policy on any table, and absence of a policy is a deny. A stolen publishable key reads nothing it
should not and writes nothing at all.

**The service role is fenced.** It bypasses RLS, so `adminFor()` in `apps/web/src/lib/authz.ts`
takes an already-proven access context as its argument. The ordering — check first, construct the
privileged client second — is then visible at every call site instead of relying on memory.

**Version 0 exists from the first upload.** Cleaning never mutates; it writes a new version with a
parent pointer (section 3). Week 2's Parquet output already has a v0 to descend from.

## Next: recipes

Everything above happens fresh each month. The PRD's actual product (section 4, and MVP criteria 6
and 9) is that it should not have to:

1. **Capture the approved session as a recipe.** The decisions an accountant just made are already
   stored as `proposed_changes` rows with an `operation` each — a recipe is that ordered list,
   versioned, with the invariants the run should be checked against.
2. **Match next month's upload to it.** `datasets.source_signature` is already written at parse
   time from the column names, types and header position, so the lookup exists; what is missing is
   replaying the recipe and reporting only where the new file deviates.
3. **Write mapping decisions back.** When someone merges "CONTOSO LIMITED" into "Contoso Ltd.",
   that should become a workspace-level mapping table entry so the same question is never asked
   twice. This is the mechanism that takes automation from ~85% to ~99%.

Criteria 6 and 9 are the product. Everything built so far is the machinery they need.
