import type { Database } from '@/lib/database.types';

/**
 * Shared vocabulary for the agent, so the dashboard and the API routes agree
 * on what a job is called and when a worker counts as alive.
 *
 * The agent runs on a host the web app does not manage — a VPS, in the
 * deployment this repo documents — and the only thing the two share is the
 * database. That makes liveness a *derived* fact rather than a reported one:
 * nothing can tell the dashboard "the agent has stopped", because a stopped
 * agent cannot send messages. The dashboard has to infer it from silence.
 */

export type AgentJobKind = Database['public']['Enums']['agent_job_kind'];
export type AgentJobStatus = Database['public']['Enums']['agent_job_status'];
export type ChangeConfidence = Database['public']['Enums']['change_confidence'];

/**
 * A worker heartbeats every 30 seconds. Three missed beats is offline.
 *
 * Deliberately not one: a single missed heartbeat is a slow network or a long
 * database write, and an agent that flickers "offline" every few minutes
 * teaches people to ignore the indicator entirely.
 */
export const WORKER_STALE_AFTER_MS = 90_000;

export function isWorkerOnline(lastSeenAt: string | null | undefined): boolean {
  if (!lastSeenAt) return false;
  return Date.now() - new Date(lastSeenAt).getTime() < WORKER_STALE_AFTER_MS;
}

export const JOB_KIND_LABELS: Record<AgentJobKind, string> = {
  parse_workbook: 'Reading the workbook',
  profile_dataset: 'Profiling the data',
  propose_cleaning: 'Working out what to fix',
  apply_cleaning: 'Applying approved changes',
  replay_recipe: 'Replaying the saved recipe',
  query_dataset: 'Answering a question',
  reconcile_sources: 'Reconciling two sources',
  generate_report: 'Writing the report',
  export_dataset: 'Preparing the download',
};

/**
 * The tiers of PRD section 5.1, in the words the accountant sees.
 *
 * "Blocks the run" rather than "low confidence": the tier is about consequence,
 * not about how sure the agent is, and naming it after the consequence is what
 * makes the queue sortable by someone who has thirty seconds.
 */
export const CONFIDENCE_LABELS: Record<ChangeConfidence, string> = {
  high: 'Routine',
  medium: 'Needs review',
  low: 'Blocks the run',
};

export const CONFIDENCE_ORDER: ChangeConfidence[] = ['low', 'medium', 'high'];

export function isTerminal(status: AgentJobStatus): boolean {
  return status === 'succeeded' || status === 'failed' || status === 'cancelled';
}

/** £4,219.00, or an em dash when a change has no monetary weight. */
export function formatMoney(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—';
  const amount = typeof value === 'string' ? Number(value) : value;
  if (Number.isNaN(amount)) return '—';
  return new Intl.NumberFormat('en-GB', {
    style: 'currency',
    currency: 'GBP',
    maximumFractionDigits: 2,
  }).format(amount);
}

/** "2 minutes ago" — the only thing anyone wants to know about a job's age. */
export function formatAge(timestamp: string | null | undefined): string {
  if (!timestamp) return '';
  const seconds = Math.max(0, Math.round((Date.now() - new Date(timestamp).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}
