import { NextResponse } from 'next/server';
import { z } from 'zod';

import { handleRouteError } from '@/lib/api';
import { requireWorkspaceAccess } from '@/lib/authz';
import { createServerSupabase } from '@/lib/supabase/server';

/**
 * Asking the agent to do something, and finding out what it has done.
 *
 * Note what this route does *not* do: it never contacts the agent. The worker
 * lives on another host with no inbound port, and the queue table is the whole
 * interface between them. Enqueuing is a database write that returns in
 * milliseconds whether the agent is up, down or mid-restart — so a VPS reboot
 * during month-end delays the work rather than losing the request.
 *
 * The enqueue deliberately runs through the *user's* client rather than the
 * service role. `enqueue_agent_job` is SECURITY DEFINER and re-checks
 * membership from `auth.uid()`, so routing the call through the signed-in
 * session means the database authorises it independently of this route having
 * got it right. `requireWorkspaceAccess` above is the other half of PRD
 * section 13's "RLS plus server-side authorization on every path".
 */

const KINDS = [
  'parse_workbook',
  'profile_dataset',
  'propose_cleaning',
  'apply_cleaning',
  'query_dataset',
  'reconcile_sources',
  'generate_report',
] as const;

const createSchema = z.object({
  workspaceId: z.string().uuid(),
  kind: z.enum(KINDS),
  datasetId: z.string().uuid().nullish(),
  datasetVersionId: z.string().uuid().nullish(),
  rawUploadId: z.string().uuid().nullish(),
  payload: z.record(z.string(), z.unknown()).nullish(),
});

export async function POST(request: Request) {
  try {
    const body = createSchema.parse(await request.json());

    await requireWorkspaceAccess(body.workspaceId);
    const supabase = await createServerSupabase();

    const { data, error } = await supabase.rpc('enqueue_agent_job', {
      p_workspace_id: body.workspaceId,
      p_kind: body.kind,
      p_payload: (body.payload ?? {}) as never,
      p_dataset_id: body.datasetId ?? undefined,
      p_dataset_version_id: body.datasetVersionId ?? undefined,
      p_raw_upload_id: body.rawUploadId ?? undefined,
      // A question typed into the dashboard is someone waiting at a screen.
      // A parse is a background chore. Ordering the queue by that difference
      // costs one number here and is the whole reason the column exists.
      p_priority: body.kind === 'query_dataset' ? 10 : 100,
    });

    if (error) {
      return NextResponse.json({ error: error.message }, { status: 400 });
    }

    return NextResponse.json({ job: data });
  } catch (error) {
    return handleRouteError(error);
  }
}

export async function GET(request: Request) {
  try {
    const url = new URL(request.url);
    const workspaceId = url.searchParams.get('workspaceId');

    if (!workspaceId) {
      return NextResponse.json({ error: 'workspaceId is required' }, { status: 400 });
    }

    await requireWorkspaceAccess(workspaceId);
    const supabase = await createServerSupabase();

    const { data: jobs, error } = await supabase
      .from('agent_jobs')
      .select(
        'id, kind, status, progress, error, result, attempts, max_attempts, dataset_id, dataset_version_id, created_at, started_at, finished_at',
      )
      .eq('workspace_id', workspaceId)
      .order('created_at', { ascending: false })
      .limit(20);

    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    // Liveness travels with the job list so the panel needs one request per
    // poll rather than two. Reading every worker row is cheap — there is one
    // per agent host, and the table carries no customer data.
    const { data: workers } = await supabase
      .from('agent_workers')
      .select('id, hostname, version, last_seen_at, jobs_claimed, metadata')
      .order('last_seen_at', { ascending: false })
      .limit(5);

    return NextResponse.json({ jobs: jobs ?? [], workers: workers ?? [] });
  } catch (error) {
    return handleRouteError(error);
  }
}
