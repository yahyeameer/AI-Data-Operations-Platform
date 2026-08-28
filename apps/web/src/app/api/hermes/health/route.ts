import { NextResponse } from 'next/server';

import { handleRouteError } from '@/lib/api';
import { requireApiUser } from '@/lib/authz';
import { createServerSupabase } from '@/lib/supabase/server';

/**
 * Liveness for the sidebar engine indicator.
 *
 * This used to open an HTTP connection to the engine and report whether it
 * answered. That stopped being possible, and stopped being the right question,
 * when the engine moved behind the queue: it has no inbound port to probe, and
 * "can this function reach that host right now" was never quite the fact the
 * operator wanted anyway.
 *
 * Liveness is now a *derived* fact. The worker upserts a heartbeat into
 * `agent_workers` every 30 seconds, and silence is what tells us it is gone --
 * nothing can send a message saying "I have stopped". Three missed beats is
 * offline, deliberately not one: a single missed beat is a slow network or a
 * long write, and an indicator that flickers offline every few minutes teaches
 * people to ignore it.
 *
 * Queue depth comes from the same read, and is the more useful number during an
 * outage: it says how much work is banked up waiting, which is exactly what a
 * user wants to know when their answer has not arrived yet.
 *
 * Always 200. "The engine is down" is a state the dashboard renders honestly;
 * a 503 here would make the sidebar look broken rather than informative.
 */

const WORKER_STALE_AFTER_MS = 90_000;

export async function GET() {
  try {
    await requireApiUser();
    const supabase = await createServerSupabase();

    const [{ data: workers }, { count: queueDepth }] = await Promise.all([
      supabase
        .from('agent_workers')
        .select('id, version, last_seen_at')
        .order('last_seen_at', { ascending: false })
        .limit(5),
      supabase
        .from('agent_jobs')
        .select('id', { count: 'exact', head: true })
        .in('status', ['queued', 'running']),
    ]);

    const live = (workers ?? []).filter(
      (worker) => Date.now() - new Date(worker.last_seen_at).getTime() < WORKER_STALE_AFTER_MS,
    );

    // `configured: false` means no worker has ever checked in, which is the
    // honest reading of an empty table -- nobody has deployed the engine yet.
    // A worker that has run before and gone quiet is a different state, and
    // reports as offline rather than as absent.
    return NextResponse.json({
      configured: (workers ?? []).length > 0,
      reachable: live.length > 0,
      queueDepth: queueDepth ?? 0,
      detail: live.length > 0 ? `${live.length} worker${live.length === 1 ? '' : 's'}` : 'No heartbeat',
      uptime: live[0]?.version ?? undefined,
    });
  } catch (error) {
    return handleRouteError(error);
  }
}
