'use client';

import { useEffect, useRef, useState } from 'react';

/**
 * Watching an analysis.
 *
 * The browser polls because the work no longer happens inside a request. That
 * is the whole point of the change: the engine may be asleep, cold-starting or
 * mid-redeploy when the job is queued, and none of those are errors any more --
 * they are reasons the answer takes a little longer to arrive.
 *
 * So this hook deliberately has no timeout of its own. A spinner that gives up
 * after 60 seconds would reintroduce, in the client, exactly the deadline the
 * queue was built to remove. It stops when the job reaches a terminal state and
 * not before; the user can always navigate away.
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
};

export function stageLabel(job: AnalysisJob | null): string {
  if (!job) return '';
  if (job.status === 'queued') return 'Waiting for the engine';
  const stage = typeof job.progress?.stage === 'string' ? job.progress.stage : '';
  return STAGE_LABELS[stage] ?? 'Analyzing';
}

export function useAnalysisJob(jobId: string | null, intervalMs = 2000) {
  const [job, setJob] = useState<AnalysisJob | null>(null);
  const [engineAwake, setEngineAwake] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Held in a ref so the polling effect does not re-subscribe every tick.
  const stopped = useRef(false);

  useEffect(() => {
    if (!jobId) {
      setJob(null);
      setError(null);
      return;
    }

    stopped.current = false;
    let timer: ReturnType<typeof setTimeout>;

    async function poll() {
      if (stopped.current) return;

      try {
        const response = await fetch(`/api/analyze/${jobId}`, { cache: 'no-store' });
        const body = await response.json();

        if (!response.ok) throw new Error(body.error ?? 'Could not read the analysis');

        setJob(body.job);
        setEngineAwake(isEngineAwake(body.workers ?? []));
        setError(null);

        if (isTerminal(body.job.status)) return;
      } catch (caught) {
        // A failed poll is not a failed analysis -- the job is safe in the
        // database and the next tick will pick it up. Surface it without
        // stopping, so a flaky connection does not look like a lost run.
        setError(caught instanceof Error ? caught.message : 'Could not read the analysis');
      }

      timer = setTimeout(poll, intervalMs);
    }

    poll();

    return () => {
      stopped.current = true;
      clearTimeout(timer);
    };
  }, [jobId, intervalMs]);

  return { job, engineAwake, error };
}
