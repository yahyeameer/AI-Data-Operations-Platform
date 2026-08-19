/**
 * Agent isolation and queue-protocol checks.
 *
 * The agent adds a second privileged actor to a system whose entire risk model
 * is "two accounting firms share one database" (PRD section 13). `rls-smoke.ts`
 * proves the Week 1 boundary holds; this proves the boundary the agent
 * introduced holds too, and it is meant to be run alongside it on every change.
 *
 * Three things are under test.
 *
 * **Tenancy.** A signed-in user must not be able to queue work against another
 * firm's data, read another firm's jobs or proposals, or decide another firm's
 * changes. The agent runs those jobs with the service key, so a job accepted
 * across the tenant boundary would be executed across it too.
 *
 * **Privilege.** The worker-side RPCs must be unreachable from a browser
 * session. `claim_agent_job` and `finish_agent_job` let the caller write
 * arbitrary results onto a job; `record_dataset_version` writes the lineage
 * chain. None of them may be callable with a publishable key.
 *
 * **The queue protocol itself.** Exactly-once claiming, lease recovery after a
 * crash, and the refusal to accept results from a worker that no longer holds
 * the job. These are correctness properties of a 24/7 process nobody watches,
 * so they are asserted rather than assumed.
 *
 * Usage: npm run test:agent   (requires `supabase start`)
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

/** A firm with one workspace, one dataset and one parsed dataset version. */
async function seedTenant(client: SupabaseClient, userId: string, name: string) {
  const { data: org, error: orgError } = await client.rpc('create_organization', {
    p_name: name,
    p_slug: `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${randomUUID().slice(0, 6)}`,
  });
  if (orgError) throw new Error(`create_organization failed: ${orgError.message}`);

  const { data: workspace, error: wsError } = await client.rpc('create_workspace', {
    p_org_id: org.id,
    p_name: `${name} client`,
  });
  if (wsError) throw new Error(`create_workspace failed: ${wsError.message}`);

  const { data: dataset } = await admin
    .from('datasets')
    .insert({ workspace_id: workspace.id, name: 'Monthly sales', created_by: userId })
    .select('id')
    .single();

  const { data: version } = await admin.rpc('record_dataset_version', {
    p_dataset_id: dataset!.id,
    p_kind: 'cleaned',
    p_parquet_path: `${org.id}/${workspace.id}/2026-08/${dataset!.id}__seed.parquet`,
    p_row_count: 10,
    p_created_by: userId,
  });

  return {
    orgId: org.id as string,
    workspaceId: workspace.id as string,
    datasetId: dataset!.id as string,
    versionId: version.id as string,
  };
}

/** A pending proposal, written the way the worker writes them. */
async function seedProposal(versionId: string, groupKey: string) {
  const count = await admin.rpc('replace_proposed_changes', {
    p_dataset_version_id: versionId,
    p_job_id: null,
    p_proposals: [
      {
        group_key: groupKey,
        step_type: 'map_values',
        column_name: 'supplier',
        title: 'Merge supplier spellings',
        rationale: 'Two spellings of one supplier.',
        operation: { op: 'map_values', column: 'supplier', mapping: { a: 'A' } },
        evidence: { groups: [] },
        confidence: 'medium',
        affected_rows: 2,
        materiality_gbp: '1200.00',
      },
    ],
  });
  return count.data as number;
}

