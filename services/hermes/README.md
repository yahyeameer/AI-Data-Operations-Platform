# Hermes agent

The process that does the data work: reads a messy workbook, profiles it, says
what should be fixed and why, applies what a human approved, answers questions
about the result and writes the month-end report.

It runs on its own host — a Hostinger VPS in the deployment documented below —
and it is designed to sit there for months without attention.

```
  Dashboard (Next.js)            Supabase                   Hermes (this)
  ───────────────────            ────────                   ─────────────
  "Analyse this file"  ──────▶  agent_jobs  ◀───── claims ──── worker loop
                                     │                             │
  polls for status     ◀─────────────┤                        parse / profile
                                     │                        propose / clean
  review queue         ◀──── proposed_changes ◀── writes ─────  query / report
```

## Why a queue instead of an API

The dashboard never calls the agent. It writes a row; the agent picks it up.
Four things follow from that, and each one is a problem that does not need
solving later:

- **The VPS needs no inbound port and no domain.** The agent dials out to
  Supabase over HTTPS and nothing dials in. Close everything except SSH.
- **A restart loses nothing.** Kill the box mid-parse and the job's lease
  expires; the next worker to poll picks it up with the attempt count already
  incremented.
- **Nothing times out.** Parsing a 50 MB workbook takes minutes. A request
  handler cannot wait that long; a status column can.
- **The tenant boundary reaches the agent.** Every job carries `org_id` and
  `workspace_id`, checked by the database before the row exists.

## What it can do

| Job | What happens |
|---|---|
| `parse_workbook` | Finds the real table in a messy sheet — header row, table bounds, subtotal and footnote rows — infers types, writes Parquet, opens a dataset version |
| `profile_dataset` | Column statistics plus the accounting checks: duplicates, name variants, VAT rates, declared-total reconciliation, date coverage |
| `propose_cleaning` | Turns the profile into grouped, materiality-ranked, explained proposals |
| `apply_cleaning` | Applies what a human approved, into a **new** version — nothing is ever overwritten |
| `query_dataset` | A question in English or a structured query → validated SQL → an answer with the source rows behind it |
| `reconcile_sources` | Two versions matched on a key: matched, matched-with-a-difference, unmatched either side |
| `generate_report` | A month-end report in the `exports` bucket |

`parse_workbook` chains the next two itself, so "Analyse" in the dashboard is
one click and three visible stages.

## Where the AI is, and where it is not

The model writes prose and translates questions. It does not compute, and it
does not decide.

- **Never sees a row.** The LLM layer accepts a `Profile` — schema, statistics,
  a handful of frequent values — and there is no code path from a table to a
  prompt. PRD section 8 asks for this as a policy; `hermes/llm/redact.py` makes
  it a structure.
- **Never emits SQL.** For a question it emits a *structured query*, which
  `analyze.compile_query` validates against the real column list before any SQL
  exists. An invented column fails loudly instead of silently returning nothing.
- **Never changes a decision.** Proposals come from the rule engine. The model
  is offered the finished proposal and asked to reword the rationale.

So with no API key configured the agent still does all of its work; the
explanations are just plainer. That is deliberate — a pilot's month-end must not
depend on an API being up.

## Deploying to a Hostinger VPS

The smallest KVM plan is enough. Everything below is copy-paste; the whole thing
takes about ten minutes.

### 1. What you need first

- A Supabase project with the migrations in `supabase/migrations/` applied
  (`supabase db push`).
- Its **Project URL** and **service-role key**, from
  *Project Settings → API* → `service_role`. This key bypasses RLS. Treat it
  like a database password: it is the reason this VPS should run nothing else.
- Optionally an OpenAI or Kimi key.

### 2. Create and secure the server

Order an Ubuntu 24.04 KVM VPS in hPanel. Then:

```bash
ssh root@YOUR_SERVER_IP

apt update && apt upgrade -y

# A non-root user to own the agent.
adduser --disabled-password --gecos "" hermes
usermod -aG sudo hermes

# Only SSH in. The agent needs no inbound port at all.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

# Unattended security updates, because nobody is going to log in for months.
apt install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades
```

If Hostinger's firewall is also enabled in hPanel, leave it closed to
everything except SSH for the same reason.

### 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker hermes
```

### 4. Deploy

```bash
su - hermes
git clone https://github.com/yahyeameer/AI-Data-Operations-Platform.git
cd AI-Data-Operations-Platform/services/hermes

cp .env.example .env
nano .env      # SUPABASE_URL, SUPABASE_SECRET_KEY, optionally a model key

chmod 600 .env

