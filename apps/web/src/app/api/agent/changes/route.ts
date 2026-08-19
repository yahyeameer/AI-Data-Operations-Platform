import { NextResponse } from 'next/server';
import { z } from 'zod';

import { handleRouteError } from '@/lib/api';
import { requireApiUser } from '@/lib/authz';
import { createServerSupabase } from '@/lib/supabase/server';

/**
 * Approving or rejecting a group of proposed changes.
 *
 * This is the human decision the whole product is built around (PRD section 5:
 * the AI proposes, the accountant decides), so it is the one agent-adjacent
 * write a user performs directly rather than by asking the worker.
 *
 * Group keys, not individual ids. Section 5.2 is explicit that a queue asking
 * for 400 separate approvals is slower than doing the work in Excel, so the
 * unit of decision is "yes, normalise the vendor names" — one click covering
 * however many rows that turns out to be.
 *
 * Authorisation lives entirely in `decide_proposed_changes`, which resolves the
 * version to its workspace and calls `has_workspace_access` itself. There is no
 * workspace id in the request to check against: passing one would invite the
 * mismatch where a caller names workspace A and a version belonging to B.
 */

const decideSchema = z.object({
  datasetVersionId: z.string().uuid(),
  groupKeys: z.array(z.string().min(1).max(200)).min(1).max(100),
  approve: z.boolean(),
  note: z.string().max(1000).nullish(),
});

export async function POST(request: Request) {
  try {
    const body = decideSchema.parse(await request.json());

    await requireApiUser();
    const supabase = await createServerSupabase();

    const { data, error } = await supabase.rpc('decide_proposed_changes', {
      p_dataset_version_id: body.datasetVersionId,
      p_group_keys: body.groupKeys,
      p_approve: body.approve,
      p_note: body.note ?? undefined,
    });

    if (error) {
      // The RPC raises insufficient_privilege both for "not yours" and for
      // "does not exist", and the message is already written not to
      // distinguish them.
      return NextResponse.json({ error: error.message }, { status: 403 });
    }

    return NextResponse.json({ decided: data ?? 0 });
  } catch (error) {
    return handleRouteError(error);
  }
}