async function main() {
  console.log('\nAgent tenancy\n');

  const alpha = await createUserClient('alpha');
  const beta = await createUserClient('beta');

  const alphaData = await seedTenant(alpha.client, alpha.userId, 'Alpha Accounting');
  const betaData = await seedTenant(beta.client, beta.userId, 'Beta Bookkeeping');

  await seedProposal(alphaData.versionId, 'entity:supplier');

  // --- Beta must not be able to queue work against Alpha's data ------------

  const { error: crossEnqueue } = await beta.client.rpc('enqueue_agent_job', {
    p_workspace_id: alphaData.workspaceId,
    p_kind: 'parse_workbook',
  });
  check("Beta cannot enqueue a job in Alpha's workspace", crossEnqueue !== null);

  // The subtler one: a workspace Beta *does* own, aimed at Alpha's dataset.
  // The worker would run this with the service key, so the enqueue RPC has to
  // refuse it rather than trusting that the ids belong together.
  const { error: mismatchedTarget } = await beta.client.rpc('enqueue_agent_job', {
    p_workspace_id: betaData.workspaceId,
    p_kind: 'profile_dataset',
    p_dataset_version_id: alphaData.versionId,
  });
  check(
    "Beta cannot aim a job in its own workspace at Alpha's dataset version",
    mismatchedTarget !== null,
    mismatchedTarget ? undefined : 'the job was accepted',
  );

  const { error: mismatchedDataset } = await beta.client.rpc('enqueue_agent_job', {
    p_workspace_id: betaData.workspaceId,
    p_kind: 'parse_workbook',
    p_dataset_id: alphaData.datasetId,
  });
  check("Beta cannot aim a job at Alpha's dataset", mismatchedDataset !== null);

  const { data: ownEnqueue, error: ownEnqueueError } = await beta.client.rpc('enqueue_agent_job', {
    p_workspace_id: betaData.workspaceId,
    p_kind: 'profile_dataset',
    p_dataset_version_id: betaData.versionId,
  });
  check(
    'Beta can enqueue a job in its own workspace',
    ownEnqueueError === null && !!ownEnqueue,
    ownEnqueueError?.message,
  );

  // --- Reads ---------------------------------------------------------------

  const { data: alphaJobs } = await admin.rpc('enqueue_agent_job_internal', {
    p_workspace_id: alphaData.workspaceId,
    p_kind: 'parse_workbook',
    p_requested_by: alpha.userId,
  });
  const alphaJobId = alphaJobs.id as string;

  const { data: seenJobs } = await beta.client
    .from('agent_jobs')
    .select('id')
    .eq('id', alphaJobId);
  check("Beta cannot read Alpha's agent jobs", (seenJobs ?? []).length === 0);

  const { data: seenChanges } = await beta.client
    .from('proposed_changes')
    .select('id')
    .eq('dataset_version_id', alphaData.versionId);
  check("Beta cannot read Alpha's proposed changes", (seenChanges ?? []).length === 0);

  await admin.rpc('record_dataset_profile', {
    p_dataset_version_id: alphaData.versionId,
    p_row_count: 10,
    p_column_count: 3,
    p_columns: [],
    p_signals: {},
  });
  const { data: seenProfiles } = await beta.client
    .from('dataset_profiles')
    .select('id')
    .eq('dataset_version_id', alphaData.versionId);
  check("Beta cannot read Alpha's dataset profile", (seenProfiles ?? []).length === 0);

  await admin.rpc('record_analysis_run', {
    p_dataset_version_id: alphaData.versionId,
    p_question: 'total by supplier',
    p_executed_sql: 'select 1',
    p_result: {},
  });
  const { data: seenRuns } = await beta.client
    .from('analysis_runs')
    .select('id')
    .eq('dataset_version_id', alphaData.versionId);
  check("Beta cannot read Alpha's analysis runs", (seenRuns ?? []).length === 0);

  // Worker liveness is readable by anyone signed in, and deliberately so: it
  // carries no customer data, and hiding it would make the UI lie during an
  // outage. Asserted rather than left implicit.
  const { error: workerReadError } = await beta.client.from('agent_workers').select('id');
  check('worker liveness is readable by any signed-in user', workerReadError === null);

  // --- Decisions -----------------------------------------------------------

  const { error: crossDecide } = await beta.client.rpc('decide_proposed_changes', {
    p_dataset_version_id: alphaData.versionId,
    p_group_keys: ['entity:supplier'],
    p_approve: true,
  });
  check("Beta cannot approve Alpha's proposed changes", crossDecide !== null);

  const { error: crossCancel } = await beta.client.rpc('cancel_agent_job', {
    p_job_id: alphaJobId,
  });
  check("Beta cannot cancel Alpha's job", crossCancel !== null);

  const { data: alphaDecided, error: ownDecideError } = await alpha.client.rpc(
    'decide_proposed_changes',
    {
      p_dataset_version_id: alphaData.versionId,
      p_group_keys: ['entity:supplier'],
      p_approve: true,
    },
  );
  check(
    'Alpha can approve its own proposed changes',
    ownDecideError === null && alphaDecided === 1,
    ownDecideError?.message ?? `decided ${alphaDecided}`,
  );

  // --- Worker-only RPCs are unreachable from a session ---------------------

  console.log('\nWorker privilege\n');

  const workerOnly: [string, Record<string, unknown>][] = [
    ['claim_agent_job', { p_worker_id: 'attacker' }],
    ['finish_agent_job', { p_job_id: alphaJobId, p_worker_id: 'attacker', p_success: true }],
    ['heartbeat_agent_job', { p_job_id: alphaJobId, p_worker_id: 'attacker' }],
    ['agent_worker_heartbeat', { p_worker_id: 'attacker' }],
    [
      'enqueue_agent_job_internal',
      { p_workspace_id: alphaData.workspaceId, p_kind: 'parse_workbook' },
    ],
    ['record_dataset_version', { p_dataset_id: alphaData.datasetId, p_kind: 'cleaned' }],
    [
      'record_dataset_profile',
      {
        p_dataset_version_id: alphaData.versionId,
        p_row_count: 1,
        p_column_count: 1,
        p_columns: [],
        p_signals: {},
      },
    ],
    [
      'replace_proposed_changes',
      { p_dataset_version_id: alphaData.versionId, p_job_id: null, p_proposals: [] },
    ],
    ['mark_changes_applied', { p_dataset_version_id: alphaData.versionId, p_group_keys: ['x'] }],
    ['set_dataset_signature', { p_dataset_id: alphaData.datasetId, p_signature: 'forged' }],
    [
      'record_analysis_run',
      {
        p_dataset_version_id: alphaData.versionId,
        p_question: 'x',
        p_executed_sql: 'select 1',
        p_result: {},
      },
    ],
  ];

  for (const [fn, params] of workerOnly) {
    const { error } = await beta.client.rpc(fn as never, params as never);
    check(`${fn} is not callable from a browser session`, error !== null);
  }

  // --- Queue protocol ------------------------------------------------------

  console.log('\nQueue protocol\n');

  // Deduplication: the same request twice is one job, so an impatient double
  // click does not parse the same workbook twice.
  const dedupeParams = {
    p_workspace_id: betaData.workspaceId,
    p_kind: 'generate_report' as const,
    p_dataset_version_id: betaData.versionId,
  };
  const { data: first } = await beta.client.rpc('enqueue_agent_job', dedupeParams);
  const { data: second } = await beta.client.rpc('enqueue_agent_job', dedupeParams);
  check('an identical queued job is returned rather than duplicated', first.id === second.id);

  await admin.rpc('agent_worker_heartbeat', { p_worker_id: 'worker-a' });
  await admin.rpc('agent_worker_heartbeat', { p_worker_id: 'worker-b' });

  // Exactly-once claiming.
  const claimed = new Set<string>();
  let claimCount = 0;
  for (let i = 0; i < 12; i += 1) {
    const worker = i % 2 === 0 ? 'worker-a' : 'worker-b';
    const { data } = await admin.rpc('claim_agent_job', {
      p_worker_id: worker,
      p_lease_seconds: 300,
    });
    const job = data?.[0];
    if (!job) break;
    claimCount += 1;
    check(
      `job ${job.kind} claimed exactly once`,
      !claimed.has(job.id),
      'the same job was handed to two workers',
    );
    claimed.add(job.id);
  }
  check('every queued job was claimed', claimCount > 0);

  const { data: nothingLeft } = await admin.rpc('claim_agent_job', { p_worker_id: 'worker-a' });
  check(
    'an empty queue returns an empty set, not a row of nulls',
    (nothingLeft ?? []).length === 0,
  );

  // A worker that no longer holds the job cannot report on it. This is what
  // stops a resumed-from-suspend worker overwriting the result of the job that
  // was taken from it.
  const targetJobId = [...claimed][0];
  const { data: heldBy } = await admin
    .from('agent_jobs')
    .select('claimed_by')
    .eq('id', targetJobId)
    .single();
  const otherWorker = heldBy!.claimed_by === 'worker-a' ? 'worker-b' : 'worker-a';

  const { error: wrongFinish } = await admin.rpc('finish_agent_job', {
    p_job_id: targetJobId,
    p_worker_id: otherWorker,
    p_success: true,
    p_result: { forged: true },
  });
  check('a worker cannot finish a job it does not hold', wrongFinish !== null);

  const { data: wrongHeartbeat } = await admin.rpc('heartbeat_agent_job', {
    p_job_id: targetJobId,
    p_worker_id: otherWorker,
  });
  check('a worker cannot renew a lease it does not hold', wrongHeartbeat === false);

  // Crash recovery: expire the lease by hand and confirm the job becomes
  // claimable again. This is the property that makes a VPS reboot survivable.
  await admin
    .from('agent_jobs')
    .update({ lease_expires_at: new Date(Date.now() - 60_000).toISOString() })
    .eq('id', targetJobId);

  const { data: reclaimedRows } = await admin.rpc('claim_agent_job', {
    p_worker_id: otherWorker,
    p_lease_seconds: 300,
  });
  const reclaimed = reclaimedRows?.[0];
  check(
    'an expired lease lets another worker take the job',
    reclaimed?.id === targetJobId,
    `got ${reclaimed?.id ?? 'nothing'}`,
  );
  check('reclaiming counts as another attempt', (reclaimed?.attempts ?? 0) >= 2);

  // Retry policy.
  const { data: retried } = await admin.rpc('finish_agent_job', {
    p_job_id: targetJobId,
    p_worker_id: otherWorker,
    p_success: false,
    p_error: 'transient',
    p_retryable: true,
  });
  check(
    'a retryable failure with attempts left returns to the queue',
    retried.status === 'queued',
    `status was ${retried.status}`,
  );

  const { data: reclaimedAgainRows } = await admin.rpc('claim_agent_job', {
    p_worker_id: 'worker-a',
    p_lease_seconds: 300,
  });
  const reclaimedAgain = reclaimedAgainRows![0];
  const { data: permanent } = await admin.rpc('finish_agent_job', {
    p_job_id: reclaimedAgain.id,
    p_worker_id: 'worker-a',
    p_success: false,
    p_error: 'this file is a legacy .xls',
    p_retryable: false,
  });
  check(
    'a non-retryable failure fails immediately rather than retrying',
    permanent.status === 'failed',
    `status was ${permanent.status}`,
  );

  // --- Proposal immutability ----------------------------------------------

  console.log('\nProposal immutability (service role, RLS bypassed)\n');

  const { data: approvedRow } = await admin
    .from('proposed_changes')
    .select('id, status')
    .eq('dataset_version_id', alphaData.versionId)
    .eq('status', 'approved')
    .single();

  const { error: reDecide } = await admin
    .from('proposed_changes')
    .update({ status: 'rejected', decided_at: new Date().toISOString() })
    .eq('id', approvedRow!.id);
  check('a decided proposal cannot be re-decided', reDecide !== null);

  const { error: rewriteOperation } = await admin
    .from('proposed_changes')
    .update({ operation: { op: 'drop_duplicate_rows' } })
    .eq('id', approvedRow!.id);
  check('an approved proposal\'s operation cannot be rewritten', rewriteOperation !== null);

  const { error: deleteProposal } = await admin
    .from('proposed_changes')
    .delete()
    .eq('id', approvedRow!.id);
  check('proposals cannot be deleted', deleteProposal !== null);

  const { error: applyTransition } = await admin
    .from('proposed_changes')
    .update({ status: 'applied' })
    .eq('id', approvedRow!.id);
  check(
    'approved -> applied is the one permitted follow-on transition',
    applyTransition === null,
    applyTransition?.message,
  );

  const { error: profileUpdate } = await admin
    .from('dataset_profiles')
    .update({ row_count: 9999 })
    .eq('dataset_version_id', alphaData.versionId);
  check('dataset_profiles cannot be updated', profileUpdate !== null);

  const { error: analysisUpdate } = await admin
    .from('analysis_runs')
    .update({ executed_sql: 'select 2' })
    .eq('dataset_version_id', alphaData.versionId);
  check('analysis_runs cannot be rewritten', analysisUpdate !== null);

  // --- Result ---------------------------------------------------------------

  console.log(`\n${passed} passed, ${failures.length} failed\n`);

  if (failures.length > 0) {
    for (const failure of failures) console.error(`  ${failure}`);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
