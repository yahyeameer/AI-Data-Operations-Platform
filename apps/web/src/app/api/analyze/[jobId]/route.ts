import { NextResponse } from 'next/server';
import { z } from 'zod';

import { handleRouteError } from '@/lib/api';
import { requireApiUser } from '@/lib/authz';
import { createServerSupabase } from '@/lib/supabase/server';

/**
 * Poll one job.
 *
 * Read through the user's RLS-bound client, so `agent_jobs_select_members` is
 * what decides whether this job is visible. There is no workspace id in the
 * request to check against, and asking for one would only invite the mismatch
 * where a caller names workspace A and a job belonging to B -- the policy
 * already resolves the job to its workspace and asks the same question.
 *
 * A job that belongs to another tenant returns 404 rather than 403: the API must
 * not confirm that someone else's job id is real.
 */

const paramsSchema = z.object({ jobId: z.string().uuid() });

export async function GET(_request: Request, context: { params: Promise<{ jobId: string }> }) {
  try {
    const { jobId } = paramsSchema.parse(await context.params);

    await requireApiUser();
    const supabase = await createServerSupabase();

    const { data: job, error } = await supabase
      .from('agent_jobs')
      .select(
        'id, kind, status, progress, error, result, attempts, max_attempts, ' +
          'created_at, started_at, finished_at',
      )
      .eq('id', jobId)
      .maybeSingle();

    if (error) {
      return NextResponse.json({ error: 'Could not read that job' }, { status: 500 });
    }
    if (!job) {
      return NextResponse.json({ error: 'Job not found' }, { status: 404 });
    }

    // Liveness travels with the job so the UI needs one request per poll rather
    // than two, and can tell "the engine is asleep" apart from "the engine is
    // working". Reading the worker rows is cheap -- there is one per host, and
    // the table carries no customer data.
    const { data: workers } = await supabase
      .from('agent_workers')
      .select('id, version, last_seen_at')
      .order('last_seen_at', { ascending: false })
      .limit(5);

    return NextResponse.json({ job, workers: workers ?? [] });
  } catch (error) {
    return handleRouteError(error);
  }
}