docker compose up -d --build
docker compose logs -f
```

You are looking for:

```
INFO    hermes.worker: hermes 0.2.0 starting as hermes-srv123 on srv123 (models: openai)
```

`restart: unless-stopped` means it comes back after a reboot, a crash, or an
out-of-memory kill. There is nothing else to configure for it to run
continuously.

### 5. Confirm it from the dashboard

Open any client workspace. The strip at the top should read **Agent online**
with the hostname beside it. That indicator is derived from a heartbeat the
worker writes every thirty seconds — after ninety seconds of silence it flips to
offline, so it tells you the truth rather than what it was last told.

Upload a workbook, press **Analyse**, and watch the stages appear.

### Running it without Docker

A 1 GB plan spends a noticeable slice of its memory on the Docker daemon. To
skip it:

```bash
sudo apt install -y python3.12-venv
sudo mkdir -p /opt/hermes /etc/hermes /var/lib/hermes
sudo chown -R hermes:hermes /opt/hermes /var/lib/hermes

su - hermes
cd /opt/hermes
git clone https://github.com/yahyeameer/AI-Data-Operations-Platform.git repo
cp -r repo/services/hermes/hermes repo/services/hermes/requirements.txt /opt/hermes/
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
exit

# Secrets in a root-owned file, not in the unit -- units are world-readable.
sudo cp /opt/hermes/repo/services/hermes/.env.example /etc/hermes/hermes.env
sudo nano /etc/hermes/hermes.env
sudo chmod 600 /etc/hermes/hermes.env
sudo chown root:hermes /etc/hermes/hermes.env

sudo cp /opt/hermes/repo/services/hermes/deploy/hermes.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes
journalctl -u hermes -f
```

### Day-to-day

| | Docker | systemd |
|---|---|---|
| Logs | `docker compose logs -f` | `journalctl -u hermes -f` |
| Restart after editing config | `docker compose restart` | `sudo systemctl restart hermes` |
| Update to a new version | `git pull && docker compose up -d --build` | `git -C repo pull && cp -r repo/services/hermes/hermes . && sudo systemctl restart hermes` |
| Stop | `docker compose down` | `sudo systemctl stop hermes` |

Both stop gracefully: the worker finishes the job in its hands before exiting,
so a deploy in the middle of month-end does not throw away a running parse.

### Running more than one

Start a second host with a different `HERMES_WORKER_ID` and nothing else
changes. `claim_agent_job` uses `for update skip locked`, so workers step over
each other's rows rather than colliding, and they need no knowledge of one
another. Worth doing when several firms close their books on the same day; a
single worker is otherwise ample.

## Troubleshooting

**The dashboard says the agent is offline.**
`docker compose logs --tail 50`. Nearly always a wrong `SUPABASE_URL` or an
expired key — the worker logs the exact HTTP status it got back. Jobs queued in
the meantime are not lost; they run when it reconnects.

**A job says "Legacy .xls files are not supported".**
Correct, and deliberate. The binary format needs a different reader, and
mis-parsing an accounting file is worse than declining it. Re-save as `.xlsx`.

**A job keeps retrying.**
Only transient failures retry. Something that fails three times is usually
storage or the network; the error text on the job says which. Anything the agent
can state as a conclusion — an unsupported file, an unresolved blocker — fails
once and stops.

**The agent runs but proposals read tersely.**
No model key is configured, so the rule engine is writing the explanations. The
findings and the figures are identical either way.

**Out of memory on a 1 GB plan.**
A workbook is held in memory while it is parsed. Either add a swap file
(`fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile`,
then add it to `/etc/fstab`) or move to the 2 GB plan.

## Development

```bash
cd services/hermes
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # .venv/bin/pip on Linux/macOS
.venv/Scripts/python -m hermes
```

With the local Supabase stack running (`npm run db:start` at the repository
root) the agent reads its credentials from `apps/web/.env.local` automatically,
so there is nothing to configure and nothing to keep in sync.

```bash
.venv/Scripts/python -m pytest        # tools, against the messy fixture
npm run test:agent                    # tenancy and queue protocol, from the root
```

## Layout

```
hermes/
  config.py        environment, validated loudly at startup
  supabase.py      PostgREST + Storage over httpx
  worker.py        the 24/7 loop: heartbeat, claim, run, report
  jobs.py          one handler per job kind -- the only place tools meet the database
  tools/
    values.py      accounting-aware coercion: (150.00), £1,240, DD/MM vs MM/DD
    parse.py       messy workbook structure detection
    profile.py     statistics and the accounting checks
    propose.py     the deviation engine: grouped, ranked, explained
    clean.py       pure operations over a dataset version, plus Parquet output
    analyze.py     DuckDB. Structured queries only, never model-authored SQL
    report.py      month-end Markdown
  llm/
    redact.py      the boundary: a Profile goes out, never a row
    router.py      OpenAI / Kimi, interchangeable, always with a fallback
```

A tool never talks to Supabase. A handler never implements a transformation.
That split is what lets the tools be tested against a fixture with no database
at all.
