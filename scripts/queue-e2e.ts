/**
 * The seam, end to end.
 *
 * Proves the two halves are actually joined up: it seeds a firm, puts the messy
 * fixture into storage, queues an analysis through the same RPC the dashboard
 * calls, and then waits for a worker to claim it and write findings back.
 *
 * With no worker running it checks everything up to the hand-off and reports the
 * rest as skipped rather than passed -- a green suite that only ever tested one
 * half is worse than a red one.
 *
 * Usage:
 *   terminal 1: cd services/parser && .venv/Scripts/python -m app.worker
 *   terminal 2: npm run test:queue:e2e
 */

import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { config as loadEnv } from 'dotenv';
import { createClient } from '@supabase/supabase-js';

loadEnv({ path: 'apps/web/.env.local', quiet: true });

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const SECRET_KEY = process.env.SUPABASE_SECRET_KEY!;
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!;
const FIXTURE = 'fixtures/messy/acme-sales-2026-08.xlsx';
const WAIT_SECONDS = Number(process.env.E2E_WAIT_SECONDS ?? 90);

const admin = createClient(SUPABASE_URL, SECRET_KEY, {
  auth: { persistSession: false, autoRefreshToken: false },
});

let passed = 0;
const failures: string[] = [];

function check(name: string, condition: boolean, detail = '') {
  if (condition) {
    passed += 1;
    console.log(`  PASS  ${name}`);
  } else {
    failures.push(`${name}${detail ? ` -- ${detail}` : ''}`);
    console.log(`  FAIL  ${name}${detail ? ` -- ${detail}` : ''}`);
  }
}

async function main() {
  console.log('\nQueue end to end\n');

  // -- a firm, a workspace, a stored file -----------------------------------

  const email = `e2e-${randomUUID().slice(0, 8)}@example.test`;
  const password = `pw-${randomUUID()}`;
  const { data: created } = await admin.auth.admin.createUser({
    email,
    password,
    email_confirm: true,
  });

  const user = createClient(SUPABASE_URL, PUBLISHABLE_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  await user.auth.signInWithPassword({ email, password });

  const { data: org } = await user.rpc('create_organization', {
    p_name: 'E2E Firm',
    p_slug: `e2e-${randomUUID().slice(0, 6)}`,
  });
  const { data: workspace } = await user.rpc('create_workspace', {
    p_org_id: org.id,
    p_name: 'E2E client',
  });
  const { data: dataset } = await admin
    .from('datasets')
    .insert({ workspace_id: workspace.id, name: 'Monthly sales', created_by: created.user!.id })
    .select('id')
    .single();

  const uploadId = randomUUID();
  const storagePath = `${org.id}/${workspace.id}/2026-08/${uploadId}__acme-sales.xlsx`;
  const bytes = readFileSync(FIXTURE);

  const { error: storageError } = await admin.storage
    .from('raw')
    .upload(storagePath, bytes, {
      contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      upsert: true,
    });
  check('the fixture lands in storage', !storageError, storageError?.message);

  await admin.from('raw_uploads').insert({
    id: uploadId,
    workspace_id: workspace.id,
    dataset_id: dataset!.id,
    storage_path: storagePath,
    original_filename: 'acme-sales-2026-08.xlsx',
    mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    byte_size: bytes.byteLength,
    status: 'stored',
    uploaded_by: created.user!.id,
  });

  // -- the hand-off ---------------------------------------------------------

  const queuedAt = Date.now();
  const { data: job, error: enqueueError } = await user.rpc('enqueue_agent_job', {
    p_workspace_id: workspace.id,
    p_kind: 'analyze_workbook',
    p_raw_upload_id: uploadId,
    p_dataset_id: dataset!.id,
  });
  check('the dashboard can queue the analysis', !enqueueError && Boolean(job?.id), enqueueError?.message);

  // The property that matters: enqueuing does not wait for the engine. If this
  // is slow, the queue is not doing its job.
  const enqueueMs = Date.now() - queuedAt;
  check(`queueing returns immediately (${enqueueMs}ms)`, enqueueMs < 3000, `took ${enqueueMs}ms`);

  if (!job?.id) {
    console.log('\nCannot continue without a queued job.\n');
    process.exit(1);
  }

  // -- the worker -----------------------------------------------------------

  const { data: workers } = await admin
    .from('agent_workers')
    .select('id, last_seen_at')
    .gte('last_seen_at', new Date(Date.now() - 90_000).toISOString());

  if (!workers || workers.length === 0) {
    console.log('\n  SKIP  no worker is running -- start one to test the other half:');
    console.log('        cd services/parser && .venv/Scripts/python -m app.worker\n');
    console.log(`${passed} passed, ${failures.length} failed, rest skipped\n`);
    process.exit(failures.length ? 1 : 0);
  }

  console.log(`  ..    waiting for worker ${workers[0].id} (up to ${WAIT_SECONDS}s)`);

  const deadline = Date.now() + WAIT_SECONDS * 1000;
  let finished: Record<string, unknown> | null = null;

  while (Date.now() < deadline) {
    const { data: current } = await admin
      .from('agent_jobs')
      .select('status, result, error, progress, attempts')
      .eq('id', job.id)
      .single();

    if (current && ['succeeded', 'failed', 'cancelled'].includes(current.status as string)) {
      finished = current;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }

  check('the worker finished the job', Boolean(finished), `still ${'running'} after ${WAIT_SECONDS}s`);
  if (!finished) {
    console.log(`\n${passed} passed, ${failures.length} failed\n`);
    process.exit(1);
  }

  check('it succeeded', finished.status === 'succeeded', String(finished.error ?? ''));

  const result = finished.result as {
    rows?: number;
    blocked?: boolean;
    findings?: Array<{ tier: string; title: string }>;
    summary?: { block?: number; review?: number };
  } | null;

  check('it read the 9 transaction rows', result?.rows === 9, `got ${result?.rows}`);
  check('it excluded the header and total rows', (result as never as { excluded_rows: number })?.excluded_rows > 0);

  // The finding the whole product is justified by: the file's own TOTAL row
  // disagrees with its transaction rows, and the run should stop.
  check('the unreconciled total blocks the run', result?.blocked === true);
  check(
    'it is reported as a blocking finding',
    (result?.findings ?? []).some((f) => f.tier === 'block' && /reconcile/i.test(f.title)),
  );

  check(
    'the duplicate row is found',
    (result?.findings ?? []).some((f) => /duplicate/i.test(f.title)),
  );
  check(
    'the supplier spelling merge is reported',
    (result?.findings ?? []).some((f) => /spelling/i.test(f.title)),
  );

  console.log('\n  findings:');
  for (const finding of result?.findings ?? []) {
    console.log(`    [${finding.tier}] ${finding.title}`);
  }

  console.log(`\n${passed} passed, ${failures.length} failed\n`);
  if (failures.length) {
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
