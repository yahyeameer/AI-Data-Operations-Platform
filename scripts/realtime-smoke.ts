/**
 * Realtime delivery, and the tenant boundary on it.
 *
 * The dashboard subscribes to its job row instead of polling. Two things have to
 * be true for that to be safe as well as useful:
 *
 *   1. The owner receives the update, carrying the whole row. `replica identity
 *      full` is what puts `result` in the payload; without it an update sends
 *      only the changed columns plus the key, and the findings would arrive
 *      empty while the status said succeeded.
 *
 *   2. Nobody else receives it. Realtime evaluates the same RLS policy that
 *      governs a SELECT, so `agent_jobs_select_members` is the boundary -- but
 *      a policy that is never exercised over the socket is a policy nobody has
 *      checked. This asserts the negative case explicitly, because a leak here
 *      would be a live feed of another firm's work.
 *
 * Usage: npm run test:realtime   (requires `supabase start`)
 */

import { randomUUID } from 'node:crypto';
import { config as loadEnv } from 'dotenv';
import { createClient, type SupabaseClient } from '@supabase/supabase-js';

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

async function signedInClient(label: string) {
  const email = `${label}-${randomUUID().slice(0, 8)}@example.test`;
  const password = `pw-${randomUUID()}`;
  const { data: created } = await admin.auth.admin.createUser({
    email,
    password,
    email_confirm: true,
  });
  const client = createClient(SUPABASE_URL, PUBLISHABLE_KEY, { auth: { persistSession: false } });
  const { data: session } = await client.auth.signInWithPassword({ email, password });
  // Realtime authorises from the socket's access token, not the REST session,
  // so it has to be handed over explicitly or the subscription joins as anon
  // and every RLS check fails closed.
  await client.realtime.setAuth(session.session!.access_token);
  return { client, userId: created.user!.id };
}

async function seed(client: SupabaseClient, userId: string, name: string) {
  const { data: org } = await client.rpc('create_organization', {
    p_name: name,
    p_slug: `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${randomUUID().slice(0, 6)}`,
  });
  const { data: workspace } = await client.rpc('create_workspace', {
    p_org_id: org.id,
    p_name: `${name} client`,
  });
  const uploadId = randomUUID();
  await admin.from('raw_uploads').insert({
    id: uploadId,
    workspace_id: workspace.id,
    storage_path: `${org.id}/${workspace.id}/2026-08/${uploadId}__x.xlsx`,
    original_filename: 'x.xlsx',
    mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    byte_size: 10,
    status: 'stored',
    uploaded_by: userId,
  });
  return { workspaceId: workspace.id, uploadId };
}

/**
 * Subscribe, and resolve on the first UPDATE matching `wanted` (or time out).
 *
 * A job emits several updates on its way through the queue -- the claim flips it
 * to 'running' before the worker has done anything. Resolving on the first event
 * would test that a claim is delivered, which is not the interesting claim; the
 * dashboard cares about the one carrying the answer.
 */
function waitForUpdate(
  client: SupabaseClient,
  jobId: string,
  label: string,
  ms: number,
  wanted: (row: Record<string, unknown>) => boolean = () => true,
): { ready: Promise<void>; event: Promise<Record<string, unknown> | null> } {
  let markReady: () => void;
  // Joining a channel is itself asynchronous. Sleeping a fixed interval and
  // hoping it finished makes this test fail intermittently for reasons that have
  // nothing to do with the code under test, so wait for the real SUBSCRIBED
  // callback instead.
  const ready = new Promise<void>((resolve) => {
    markReady = resolve;
  });

  const event = new Promise<Record<string, unknown> | null>((resolve) => {
    const timer = setTimeout(() => {
      client.removeChannel(channel);
      resolve(null);
    }, ms);

    const channel = client
      .channel(`${label}:${jobId}`)
      .on(
        'postgres_changes',
        { event: 'UPDATE', schema: 'public', table: 'agent_jobs', filter: `id=eq.${jobId}` },
        (payload) => {
          const row = payload.new as Record<string, unknown>;
          if (!wanted(row)) return;
          clearTimeout(timer);
          client.removeChannel(channel);
          resolve(row);
        },
      )
      .subscribe((status) => {
        // CHANNEL_ERROR and TIMED_OUT also release the waiter -- a test that
        // hangs for ever on a failed join tells you less than one that proceeds
        // and reports no event received.
        if (['SUBSCRIBED', 'CHANNEL_ERROR', 'TIMED_OUT', 'CLOSED'].includes(status)) markReady();
      });
  });

  return { ready, event };
}

