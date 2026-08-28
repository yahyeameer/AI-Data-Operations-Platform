/**
 * The queue protocol, proved.
 *
 * The worker holds the service-role key, so a job accepted across the tenant
 * boundary would also be *executed* across it. These checks prove a user cannot
 * queue work against another firm's data, that the worker-side RPCs are
 * unreachable from a browser session, and that the queue itself claims each job
 * exactly once and recovers one whose worker died.
 *
 * The last two checks are the ones worth reading. A job whose worker dies is
 * recovered by its lease expiring -- but only while it has attempts left. Once
 * they are spent the row would sit at 'running' for ever: never claimed again,
 * never failed, and never re-queueable because the enqueue dedup matches
 * 'running'. That is a dataset the user can never analyse again, behind a
 * spinner that never resolves. `reap_expired_agent_jobs` is what closes it, and
 * the test named "exhausted job is reaped" is what proves it stays closed.
 *
 * Usage: npm run test:queue   (requires `supabase start`)
 */

import { randomUUID } from 'node:crypto';
import { config as loadEnv } from 'dotenv';
import { createClient, type SupabaseClient } from '@supabase/supabase-js';

loadEnv({ path: 'apps/web/.env.local', quiet: true });

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL;
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
const SECRET_KEY = process.env.SUPABASE_SECRET_KEY;

if (!SUPABASE_URL || !PUBLISHABLE_KEY || !SECRET_KEY) {
  console.error('Missing Supabase env. Run `supabase start` and fill apps/web/.env.local.');
  process.exit(1);
}

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

