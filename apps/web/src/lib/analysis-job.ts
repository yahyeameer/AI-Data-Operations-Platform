'use client';

import { useEffect, useRef, useState } from 'react';

import { createBrowserSupabase } from '@/lib/supabase/client';

/**
 * Watching a job.
 *
 * The row in `agent_jobs` is the source of truth and Postgres knows the instant
 * it changes, so the browser subscribes rather than asking repeatedly. The
 * answer then appears when it happens instead of up to a poll interval later,
 * and an idle dashboard stops generating requests.
 *
 * RLS governs the subscription exactly as it governs a SELECT -- Realtime
 * evaluates `agent_jobs_select_members` per subscriber -- so this cannot be
 * used to watch another firm's work.
 *
 * Two things are deliberate.
 *
 * **There is no timeout.** The engine may be asleep, cold-starting or
 * mid-redeploy when a job is queued, and none of those are errors. A spinner
 * that gave up after sixty seconds would reintroduce, in the client, precisely
 * the deadline the queue was built to remove.
 *
 * **A slow poll runs alongside the subscription.** A websocket can drop without
 * saying so, and a missed terminal event would leave the user watching a
 * spinner for work that finished. The poll is the floor under the socket, not
 * the mechanism -- it also carries engine liveness, which has no row of its own
 * to subscribe to.
 */

export type FindingTier = 'block' | 'review' | 'routine';

export type Finding = {
  tier: FindingTier;
  key: string;
  title: string;
  detail: string;
  affected_rows: number;
  value_gbp: number | null;
};

export type AnalysisResult = {
  filename?: string;
  rows: number;
  columns: string[];
  excluded_rows: number;
  findings: Finding[];
  blocked: boolean;
  summary: {
    total: number;
    block: number;
    review: number;
    routine: number;
    at_stake_gbp: number;
  };
  duration_ms?: number;
};

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export type AnalysisJob = {
  id: string;
  status: JobStatus;
  progress: { stage?: string; [key: string]: unknown };
  error: string | null;
  result: AnalysisResult | null;
  attempts: number;
  created_at: string;
  finished_at: string | null;
};

type Worker = { id: string; version: string | null; last_seen_at: string };

/**
 * A worker beats every 30 seconds. Three missed beats is offline.
 *
 * Deliberately not one: a single missed beat is a slow network or a long write,
 * and an indicator that flickers "offline" every few minutes teaches people to
 * ignore it entirely.
 */
const WORKER_STALE_AFTER_MS = 90_000;

/** The socket is the mechanism; this is the floor under it. */
const FALLBACK_POLL_MS = 15_000;

export function isEngineAwake(workers: Worker[]): boolean {
  return workers.some(
    (worker) => Date.now() - new Date(worker.last_seen_at).getTime() < WORKER_STALE_AFTER_MS,
  );
}

export function isTerminal(status: JobStatus): boolean {
  return status === 'succeeded' || status === 'failed' || status === 'cancelled';
}

/** What the worker last said it was doing, in the words the user should read. */
const STAGE_LABELS: Record<string, string> = {
  downloading: 'Reading the file',
  reading: 'Interpreting the workbook',
  analysing: 'Checking the figures',
  thinking: 'Thinking',
};

export function stageLabel(job: AnalysisJob | null): string {
  if (!job) return '';
  if (job.status === 'queued') return 'Waiting for the engine';
  const stage = typeof job.progress?.stage === 'string' ? job.progress.stage : '';
  return STAGE_LABELS[stage] ?? 'Working';
}

export function useAnalysisJob(jobId: string | null) {
  const [job, setJob] = useState<AnalysisJob | null>(null);
  const [engineAwake, setEngineAwake] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const finished = useRef(false);

  useEffect(() => {
    if (!jobId) {
      setJob(null);
      setError(null);
      return;
    }

    finished.current = false;
    let poll: ReturnType<typeof setTimeout>;

    /**
     * One read through the route, which returns the job and engine liveness
     * together. Used for the initial state -- the job may already be terminal
     * before the subscription is open -- and as the fallback tick.
     */
    async function refresh() {
      try {
        const response = await fetch(`/api/analyze/${jobId}`, { cache: 'no-store' });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error ?? 'Could not read the analysis');

        setEngineAwake(isEngineAwake(body.workers ?? []));
        setError(null);
        apply(body.job as AnalysisJob);
      } catch (caught) {
        // A failed read is not a failed job -- the row is safe in the database.
        // Surface it without stopping, so a flaky connection does not look like
        // a lost run.
        setError(caught instanceof Error ? caught.message : 'Could not read the analysis');
      }

      if (!finished.current) poll = setTimeout(refresh, FALLBACK_POLL_MS);
    }

    function apply(next: AnalysisJob) {
      setJob(next);
      if (isTerminal(next.status)) {
        finished.current = true;
        clearTimeout(poll);
      }
    }

    const supabase = createBrowserSupabase();
    const channel = supabase
      .channel(`agent_job:${jobId}`)
      .on(
        'postgres_changes',
        { event: 'UPDATE', schema: 'public', table: 'agent_jobs', filter: `id=eq.${jobId}` },
        (payload) => {
          // replica identity is FULL on this table, so an update carries the
          // whole row -- the findings arrive in the same event that reports
          // success, rather than needing a follow-up fetch.
          apply(payload.new as AnalysisJob);
          setError(null);
        },
      )
      .subscribe();

    refresh();

    return () => {
      finished.current = true;
      clearTimeout(poll);
      supabase.removeChannel(channel);
    };
  }, [jobId]);

  return { job, engineAwake, error };
}
