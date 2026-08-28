'use client';

import {
  type AnalysisJob,
  type Finding,
  type FindingTier,
  stageLabel,
} from '@/lib/analysis-job';
import { ErrorText, ProgressBar, Spinner } from '@/components/ui';

/**
 * What the accountant reads after pressing Analyze.
 *
 * Two rules from the spec are visible here, and each is there because the
 * obvious alternative fails.
 *
 * **Ranked by consequence, then by money.** One unreconciled total outranks two
 * hundred whitespace fixes. The worker orders the findings; this only has to not
 * undo it.
 *
 * **Blocking items cannot be scrolled past.** A totals mismatch is rendered
 * first and separately, because it is the difference between an automation tool
 * and a liability, and that is not something to leave to the reader's diligence.
 */

const TIER_LABELS: Record<FindingTier, string> = {
  block: 'Blocks the run',
  review: 'Needs review',
  routine: 'Routine',
};

/** £4,219.00, or an em dash when a finding has no monetary weight. */
function money(value: number | null): string {
  if (value === null || value === undefined) return '—';
  return new Intl.NumberFormat('en-GB', {
    style: 'currency',
    currency: 'GBP',
    maximumFractionDigits: 2,
  }).format(value);
}

function FindingRow({ finding }: { finding: Finding }) {
  const blocking = finding.tier === 'block';

  return (
    <li
      className={`rounded-lg border px-4 py-3 ${
        blocking ? 'border-red-300 bg-red-50/60' : 'border-slate-200 bg-white'
      }`}
    >
      <div className="flex items-baseline justify-between gap-4">
        <span className={`text-sm font-medium ${blocking ? 'text-red-900' : 'text-slate-900'}`}>
          {finding.title}
        </span>
        <span className="shrink-0 text-sm tabular-nums text-slate-600">
          {money(finding.value_gbp)}
        </span>
      </div>
      <p className="mt-1 text-sm text-slate-600">{finding.detail}</p>
      {finding.affected_rows > 0 && (
        <p className="mt-1 text-xs text-slate-500">
          {finding.affected_rows} row{finding.affected_rows === 1 ? '' : 's'} affected
        </p>
      )}
    </li>
  );
}

export function AnalysisResult({
  job,
  engineAwake,
  pollError,
}: {
  job: AnalysisJob | null;
  engineAwake: boolean;
  pollError: string | null;
}) {
  if (!job) return null;

  // -- still working --------------------------------------------------------

  if (job.status === 'queued' || job.status === 'running') {
    const seconds = Math.max(0, Math.round((Date.now() - new Date(job.created_at).getTime()) / 1000));

    return (
      <div className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
        <div className="flex items-center gap-2 text-sm text-slate-700">
          <Spinner />
          <span>{stageLabel(job)}…</span>
          <span className="tabular-nums text-slate-500">({seconds}s)</span>
        </div>
        <ProgressBar progress={job.status === 'queued' ? 10 : 60} />

        {/* Honest about a cold start rather than silent. The queue means this
            is a wait, not a failure -- so say so, instead of letting the user
            conclude the app is broken. */}
        {!engineAwake && (
          <p className="text-xs text-slate-500">
            The engine is waking up. Your file is queued and will be analyzed as soon as it
            is ready — you can leave this page.
          </p>
        )}
        {job.attempts > 1 && (
          <p className="text-xs text-slate-500">Retrying (attempt {job.attempts}).</p>
        )}
        {pollError && <p className="text-xs text-slate-400">Reconnecting…</p>}
      </div>
    );
  }

  // -- finished badly -------------------------------------------------------

  if (job.status === 'failed' || job.status === 'cancelled') {
    return <ErrorText>{job.error ?? 'The analysis did not finish.'}</ErrorText>;
  }

  const result = job.result;
  if (!result) return null;

  // -- finished -------------------------------------------------------------

  const blocking = result.findings.filter((f) => f.tier === 'block');
  const review = result.findings.filter((f) => f.tier === 'review');
  const routine = result.findings.filter((f) => f.tier === 'routine');

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold text-slate-900">
          {result.filename ?? 'Findings'}
        </h3>
        <p className="text-xs text-slate-500">
          {result.rows} row{result.rows === 1 ? '' : 's'} read
          {result.excluded_rows > 0 && `, ${result.excluded_rows} excluded`}
          {result.summary.at_stake_gbp > 0 && ` · ${money(result.summary.at_stake_gbp)} at stake`}
        </p>
      </div>

      {result.findings.length === 0 && (
        <p className="text-sm text-slate-600">
          Nothing needed fixing — the figures reconcile and no duplicates or spelling
          variants were found.
        </p>
      )}

      {[
        ['block', blocking],
        ['review', review],
        ['routine', routine],
      ].map(([tier, findings]) => {
        const list = findings as Finding[];
        if (list.length === 0) return null;
        return (
          <section key={tier as string} className="space-y-2">
            <h4 className="text-xs font-medium uppercase tracking-wide text-slate-500">
              {TIER_LABELS[tier as FindingTier]}
            </h4>
            <ul className="space-y-2">
              {list.map((finding) => (
                <FindingRow key={finding.key} finding={finding} />
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