const TERMINAL = ['succeeded', 'failed', 'cancelled'];

/**
 * Empty the queue first.
 *
 * `claim_agent_job` takes the oldest job anywhere, by design -- a worker is not
 * tenant-scoped. So a job left behind by an earlier run gets claimed instead of
 * this test's, `finish_agent_job` then refuses to finish a job this worker does
 * not hold, no update happens, and the test reports "no realtime event" for a
 * subscription that was working perfectly.
 *
 * Guarded to a local stack, because this deletes rows and no test should be able
 * to do that to a real deployment by way of a mistyped env file.
 */
async function clearQueue() {
  if (!/127\.0\.0\.1|localhost/.test(SUPABASE_URL)) {
    console.error(`Refusing to clear the queue on a non-local stack (${SUPABASE_URL}).`);
    process.exit(1);
  }
  const { error } = await admin
    .from('agent_jobs')
    .delete()
    .in('status', ['queued', 'running', 'succeeded', 'failed', 'cancelled']);
  if (error) throw new Error(`Could not clear the queue: ${error.message}`);
}

async function main() {
  console.log('\nRealtime\n');

  await clearQueue();

  const owner = await signedInClient('owner');
  const stranger = await signedInClient('stranger');
  const tenant = await seed(owner.client, owner.userId, 'Owner Firm');

  const { data: job } = await owner.client.rpc('enqueue_agent_job', {
    p_workspace_id: tenant.workspaceId,
    p_kind: 'analyze_workbook',
    p_raw_upload_id: tenant.uploadId,
  });
  check('a job to watch was queued', Boolean(job?.id));
  if (!job?.id) process.exit(1);

  // Both listen. Only one should hear anything.
  const ownerWatch = waitForUpdate(owner.client, job.id, 'owner', 12_000, (row) =>
    TERMINAL.includes(String(row.status)),
  );
  // No predicate for the stranger: *any* event reaching them is a leak, and
  // filtering would hide the very thing this is looking for.
  const strangerWatch = waitForUpdate(stranger.client, job.id, 'stranger', 12_000);

  // Both sockets must be joined before the row changes. An update that fires
  // before either is listening proves nothing in either direction -- neither
  // that delivery works, nor that the stranger is properly excluded.
  await Promise.all([ownerWatch.ready, strangerWatch.ready]);
  const ownerHeard = ownerWatch.event;
  const strangerHeard = strangerWatch.event;

  const workerId = `rt-${randomUUID().slice(0, 6)}`;
  await admin.rpc('agent_worker_heartbeat', { p_worker_id: workerId, p_version: 'test' });
  await admin.rpc('claim_agent_job', { p_worker_id: workerId, p_lease_seconds: 300 });
  await admin.rpc('finish_agent_job', {
    p_job_id: job.id,
    p_worker_id: workerId,
    p_success: true,
    p_result: { rows: 9, findings: [{ tier: 'block', title: 'Totals do not reconcile' }] },
  });

  const heard = await ownerHeard;
  check('the owner receives the update over the socket', Boolean(heard));
  check('it reports the terminal status', heard?.status === 'succeeded', String(heard?.status));

  // The point of `replica identity full`: the findings ride along with the
  // status rather than requiring a follow-up fetch.
  const result = heard?.result as { rows?: number; findings?: unknown[] } | null;
  check('the payload carries the whole row, not just changed columns', Boolean(result));
  check('the findings arrive in the same event', (result?.findings ?? []).length === 1);

  const leaked = await strangerHeard;
  check('another firm receives nothing', leaked === null, leaked ? 'RLS LEAK OVER REALTIME' : '');

  // See queue-smoke: a leftover heartbeat makes `test:cold-start` skip.
  await admin.from('agent_workers').delete().eq('id', workerId);

  console.log(`\n${passed} passed, ${failures.length} failed\n`);
  if (failures.length) {
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exit(1);
  }
  process.exit(0);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
