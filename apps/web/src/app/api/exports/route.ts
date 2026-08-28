import { NextResponse } from 'next/server';
import { z } from 'zod';

import { handleRouteError } from '@/lib/api';
import { adminFor, requireWorkspaceAccess } from '@/lib/authz';
import { createServerSupabase } from '@/lib/supabase/server';

/**
 * Handing back a file the agent produced.
 *
 * The route takes a **job id**, never a storage path. That is the whole
 * security design of it. Signing a URL requires the service-role client, which
 * bypasses RLS completely, so a route that signed a caller-supplied path would
 * be an arbitrary-object reader over every tenant's data wearing an
 * authorization check as decoration. Taking a job id means the path can only
 * ever come from a row the caller is already allowed to read, and the storage
 * layout (`{org_id}/{workspace_id}/...`) stops being load-bearing for access
 * control.
 *
 * Two checks, on purpose, matching PRD section 13's "RLS plus server-side
 * authorization on every path":
 *
 *   1. the job is read through the caller's own RLS-bound client, so the
 *      database decides whether this job is visible at all
 *   2. `requireWorkspaceAccess` re-derives membership from the workspace the
 *      job claims, so the route does not depend on having got step 1 right
 *
 * Only then is the admin client constructed.
 */

const querySchema = z.object({
  jobId: z.string().uuid(),
});

/** The buckets an export may legitimately live in. Never `raw`. */
const DOWNLOADABLE_BUCKETS = new Set(['exports', 'cleaned']);

/** The job kinds that produce a downloadable artefact. */
const DOWNLOADABLE_KINDS = new Set(['generate_report', 'export_dataset']);

/**
 * Sixty seconds. The link is minted in response to a click and handed straight
 * to the browser, so it needs to survive one redirect, not an afternoon in
 * someone's inbox.
 */
const SIGNED_URL_TTL_SECONDS = 60;

export async function GET(request: Request) {
  try {
    const url = new URL(request.url);
    const { jobId } = querySchema.parse({ jobId: url.searchParams.get('jobId') });

    const supabase = await createServerSupabase();

    const { data: job, error } = await supabase
      .from('agent_jobs')
      .select('id, kind, status, result, workspace_id')
      .eq('id', jobId)
      .maybeSingle();

    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    // A job belonging to another tenant is invisible under RLS and reported as
    // 404 rather than 403, so the API does not confirm that someone else's job
    // id is real.
    if (!job) {
      return NextResponse.json({ error: 'Job not found' }, { status: 404 });
    }

    const context = await requireWorkspaceAccess(job.workspace_id);

    if (!DOWNLOADABLE_KINDS.has(job.kind)) {
      return NextResponse.json(
        { error: `A ${job.kind} job does not produce a file` },
        { status: 400 },
      );
    }

    if (job.status !== 'succeeded') {
      return NextResponse.json(
        { error: `This job is ${job.status}; there is nothing to download yet` },
        { status: 409 },
      );
    }

    const result = (job.result ?? {}) as Record<string, unknown>;
    const bucket = typeof result.bucket === 'string' ? result.bucket : null;
    // export_dataset writes export_path, generate_report writes report_path.
    const path =
      typeof result.export_path === 'string'
        ? result.export_path
        : typeof result.report_path === 'string'
          ? result.report_path
          : null;

    if (!bucket || !path || !DOWNLOADABLE_BUCKETS.has(bucket)) {
      return NextResponse.json(
        { error: 'This job recorded no downloadable output' },
        { status: 404 },
      );
    }

    const admin = adminFor(context);
    const { data: signed, error: signError } = await admin.storage
      .from(bucket)
      .createSignedUrl(path, SIGNED_URL_TTL_SECONDS, {
        // Without this the browser saves the object key's last segment, which
        // is a uuid pair. The accountant wants a filename.
        download: path.split('/').pop() ?? 'export',
      });

    if (signError || !signed) {
      return NextResponse.json(
        { error: signError?.message ?? 'Could not create a download link' },
        { status: 502 },
      );
    }

    return NextResponse.json({
      url: signed.signedUrl,
      filename: path.split('/').pop(),
      expiresInSeconds: SIGNED_URL_TTL_SECONDS,
    });
  } catch (error) {
    return handleRouteError(error);
  }
}
