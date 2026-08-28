# Running the analysis worker on the Hostinger VPS

The worker is the process that replaced the synchronous parser call. This is how
to run it, and why it is shaped the way it is.

## The architecture

```
                         👤 Accountant
                              │
                              ▼
                    ┌───────────────────┐
                    │   Vercel          │
                    │   Next.js UI      │
                    └─────────┬─────────┘
                              │  writes a job row, returns in ~30ms
                              ▼
       ┌────────────────────────────────────────┐
       │              Supabase                  │
       │   Auth · Postgres · Storage            │
       │   agent_jobs  ← the whole interface    │
       └────────────────────────────────────────┘
                              ▲
                              │  claims jobs, writes results
                              │  (outbound only)
                    ┌─────────┴─────────┐
                    │  Hostinger VPS    │
                    │  worker           │
                    │  Polars · DuckDB  │
                    └───────────────────┘
```

Three hosts, no fourth. **The dashboard never calls the VPS.** It writes a row to
`agent_jobs`; the worker claims it and writes the answer back.

That indirection is the entire fix for *"The analysis is taking longer than this
plan allows."* That error came from the browser waiting on a Vercel function
(60s cap on Hobby, set in `api/hermes/chat/route.ts`) which was itself waiting on
a sleeping host to cold-start. Nothing waits on the VPS now, so neither clock
exists. A worker that is rebooting, redeploying or simply slow **delays** an
analysis; it cannot fail one.

## Why the worker pulls instead of being pushed

An earlier sketch had Next.js POST the job to Hermes with a shared
`HERMES_API_SECRET`. Pulling is strictly better here, and it removes work rather
than adding it:

| | push (dashboard → VPS) | pull (VPS → Supabase) |
|---|---|---|
| Inbound port | needs one | none |
| Domain + TLS cert | needs both | neither |
| VPS rebooting | the request fails | the job waits |
| Shared secret | `HERMES_API_SECRET` to manage and rotate | none — the Supabase key is the only credential |
| Two workers | needs a load balancer | `for update skip locked`, no coordination |

The `HERMES_API_SECRET` is not rotated in this design, it is **deleted**. There
is no inbound call left for it to authenticate.

## Install

```bash
# On the VPS, once:
cd /opt/AI-Data-Operations-Platform/services/parser
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Credentials — the worker's only secret is the Supabase service key.
cat >> .env <<'ENV'
SUPABASE_URL=https://<your-project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=sb_secret_...
WORKER_ID=hostinger-1
ENV

sudo cp ../../deploy/analyzit-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now analyzit-worker

systemctl status analyzit-worker
journalctl -u analyzit-worker -f
```

A healthy log looks like:

```
INFO    worker: worker hostinger-1 starting (version phase-1)
INFO    worker: job 60e2f254-…: analyze_workbook (attempt 1)
INFO    worker: job 60e2f254-…: done in 2349ms
```

Prefer Docker? `Dockerfile.worker` builds the same thing:
`docker run -d --env-file .env --restart unless-stopped analyzit-worker`.

## Checking it from your laptop

You do not need to SSH in to know whether it is up. The worker heartbeats into
`agent_workers` every 30 seconds, and the dashboard treats three missed beats as
offline — which is the only liveness signal that works when the dashboard cannot
reach the host.

```sql
select id, version, last_seen_at, jobs_claimed from agent_workers;
select status, count(*) from agent_jobs group by status;
```

## Scaling

Run the unit on a second box with a different `WORKER_ID`. `claim_agent_job` uses
`for update skip locked`, so two workers cooperate with no coordination and no
load balancer — concurrency that lives in the database is concurrency you can
reason about from a SQL prompt.

## Tuning

| Variable | Default | Raise it when |
|---|---|---|
| `WORKER_LEASE_SECONDS` | 300 | an analysis legitimately takes longer than 5 min |
| `WORKER_POLL_SECONDS` | 5 | you want less idle chatter against Supabase |
| `WORKER_MAX_DOWNLOAD_BYTES` | 50 MB | workbooks exceed the upload limit |

The lease is the crash-recovery window: if a worker dies holding a job, that job
becomes claimable again once the lease lapses. Setting it far higher than a real
analysis takes just makes recovery slower.

## A note on live updates

The dashboard currently polls `/api/analyze/:id` every 2 seconds. Supabase
Realtime on `agent_jobs` would push instead, and is a natural fit — the row is
already the source of truth. It is worth doing, but it is a change to how the UI
learns the answer, not to whether the answer arrives, so it was left out of this
phase deliberately.
