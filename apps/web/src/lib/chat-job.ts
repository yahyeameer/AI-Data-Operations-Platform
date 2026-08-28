'use client';

import { createBrowserSupabase } from '@/lib/supabase/client';

/**
 * Waiting for a queued chat turn to produce its answer.
 *
 * The reply used to be the body of the POST that asked the question, which is
 * what made a slow model indistinguishable from a broken app: the browser, the
 * Vercel function and the model were all on one clock, and the shortest of them
 * decided what the user saw.
 *
 * Now the question becomes a row and the answer is written onto it. This watches
 * that row over Realtime and resolves when it reaches a terminal state.
 *
 * There is no timeout, on purpose. A turn that thinks for four minutes should
 * cost four minutes of waiting; a client-side deadline would simply reinvent the
 * failure this design removed. What the caller gets instead is a promise that
 * settles when the work actually settles.
 */

export type Download = {
  filename: string;
  url: string;
  format?: string;
  rows?: number;
};

export type ChatAnswer = {
  reply: string;
  downloads: Download[];
};

type JobRow = {
  status: string;
  error: string | null;
  result: { reply?: string; downloads?: Download[] } | null;
};

/** A socket can drop silently; this is the floor under it, not the mechanism. */
const FALLBACK_POLL_MS = 15_000;

const TERMINAL = ['succeeded', 'failed', 'cancelled'];

function settle(row: JobRow): ChatAnswer {
  if (row.status !== 'succeeded') {
    throw new Error(row.error ?? 'The agent could not answer that.');
  }
  return {
    reply: row.result?.reply ?? '',
    downloads: Array.isArray(row.result?.downloads) ? row.result!.downloads! : [],
  };
}

export function waitForReply(jobId: string): Promise<ChatAnswer> {
  return new Promise<ChatAnswer>((resolve, reject) => {
    const supabase = createBrowserSupabase();
    let done = false;
    let poll: ReturnType<typeof setTimeout>;

    function finish(row: JobRow) {
      if (done) return;
      done = true;
      clearTimeout(poll);
      supabase.removeChannel(channel);
      try {
        resolve(settle(row));
      } catch (error) {
        reject(error);
      }
    }

    // The fallback read. It also covers the race where the worker finishes
    // before the subscription has joined -- a fast turn against a warm engine
    // can beat the socket, and an answer that arrived before anyone was
    // listening must not strand the caller.
    async function check() {
      if (done) return;
      try {
        const response = await fetch(`/api/analyze/${jobId}`, { cache: 'no-store' });
        const body = await response.json();
        if (response.ok && body.job && TERMINAL.includes(body.job.status)) {
          finish(body.job as JobRow);
          return;
        }
      } catch {
        // A failed read is not a failed turn; the row is safe in the database.
        // Try again on the next tick rather than giving up on the answer.
      }
      if (!done) poll = setTimeout(check, FALLBACK_POLL_MS);
    }

    const channel = supabase
      .channel(`chat_job:${jobId}`)
      .on(
        'postgres_changes',
        { event: 'UPDATE', schema: 'public', table: 'agent_jobs', filter: `id=eq.${jobId}` },
        (payload) => {
          const row = payload.new as JobRow;
          // Ignore the claim, which flips the row to 'running' before the model
          // has said anything.
          if (TERMINAL.includes(row.status)) finish(row);
        },
      )
      .subscribe();

    check();
  });
}
