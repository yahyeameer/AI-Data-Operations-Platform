import { NextResponse } from 'next/server';
import { z } from 'zod';

import { handleRouteError } from '@/lib/api';
import { adminFor, requireWorkspaceAccess } from '@/lib/authz';
import { createServerSupabase } from '@/lib/supabase/server';

/**
 * A chat turn, queued.
 *
 * This route used to hold the conversation open: it pushed every stored upload
 * to the parser, then called it and waited for the model to finish a multi-round
 * tool loop before replying. That is what produced *"The analysis is taking
 * longer than this plan allows."* The function's own comment set
 * `maxDuration = 60` and noted that Hobby would not allow more, while
 * `lib/hermes.ts` was willing to wait 300 seconds -- so Vercel always won, killed
 * the function, and returned a plain-text error page that broke `JSON.parse` in
 * the chat component.
 *
 * There is nothing to time out now. The turn becomes a row in `agent_jobs` and
 * this returns a job id; the worker runs the loop on a host with no deadline,
 * and the browser watches the row over Realtime. A model that thinks for four
 * minutes now costs the user four minutes of waiting instead of an error at
 * sixty seconds.
 *
 * The upload push is gone too. The worker reads from Supabase Storage itself, so
 * copying every recent workbook through this function on every single message
 * was buying nothing but latency and a body-size ceiling.
 *
 * Order still matters and is not negotiable: prove workspace access first. The
 * workspace id arrives from the browser, so until `requireWorkspaceAccess` has
 * run it is a claim, not a fact -- and `enqueue_agent_job` then re-checks
 * membership from `auth.uid()` independently, which is the second half of
 * "RLS plus server-side authorization on every path".
 */

const requestSchema = z.object({
  workspaceId: z.string().uuid(),
  message: z.string().trim().min(1, 'Message is empty').max(4000),
  history: z
    .array(
      z.object({
        role: z.enum(['user', 'assistant']),
        content: z.string().max(8000),
      }),
    )
    .max(40)
    .default([]),
});

export async function POST(request: Request) {
  try {
    const body = requestSchema.parse(await request.json());

    const context = await requireWorkspaceAccess(body.workspaceId);
    const supabase = await createServerSupabase();

    const { data: job, error } = await supabase.rpc('enqueue_agent_job', {
      p_workspace_id: body.workspaceId,
      p_kind: 'chat_turn',
      // Only the last dozen turns travel. The worker caps history again on its
      // own side; this keeps the job row from growing without bound when a
      // conversation runs long.
      p_payload: {
        message: body.message,
        history: body.history.slice(-12),
      } as never,
    });

    if (error) {
      return NextResponse.json({ error: error.message }, { status: 403 });
    }

    // Through the admin client: `write_audit` is revoked from `authenticated`
    // precisely so a user can cause an audit entry but never compose one.
    //
    // `enqueue_agent_job` already audited the enqueue, but not the prompt --
    // and a question an accountant asked about a client's numbers belongs in the
    // trail. The reply is not recorded here because it does not exist yet; the
    // worker writes it onto the job and `agent.job.succeeded` closes the trail.
    // An audit row per chat turn that grew by several KB would stop being
    // readable, which is the only property that makes an audit trail worth
    // having.
    await adminFor(context).rpc('write_audit', {
      p_org_id: context.orgId,
      p_workspace_id: context.workspaceId,
      p_action: 'hermes.chat.queued',
      p_entity_type: 'agent_job',
      p_entity_id: (job as { id: string }).id,
      p_metadata: { prompt: body.message },
    });

    return NextResponse.json({ job });
  } catch (error) {
    return handleRouteError(error);
  }
}
