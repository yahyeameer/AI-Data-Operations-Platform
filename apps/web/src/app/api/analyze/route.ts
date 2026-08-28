import { NextResponse } from 'next/server';
import { z } from 'zod';

import { handleRouteError } from '@/lib/api';
import { requireWorkspaceAccess } from '@/lib/authz';
import { createServerSupabase } from '@/lib/supabase/server';

/**
 * Ask the engine to analyse an upload.
 *
 * Note what this route does *not* do: it never contacts the parser. It writes a
 * row to `agent_jobs` and returns. That is the whole fix for the timeout this
 * phase exists to remove -- the previous design called the parser over HTTP and
 * waited, which put Vercel's function cap, the parser's cold start and the
 * analysis itself on one clock, and the shortest of them decided whether the
 * user saw a result or an error.
 *
 * Enqueuing is a database write that returns in milliseconds whether the engine
 * is up, down or mid-restart. A sleeping worker now delays the answer instead of
 * failing the request.
 *
 * The enqueue deliberately runs through the *user's* client rather than the
 * service role. `enqueue_agent_job` is SECURITY DEFINER and re-checks membership
 * from `auth.uid()`, so routing the call through the signed-in session means the
 * database authorises it independently of this route having got it right.
 * `requireWorkspaceAccess` is the other half of that pair.
 */

const requestSchema = z.object({
  workspaceId: z.string().uuid(),
  uploadId: z.string().uuid(),
  datasetId: z.string().uuid().nullish(),
  // What the accountant wants done with this file, in their own words. Carried
  // on the job so the worker can read it; it does not change what the engine is
  // allowed to touch.
  instructions: z.string().trim().max(4000).nullish(),
});

export async function POST(request: Request) {
  try {
    const body = requestSchema.parse(await request.json());

    await requireWorkspaceAccess(body.workspaceId);
    const supabase = await createServerSupabase();

    const { data, error } = await supabase.rpc('enqueue_agent_job', {
      p_workspace_id: body.workspaceId,
      p_kind: 'analyze_workbook',
      p_raw_upload_id: body.uploadId,
      p_dataset_id: body.datasetId ?? undefined,
      p_payload: (body.instructions ? { instructions: body.instructions } : {}) as never,
    });

    if (error) {
      // The RPC raises insufficient_privilege both for "not yours" and for
      // "does not exist", and the message is already written not to distinguish
      // them.
      return NextResponse.json({ error: error.message }, { status: 403 });
    }

    return NextResponse.json({ job: data });
  } catch (error) {
    return handleRouteError(error);
  }
}
