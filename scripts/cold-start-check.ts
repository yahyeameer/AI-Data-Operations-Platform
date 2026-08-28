/**
 * The regression test for the bug this phase exists to fix.
 *
 * Before the queue, asking for an analysis while the engine was asleep produced
 * "The analysis is taking longer than this plan allows. The engine may be waking
 * up -- try again in a moment." The request went browser -> Vercel function ->
 * Render, and Vercel's 60-second cap expired during the cold start, before any
 * of our own code ran.
 *
 * The fix is that nothing waits any more. This script proves it by doing the
 * thing that used to fail: it queues real work with **no worker running at all**
 * and asserts that the request succeeds, returns immediately, and leaves the job
 * sitting safely in the queue rather than erroring.
 *
 * Run it with the worker stopped. If it ever goes red, something has put a
 * synchronous call back on the request path.
 *
 * Usage: npm run test:cold-start   (with no worker running)
 */

import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { config as loadEnv } from 'dotenv';
import { createClient } from '@supabase/supabase-js';

loadEnv({ path: 'apps/web/.env.local', quiet: true });

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const SECRET_KEY = process.env.SUPABASE_SECRET_KEY!;
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!;

const admin = createClient(SUPABASE_URL, SECRET_KEY, { auth: { persistSession: false } });

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
  console.log('\nCold start: queueing while the engine is down\n');

  const { data: workers } = await admin.from('agent_workers').select('id, last_seen_at');
  const awake = (workers ?? []).filter(
    (w) => Date.now() - new Date(w.last_seen_at).getTime() < 90_000,
  );

  if (awake.length > 0) {
    console.log(`  SKIP  a worker is running (${awake.map((w) => w.id).join(', ')}).`);
    console.log('        Stop it and re-run -- this test is about the engine being down.\n');
    process.exit(0);
  }

  const email = `cold-${randomUUID().slice(0, 8)}@example.test`;
  const password = `pw-${randomUUID()}`;
  const { data: created } = await admin.auth.admin.createUser({
    email,
    password,
    email_confirm: true,
  });

  const user = createClient(SUPABASE_URL, PUBLISHABLE_KEY, { auth: { persistSession: false } });
  await user.auth.signInWithPassword({ email, password });

  const { data: org } = await user.rpc('create_organization', {
    p_name: 'Cold Start Firm',
    p_slug: `cold-${randomUUID().slice(0, 6)}`,
  });
  const { data: workspace } = await user.rpc('create_workspace', {
    p_org_id: org.id,
    p_name: 'Cold client',
  });
  const { data: dataset } = await admin
    .from('datasets')
    .insert({ workspace_id: workspace.id, name: 'Sales', created_by: created.user!.id })
    .select('id')
    .single();

  const uploadId = randomUUID();
  const storagePath = `${org.id}/${workspace.id}/2026-08/${uploadId}__acme.xlsx`;
  const bytes = readFileSync('fixtures/messy/acme-sales-2026-08.xlsx');

  await admin.storage.from('raw').upload(storagePath, bytes, {
    contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    upsert: true,
  });
  await admin.from('raw_uploads').insert({
    id: uploadId,
    workspace_id: workspace.id,
    dataset_id: dataset!.id,
    storage_path: storagePath,
    original_filename: 'acme.xlsx',
    mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    byte_size: bytes.byteLength,
    status: 'stored',
    uploaded_by: created.user!.id,
  });

  // The moment that used to fail.
  const started = Date.now();
  const { data: job, error } = await user.rpc('enqueue_agent_job', {
    p_workspace_id: workspace.id,
    p_kind: 'analyze_workbook',
    p_raw_upload_id: uploadId,
    p_dataset_id: dataset!.id,
  });
  const elapsed = Date.now() - started;

  check('asking for an analysis with the engine down succeeds', !error, error?.message);
  check(`it returns immediately (${elapsed}ms, was a 60s timeout)`, elapsed < 3000, `${elapsed}ms`);
  check('a job id comes back to poll', Boolean(job?.id));

  if (!job?.id) {
    console.log(`\n${passed} passed, ${failures.length} failed\n`);
    process.exit(1);
  }

  // The job must wait, not fail. This is the difference between "the answer is
  // coming" and the error message the user was seeing.
  await new Promise((resolve) => setTimeout(resolve, 4000));
  const { data: after } = await admin
    .from('agent_jobs')
    .select('status, error')
    .eq('id', job.id)
    .single();

  check('the job waits in the queue rather than failing', after?.status === 'queued', `status=${after?.status}`);
  check('no error is recorded against it', !after?.error, String(after?.error ?? ''));

  console.log(
    '\n  The work is queued and will run whenever the engine next wakes.\n' +
      '  Start the worker and `npm run test:queue:e2e` to watch it complete.\n',
  );

  console.log(`${passed} passed, ${failures.length} failed\n`);
  if (failures.length) {
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
