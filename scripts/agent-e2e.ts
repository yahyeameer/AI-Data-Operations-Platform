/**
 * The agent seam, over real HTTP.
 *
 * `agent-smoke.ts` tests the database contract directly. This tests the path an
 * accountant's browser actually takes: the route handlers, their session
 * handling, and their authorization checks — the layer that would still be a
 * hole if the SQL were perfect and a route forgot to call it.
 *
 * If a worker is online it also waits for the pipeline to finish, which makes
 * this the one test that proves the dashboard and the agent are connected
 * rather than merely both correct. With no worker it checks everything up to
 * the point of hand-off and says clearly what it skipped, because a CI box
 * without an agent should report "not exercised" rather than a false pass.
 *
 * Usage: npm run test:agent:e2e   (needs the dev server; a worker is optional)
 */

import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { config as loadEnv } from 'dotenv';
import { createClient } from '@supabase/supabase-js';

loadEnv({ path: 'apps/web/.env.local', quiet: true });

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!;
const SECRET_KEY = process.env.SUPABASE_SECRET_KEY!;
const APP_URL = process.env.APP_URL ?? 'http://127.0.0.1:3100';

if (!SUPABASE_URL || !PUBLISHABLE_KEY || !SECRET_KEY) {
  console.error('Missing Supabase env. Run `supabase start` and fill apps/web/.env.local.');
  process.exit(1);
}

const MAX_CHUNK_SIZE = 3180;
const FIXTURE = 'fixtures/messy/acme-sales-2026-08.xlsx';

/** How long to wait for a worker to finish the parse -> profile -> propose chain. */
const PIPELINE_TIMEOUT_MS = 60_000;

const admin = createClient(SUPABASE_URL, SECRET_KEY, {
  auth: { persistSession: false, autoRefreshToken: false },
});

let passed = 0;
const failures: string[] = [];
const skipped: string[] = [];

function check(name: string, condition: boolean, detail = '') {
  if (condition) {
    passed += 1;
    console.log(`  PASS  ${name}`);
  } else {
    failures.push(`${name}${detail ? ` -- ${detail}` : ''}`);
    console.log(`  FAIL  ${name}${detail ? ` -- ${detail}` : ''}`);
  }
}

function skip(name: string, why: string) {
  skipped.push(`${name} -- ${why}`);
  console.log(`  SKIP  ${name} (${why})`);
}

function cookieBaseName(): string {
  return `sb-${new URL(SUPABASE_URL).hostname.split('.')[0]}-auth-token`;
}

function sessionCookie(session: unknown): string {
  const encoded = `base64-${Buffer.from(JSON.stringify(session)).toString('base64url')}`;
  const base = cookieBaseName();

  const chunks: string[] = [];
  for (let i = 0; i < encoded.length; i += MAX_CHUNK_SIZE) {
    chunks.push(encoded.slice(i, i + MAX_CHUNK_SIZE));
  }

  return chunks.map((chunk, index) => `${base}.${index}=${chunk}`).join('; ');
}

async function signUpUser(label: string) {
  const email = `${label}-${randomUUID().slice(0, 8)}@example.test`;
  const password = `pw-${randomUUID()}`;

  const { error } = await admin.auth.admin.createUser({ email, password, email_confirm: true });
  if (error) throw new Error(`createUser failed: ${error.message}`);

  const response = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=password`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', apikey: PUBLISHABLE_KEY },
    body: JSON.stringify({ email, password }),
  });

  const session = await response.json();
  if (!response.ok) throw new Error(`sign-in failed: ${JSON.stringify(session)}`);

  const client = createClient(SUPABASE_URL, PUBLISHABLE_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  await client.auth.setSession({
    access_token: session.access_token,
    refresh_token: session.refresh_token,
  });

  return { cookie: sessionCookie(session), client, userId: session.user.id as string };
}

const postJson = (path: string, body: unknown, cookie?: string) =>
  fetch(`${APP_URL}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...(cookie ? { cookie } : {}) },
    body: JSON.stringify(body),
    redirect: 'manual',
  });

const get = (path: string, cookie?: string) =>
  fetch(`${APP_URL}${path}`, {
    headers: cookie ? { cookie } : {},
    redirect: 'manual',
  });