async function createUserClient(label: string) {
  const email = `${label}-${randomUUID().slice(0, 8)}@example.test`;
  const password = `pw-${randomUUID()}`;

  const { data: created, error: createError } = await admin.auth.admin.createUser({
    email,
    password,
    email_confirm: true,
  });
  if (createError) throw new Error(`Could not create ${label}: ${createError.message}`);

  const client = createClient(SUPABASE_URL!, PUBLISHABLE_KEY!, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { error: signInError } = await client.auth.signInWithPassword({ email, password });
  if (signInError) throw new Error(`Could not sign in ${label}: ${signInError.message}`);

  return { client, userId: created.user!.id };
}

async function seedTenant(client: SupabaseClient, userId: string, name: string) {
  const { data: org, error: orgError } = await client.rpc('create_organization', {
    p_name: name,
    p_slug: `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${randomUUID().slice(0, 6)}`,
  });
  if (orgError) throw new Error(`create_organization failed for ${name}: ${orgError.message}`);

  const { data: workspace, error: wsError } = await client.rpc('create_workspace', {
    p_org_id: org.id,
    p_name: `${name} client`,
  });
  if (wsError) throw new Error(`create_workspace failed for ${name}: ${wsError.message}`);

  const { data: dataset } = await admin
    .from('datasets')
    .insert({ workspace_id: workspace.id, name: 'Monthly sales', created_by: userId })
    .select('id')
    .single();

  const uploadId = randomUUID();
  await admin.from('raw_uploads').insert({
    id: uploadId,
    workspace_id: workspace.id,
    dataset_id: dataset!.id,
    storage_path: `${org.id}/${workspace.id}/2026-08/${uploadId}__sales.xlsx`,
    original_filename: 'sales.xlsx',
    mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    byte_size: 100,
    status: 'stored',
    uploaded_by: userId,
  });

  return { orgId: org.id, workspaceId: workspace.id, datasetId: dataset!.id, uploadId };
}

/**
 * Empty the queue before testing it.
 *
 * `claim_agent_job` serves the whole queue by design -- it takes the oldest
 * highest-priority job anywhere, because a worker is not tenant-scoped. That
 * makes the claim assertions below order-dependent: a job left behind by an
 * earlier run is older than this run's, so it is claimed first and the test
 * fails while the code is perfectly correct.
 *
 * So the test starts from an empty queue rather than asserting loosely. Guarded
 * to a local stack, because this deletes rows and no test should be able to do
 * that to a real deployment by way of a mistyped env file.
 */
async function clearQueue() {
  const local = /127\.0\.0\.1|localhost/.test(SUPABASE_URL!);
  if (!local) {
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
  console.log('\nQueue protocol\n');

  await clearQueue();

  const firmA = await createUserClient('firm-a');
  const firmB = await createUserClient('firm-b');
  const tenantA = await seedTenant(firmA.client, firmA.userId, 'Firm A');
  const tenantB = await seedTenant(firmB.client, firmB.userId, 'Firm B');

  // -- tenancy --------------------------------------------------------------

  const { data: ownJob, error: ownError } = await firmA.client.rpc('enqueue_agent_job', {
    p_workspace_id: tenantA.workspaceId,
    p_kind: 'analyze_workbook',
    p_raw_upload_id: tenantA.uploadId,
    p_dataset_id: tenantA.datasetId,
  });
  check('a user can queue work in their own workspace', !ownError && Boolean(ownJob?.id), ownError?.message);

  const { error: crossError } = await firmB.client.rpc('enqueue_agent_job', {
    p_workspace_id: tenantA.workspaceId,
    p_kind: 'analyze_workbook',
    p_raw_upload_id: tenantA.uploadId,
  });
  check('another firm cannot queue work in it', Boolean(crossError));

  // Firm B owns the workspace but names Firm A's upload: the id is real, and
  // only the same-workspace check stops the worker acting on it.
  const { error: mixedError } = await firmB.client.rpc('enqueue_agent_job', {
    p_workspace_id: tenantB.workspaceId,
    p_kind: 'analyze_workbook',
    p_raw_upload_id: tenantA.uploadId,
  });
  check("a job cannot point at another workspace's upload", Boolean(mixedError));

  const { data: visible } = await firmB.client.from('agent_jobs').select('id');
  check("another firm's jobs are invisible", (visible ?? []).length === 0);

  // -- worker privilege -----------------------------------------------------

  for (const fn of ['claim_agent_job', 'finish_agent_job', 'heartbeat_agent_job', 'agent_worker_heartbeat', 'reap_expired_agent_jobs']) {
    const { error } = await firmA.client.rpc(fn as never, {} as never);
    check(`${fn} is unreachable from a browser session`, Boolean(error));
  }

  // -- the protocol ---------------------------------------------------------

  const workerId = `test-${randomUUID().slice(0, 8)}`;
  await admin.rpc('agent_worker_heartbeat', { p_worker_id: workerId, p_version: 'test' });

  const { data: firstClaim } = await admin.rpc('claim_agent_job', {
    p_worker_id: workerId,
    p_lease_seconds: 300,
  });
  const claimed = Array.isArray(firstClaim) ? firstClaim[0] : firstClaim;
  check('a worker can claim a queued job', claimed?.id === ownJob.id);

  const { data: secondClaim } = await admin.rpc('claim_agent_job', {
    p_worker_id: workerId,
    p_lease_seconds: 300,
  });
  check(
    'the same job is not claimed twice',
    !Array.isArray(secondClaim) || secondClaim.length === 0,
  );

  // The dedup: pressing Analyze again while it runs returns the running job
  // rather than queueing a second one.
  const { data: dedup } = await firmA.client.rpc('enqueue_agent_job', {
    p_workspace_id: tenantA.workspaceId,
    p_kind: 'analyze_workbook',
    p_raw_upload_id: tenantA.uploadId,
    p_dataset_id: tenantA.datasetId,
  });
  check('queueing the same work again returns the running job', dedup?.id === ownJob.id);

  const { data: beat } = await admin.rpc('heartbeat_agent_job', {
    p_job_id: ownJob.id,
    p_worker_id: workerId,
    p_progress: { stage: 'analysing' },
  });
  check('the owning worker can extend its lease', beat === true);

  const { data: stolen } = await admin.rpc('heartbeat_agent_job', {
    p_job_id: ownJob.id,
    p_worker_id: 'someone-else',
  });
  check('another worker cannot extend it', stolen === false);

  const { data: finished } = await admin.rpc('finish_agent_job', {
    p_job_id: ownJob.id,
    p_worker_id: workerId,
    p_success: true,
    p_result: { rows: 9, findings: [] },
  });
  check('a finished job is terminal', finished?.status === 'succeeded' && finished?.finished_at);

  // -- recovery -------------------------------------------------------------

  const { data: recoverable } = await firmA.client.rpc('enqueue_agent_job', {
    p_workspace_id: tenantA.workspaceId,
    p_kind: 'analyze_workbook',
    p_raw_upload_id: tenantA.uploadId,
    p_payload: { note: 'recovery' },
  });

  await admin.rpc('claim_agent_job', { p_worker_id: workerId, p_lease_seconds: 300 });
  // Simulate the worker dying: the lease lapses with the job still 'running'.
  await admin
    .from('agent_jobs')
    .update({ lease_expires_at: new Date(Date.now() - 60_000).toISOString() })
    .eq('id', recoverable.id);

  const { data: reclaim } = await admin.rpc('claim_agent_job', {
    p_worker_id: workerId,
    p_lease_seconds: 300,
  });
  const reclaimed = Array.isArray(reclaim) ? reclaim[0] : reclaim;
  check('a job whose worker died is claimable again', reclaimed?.id === recoverable.id);
  check('the retry is counted', (reclaimed?.attempts ?? 0) === 2);

  // Burn the remaining attempt, then expire the lease with none left. Before
  // the reaper existed this row stayed 'running' for ever and the dedup made
  // the work permanently un-requeueable.
  await admin
    .from('agent_jobs')
    .update({ attempts: 3, lease_expires_at: new Date(Date.now() - 60_000).toISOString() })
    .eq('id', recoverable.id);

  await admin.rpc('claim_agent_job', { p_worker_id: workerId, p_lease_seconds: 300 });

  const { data: reaped } = await admin
    .from('agent_jobs')
    .select('status, finished_at, error')
    .eq('id', recoverable.id)
    .single();

  check('an exhausted job is reaped rather than stranded', reaped?.status === 'failed', `status was ${reaped?.status}`);
  check('the reaped job records when it ended', Boolean(reaped?.finished_at));
  check('the reaped job explains itself to the user', Boolean(reaped?.error));

  // With the job terminal, the same work can be asked for again -- the thing
  // the deadlock made impossible.
  const { data: requeued, error: requeueError } = await firmA.client.rpc('enqueue_agent_job', {
    p_workspace_id: tenantA.workspaceId,
    p_kind: 'analyze_workbook',
    p_raw_upload_id: tenantA.uploadId,
    p_payload: { note: 'recovery' },
  });
  check(
    'the user can ask for that work again',
    !requeueError && Boolean(requeued?.id) && requeued.id !== recoverable.id,
    requeueError?.message,
  );

  // -- report ---------------------------------------------------------------

  // Remove the synthetic worker row this run registered. It is only a
  // heartbeat, but a fresh one left behind makes `test:cold-start` skip -- and a
  // suite that quietly skips its own regression test is worse than one that
  // fails.
  await admin.from('agent_workers').delete().eq('id', workerId);

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