/** A firm with one workspace, and the fixture uploaded through the real routes. */
async function seedTenantViaHttp(
  label: string,
): Promise<{
  cookie: string;
  userId: string;
  orgId: string;
  workspaceId: string;
  datasetId: string;
  uploadId: string;
}> {
  const user = await signUpUser(label);

  const { data: org, error: orgError } = await user.client.rpc('create_organization', {
    p_name: `${label} Accounting`,
    p_slug: `${label}-${randomUUID().slice(0, 8)}`,
  });
  if (orgError) throw new Error(`create_organization failed: ${orgError.message}`);

  const { data: workspace, error: wsError } = await user.client.rpc('create_workspace', {
    p_org_id: org.id,
    p_name: `${label} client`,
  });
  if (wsError) throw new Error(`create_workspace failed: ${wsError.message}`);

  const bytes = readFileSync(FIXTURE);

  const signResponse = await postJson(
    '/api/uploads/sign',
    {
      workspaceId: workspace.id,
      filename: 'acme-sales-2026-08.xlsx',
      byteSize: bytes.byteLength,
      datasetName: 'ACME monthly sales',
    },
    user.cookie,
  );
  const signed = await signResponse.json();
  if (!signResponse.ok) throw new Error(`sign failed: ${JSON.stringify(signed)}`);

  const storage = createClient(SUPABASE_URL, PUBLISHABLE_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { error: uploadError } = await storage.storage
    .from(signed.bucket)
    .uploadToSignedUrl(signed.storagePath, signed.token, bytes, {
      contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });
  if (uploadError) throw new Error(`upload failed: ${uploadError.message}`);

  const completeResponse = await postJson(
    '/api/uploads/complete',
    { uploadId: signed.uploadId, workspaceId: workspace.id },
    user.cookie,
  );
  const completed = await completeResponse.json();
  if (!completeResponse.ok) throw new Error(`complete failed: ${JSON.stringify(completed)}`);

  return {
    cookie: user.cookie,
    userId: user.userId,
    orgId: org.id,
    workspaceId: workspace.id,
    datasetId: signed.datasetId,
    uploadId: signed.uploadId,
  };
}

async function workerIsOnline(): Promise<boolean> {
  const { data } = await admin
    .from('agent_workers')
    .select('last_seen_at')
    .order('last_seen_at', { ascending: false })
    .limit(1);

  const seen = data?.[0]?.last_seen_at;
  return !!seen && Date.now() - new Date(seen).getTime() < 90_000;
}

/** Poll until the workspace has no unfinished jobs left. */
async function waitForQuiet(workspaceId: string, cookie: string): Promise<boolean> {
  const deadline = Date.now() + PIPELINE_TIMEOUT_MS;

  while (Date.now() < deadline) {
    const response = await get(`/api/agent/jobs?workspaceId=${workspaceId}`, cookie);
    if (response.ok) {
      const body = await response.json();
      const jobs = body.jobs as { status: string }[];
      const active = jobs.filter(
        (job) => job.status === 'queued' || job.status === 'running',
      );
      if (jobs.length > 0 && active.length === 0) return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }

  return false;
}

async function main() {
  console.log('\nRoute guards\n');

  const alpha = await seedTenantViaHttp('alpha');
  const beta = await seedTenantViaHttp('beta');

  const anonymous = await postJson('/api/agent/jobs', {
    workspaceId: alpha.workspaceId,
    kind: 'parse_workbook',
  });
  check('an unauthenticated caller cannot queue a job', anonymous.status === 401, `got ${anonymous.status}`);

  const crossTenant = await postJson(
    '/api/agent/jobs',
    { workspaceId: alpha.workspaceId, kind: 'parse_workbook', rawUploadId: alpha.uploadId },
    beta.cookie,
  );
  check(
    "Beta cannot queue a job against Alpha's workspace",
    crossTenant.status === 404 || crossTenant.status === 403,
    `got ${crossTenant.status}`,
  );

  const crossRead = await get(`/api/agent/jobs?workspaceId=${alpha.workspaceId}`, beta.cookie);
  check(
    "Beta cannot list Alpha's jobs",
    crossRead.status === 404 || crossRead.status === 403,
    `got ${crossRead.status}`,
  );

  const badKind = await postJson(
    '/api/agent/jobs',
    { workspaceId: alpha.workspaceId, kind: 'rm_minus_rf' },
    alpha.cookie,
  );
  check('an unknown job kind is rejected', badKind.status === 400, `got ${badKind.status}`);

  console.log('\nEnqueue\n');

  const enqueue = await postJson(
    '/api/agent/jobs',
    { workspaceId: alpha.workspaceId, kind: 'parse_workbook', rawUploadId: alpha.uploadId, datasetId: alpha.datasetId },
    alpha.cookie,
  );
  const enqueued = await enqueue.json();
  check('Alpha can queue a parse for its own upload', enqueue.ok, JSON.stringify(enqueued).slice(0, 200));

  // The double-click case. Two identical requests must not parse twice.
  const again = await postJson(
    '/api/agent/jobs',
    { workspaceId: alpha.workspaceId, kind: 'parse_workbook', rawUploadId: alpha.uploadId, datasetId: alpha.datasetId },
    alpha.cookie,
  );
  const repeated = await again.json();
  check(
    'queueing the same work twice returns the same job',
    repeated.job?.id === enqueued.job?.id,
    `${enqueued.job?.id} vs ${repeated.job?.id}`,
  );

  const listed = await get(`/api/agent/jobs?workspaceId=${alpha.workspaceId}`, alpha.cookie);
  const listedBody = await listed.json();
  check(
    'the job appears in the workspace job list',
    listed.ok && listedBody.jobs.some((job: { id: string }) => job.id === enqueued.job.id),
  );
  check('the job list reports worker liveness', Array.isArray(listedBody.workers));

  console.log('\nPipeline\n');

  if (!(await workerIsOnline())) {
    skip('the agent completes the pipeline', 'no worker is online');
    skip('approving changes produces a new version', 'no worker is online');
  } else {
    const quiet = await waitForQuiet(alpha.workspaceId, alpha.cookie);
    check('the agent finished every queued job', quiet, `still busy after ${PIPELINE_TIMEOUT_MS}ms`);

    const { data: jobs } = await admin
      .from('agent_jobs')
      .select('kind, status, error')
      .eq('workspace_id', alpha.workspaceId);

    const byKind = new Map((jobs ?? []).map((job) => [job.kind, job]));
    for (const kind of ['parse_workbook', 'profile_dataset', 'propose_cleaning']) {
      const job = byKind.get(kind);
      check(`${kind} succeeded`, job?.status === 'succeeded', job?.error ?? 'never ran');
    }

    const { data: versions } = await admin
      .from('dataset_versions')
      .select('id, version_no, row_count')
      .eq('dataset_id', alpha.datasetId)
      .order('version_no', { ascending: true });

    check(
      'the parser wrote a dataset version with the nine transaction rows',
      (versions ?? []).some((version) => version.row_count === 9),
      JSON.stringify(versions),
    );

    const parsed = (versions ?? []).find((version) => version.row_count === 9);

    const { data: changes } = await admin
      .from('proposed_changes')
      .select('group_key, confidence, status')
      .eq('dataset_version_id', parsed!.id);

    check('the agent proposed the full set of changes', (changes ?? []).length === 7, `got ${changes?.length}`);
    check(
      'the totals mismatch is proposed as a blocking change',
      (changes ?? []).some(
        (change) => change.group_key === 'invariant:declared_totals' && change.confidence === 'low',
      ),
    );

    // Beta must not be able to decide Alpha's changes through the route either.
    const crossDecide = await postJson(
      '/api/agent/changes',
      { datasetVersionId: parsed!.id, groupKeys: ['duplicates:exact'], approve: true },
      beta.cookie,
    );
    check("Beta cannot approve Alpha's changes", crossDecide.status === 403, `got ${crossDecide.status}`);

    // Applying with the blocker unresolved must fail, and must not retry.
    await postJson(
      '/api/agent/changes',
      { datasetVersionId: parsed!.id, groupKeys: ['duplicates:exact'], approve: true },
      alpha.cookie,
    );
    await postJson(
      '/api/agent/jobs',
      { workspaceId: alpha.workspaceId, kind: 'apply_cleaning', datasetVersionId: parsed!.id },
      alpha.cookie,
    );
    await waitForQuiet(alpha.workspaceId, alpha.cookie);

    const { data: blockedRun } = await admin
      .from('agent_jobs')
      .select('status, error, attempts')
      .eq('workspace_id', alpha.workspaceId)
      .eq('kind', 'apply_cleaning')
      .order('created_at', { ascending: false })
      .limit(1);

    check(
      'applying is refused while a blocking change is unresolved',
      blockedRun?.[0]?.status === 'failed' && /blocking/i.test(blockedRun[0].error ?? ''),
      blockedRun?.[0]?.error ?? 'no run recorded',
    );
    check(
      'a refusal like that is not retried',
      blockedRun?.[0]?.attempts === 1,
      `attempts=${blockedRun?.[0]?.attempts}`,
    );

    // Resolve everything and apply for real.
    const groups = (changes ?? []).map((change) => change.group_key);
    await postJson(
      '/api/agent/changes',
      { datasetVersionId: parsed!.id, groupKeys: groups, approve: true },
      alpha.cookie,
    );
    await postJson(
      '/api/agent/jobs',
      { workspaceId: alpha.workspaceId, kind: 'apply_cleaning', datasetVersionId: parsed!.id },
      alpha.cookie,
    );
    await waitForQuiet(alpha.workspaceId, alpha.cookie);

    const { data: after } = await admin
      .from('dataset_versions')
      .select('id, version_no, row_count, parent_version_id')
      .eq('dataset_id', alpha.datasetId)
      .order('version_no', { ascending: true });

    const cleaned = (after ?? []).find((version) => version.version_no === parsed!.version_no + 1);

    check('applying wrote a new version', !!cleaned, JSON.stringify(after));
    check(
      'the duplicate row was removed',
      cleaned?.row_count === 8,
      `row_count=${cleaned?.row_count}`,
    );
    check(
      'the new version descends from the one that was reviewed',
      cleaned?.parent_version_id === parsed!.id,
    );
    check(
      'the version that was reviewed is left untouched',
      (after ?? []).some(
        (version) => version.version_no === parsed!.version_no && version.row_count === 9,
      ),
    );

    // The last mile: a cleaned version handed back as a file someone can open.
    await postJson(
      '/api/agent/jobs',
      {
        workspaceId: alpha.workspaceId,
        kind: 'export_dataset',
        datasetVersionId: cleaned!.id,
        payload: { format: 'xlsx' },
      },
      alpha.cookie,
    );
    await waitForQuiet(alpha.workspaceId, alpha.cookie);

    const { data: exportRuns } = await admin
      .from('agent_jobs')
      .select('id, status, result, error')
      .eq('workspace_id', alpha.workspaceId)
      .eq('kind', 'export_dataset')
      .order('created_at', { ascending: false })
      .limit(1);

    const exportJob = exportRuns?.[0];
    const exportResult = (exportJob?.result ?? {}) as Record<string, unknown>;

    check('the export job succeeded', exportJob?.status === 'succeeded', exportJob?.error ?? 'no run');
    check(
      'the export wrote a file to the exports bucket',
      exportResult.bucket === 'exports' && typeof exportResult.export_path === 'string',
      JSON.stringify(exportResult).slice(0, 200),
    );
    // The cleaned version, not the raw one -- exporting the pre-review data
    // would be the kind of failure nobody notices until a client does.
    check(
      'the export carries the cleaned row count',
      exportResult.row_count === 8,
      `row_count=${exportResult.row_count}`,
    );

    if (exportJob) {
      const link = await get(`/api/exports?jobId=${exportJob.id}`, alpha.cookie);
      const linkBody = await link.json();
      check(
        'Alpha can mint a download link for its own export',
        link.ok && typeof linkBody.url === 'string',
        JSON.stringify(linkBody).slice(0, 200),
      );

      // The property the whole route design exists for: the signing client
      // bypasses RLS, so this must be refused by the route, not by storage.
      const crossDownload = await get(`/api/exports?jobId=${exportJob.id}`, beta.cookie);
      check(
        "Beta cannot download Alpha's export",
        crossDownload.status === 404,
        `got ${crossDownload.status}`,
      );
    }

    const { data: audit } = await admin
      .from('audit_logs')
      .select('action')
      .eq('workspace_id', alpha.workspaceId);

    const actions = new Set((audit ?? []).map((row) => row.action));
    for (const action of [
      'agent.job.enqueued',
      'agent.job.succeeded',
      'agent.changes.proposed',
      'agent.changes.approved',
      'dataset.version.created',
    ]) {
      check(`the audit log records ${action}`, actions.has(action));
    }
  }

  console.log(`\n${passed} passed, ${failures.length} failed, ${skipped.length} skipped\n`);

  for (const item of skipped) console.log(`  SKIPPED  ${item}`);

  if (failures.length > 0) {
    for (const failure of failures) console.error(`  ${failure}`);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
