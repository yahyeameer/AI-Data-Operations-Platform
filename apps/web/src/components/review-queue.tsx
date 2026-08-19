'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import {
  type ChangeConfidence,
  CONFIDENCE_LABELS,
  CONFIDENCE_ORDER,
  formatMoney,
} from '@/lib/agent';
import type { Json } from '@/lib/database.types';
import { ErrorText, buttonClass, secondaryButtonClass } from '@/components/ui';

export type ProposedChange = {
  id: string;
  group_key: string;
  step_type: string;
  column_name: string | null;
  title: string;
  rationale: string;
  confidence: ChangeConfidence;
  affected_rows: number;
  materiality_gbp: string | number | null;
  status: string;
  evidence: Json;
};

/**
 * The review queue (PRD section 5.2).
 *
 * Three rules from the spec are visible in this component, and each one is
 * there because the obvious alternative fails.
 *
 * **Ranked by money, not by row count.** One £40,000 unmatched transaction
 * outranks 200 whitespace fixes. The server orders by materiality; this only
 * has to not undo it.
 *
 * **Grouped.** One decision covers however many rows it covers. "Normalise 312
 * dates" is one row here with the detail behind a disclosure, not 312 rows.
 *
 * **Blocking items cannot be scrolled past.** A totals mismatch is rendered
 * first and separately, and the apply button stays disabled while one is still
 * pending. Section 5.3 calls this the difference between an automation tool and
 * a liability, and it is not something to leave to the reader's diligence.
 */
export function ReviewQueue({
  workspaceId,
  datasetVersionId,
  changes,
}: {
  workspaceId: string;
  datasetVersionId: string;
  changes: ProposedChange[];
}) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Re-sorted here rather than trusted from the server. The tier ordering is
  // the one thing in this component that must not silently depend on how the
  // Postgres enum happens to be declared.
  const ordered = [...changes].sort((a, b) => {
    const tier =
      CONFIDENCE_ORDER.indexOf(a.confidence) - CONFIDENCE_ORDER.indexOf(b.confidence);
    if (tier !== 0) return tier;
    return Math.abs(Number(b.materiality_gbp ?? 0)) - Math.abs(Number(a.materiality_gbp ?? 0));
  });

  const pending = ordered.filter((change) => change.status === 'pending');
  const approved = ordered.filter((change) => change.status === 'approved');
  const blocking = pending.filter((change) => change.confidence === 'low');
  const reviewable = pending.filter((change) => change.confidence !== 'low');

  async function decide(groupKeys: string[], approve: boolean) {
    setBusy(groupKeys.join(',') + String(approve));
    setError(null);
    try {
      const response = await fetch('/api/agent/changes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ datasetVersionId, groupKeys, approve }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error ?? 'Could not record that decision');
      router.refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not record that decision');
    } finally {
      setBusy(null);
    }
  }

  async function apply() {
    setBusy('apply');
    setError(null);
    try {
      const response = await fetch('/api/agent/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          workspaceId,
          kind: 'apply_cleaning',
          datasetVersionId,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error ?? 'Could not start the run');
      router.refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not start the run');
    } finally {
      setBusy(null);
    }
  }

  if (changes.length === 0) return null;

  const atStake = pending.reduce(
    (total, change) => total + Math.abs(Number(change.materiality_gbp ?? 0)),
    0,
  );

  return (
    <section>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide opacity-60">
          Review queue
        </h2>
        {pending.length > 0 ? (
          <p className="text-xs opacity-60">
            {pending.length} decision{pending.length === 1 ? '' : 's'} ·{' '}
            {formatMoney(atStake)} affected
          </p>
        ) : null}
      </div>

      <ErrorText>{error}</ErrorText>

      {blocking.length > 0 ? (
        <div className="mb-4 rounded-lg border border-red-500/40 bg-red-500/5">
          <p className="border-b border-red-500/20 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-red-700 dark:text-red-300">
            Blocks the run — resolve before applying anything
          </p>
          <p className="px-4 pt-2 text-xs opacity-70">
            A blocking finding carries no change to apply; it is a question. Either answer
            clears the block and both are recorded in the audit log — the difference is what
            you are on record as having decided.
          </p>
          <ul className="divide-y divide-red-500/15">
            {blocking.map((change) => (
              <ChangeRow
                key={change.id}
                change={change}
                busy={busy}
                onDecide={decide}
              />
            ))}
          </ul>
        </div>
      ) : null}

      {reviewable.length > 0 ? (
        <ul className="divide-y divide-black/10 rounded-lg border border-black/10 dark:divide-white/10 dark:border-white/15">
          {reviewable.map((change) => (
            <ChangeRow key={change.id} change={change} busy={busy} onDecide={decide} />
          ))}
        </ul>
      ) : null}

      {reviewable.length > 1 ? (
        <button
          type="button"
          className="mt-2 text-xs underline opacity-70 hover:opacity-100 disabled:opacity-40"
          disabled={busy !== null}
          onClick={() => decide(reviewable.map((change) => change.group_key), true)}
        >
          Approve all {reviewable.length} reviewable changes
        </button>
      ) : null}

      {approved.length > 0 ? (
        <div className="mt-5 flex flex-wrap items-center gap-3 rounded-lg border border-black/10 px-4 py-3 dark:border-white/15">
          <p className="text-sm">
            {approved.length} change{approved.length === 1 ? '' : 's'} approved and ready to
            apply.
          </p>
          <button
            type="button"
            className={`${buttonClass} ml-auto px-3 py-1.5 text-xs`}
            disabled={busy !== null || blocking.length > 0}
            onClick={apply}
            title={
              blocking.length > 0
                ? 'A blocking issue is still unresolved'
                : 'Writes a new version; the current one is left untouched'
            }
          >
            {busy === 'apply' ? 'Starting…' : 'Apply and create a new version'}
          </button>
          {blocking.length > 0 ? (
            <p className="w-full text-xs text-red-700 dark:text-red-300">
              The blocking issue above has to be approved or rejected first.
            </p>
          ) : (
            <p className="w-full text-xs opacity-60">
              Nothing is overwritten. Applying writes a new dataset version whose parent is
              this one, so the current figures stay available.
            </p>
          )}
        </div>
      ) : null}
    </section>
  );
}

function hasEvidence(evidence: Json): boolean {
  return (
    !!evidence &&
    typeof evidence === 'object' &&
    !Array.isArray(evidence) &&
    Object.keys(evidence).length > 0
  );
}

function ChangeRow({
  change,
  busy,
  onDecide,
}: {
  change: ProposedChange;
  busy: string | null;
  onDecide: (groupKeys: string[], approve: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const money = formatMoney(change.materiality_gbp);
  // A blocking finding has no operation behind it -- "Approve" and "Reject"
  // both simply clear the block, so labelling them that way asks the reader to
  // approve something that does not exist.
  const blocking = change.confidence === 'low';

  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium">{change.title}</p>
          <p className="mt-0.5 text-xs opacity-70">{change.rationale}</p>
          <p className="mt-1 text-xs opacity-50">
            {CONFIDENCE_LABELS[change.confidence]} · {change.affected_rows} row
            {change.affected_rows === 1 ? '' : 's'}
            {money !== '—' ? ` · ${money}` : ''}
            {change.column_name ? ` · ${change.column_name}` : ''}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            className={`${secondaryButtonClass} px-2.5 py-1 text-xs`}
            disabled={busy !== null}
            onClick={() => onDecide([change.group_key], false)}
            title={
              blocking
                ? 'Records that you do not accept this finding, and clears the block'
                : 'Leave the data as it is'
            }
          >
            {blocking ? 'Not an issue' : 'Reject'}
          </button>
          <button
            type="button"
            className={`${buttonClass} px-2.5 py-1 text-xs`}
            disabled={busy !== null}
            onClick={() => onDecide([change.group_key], true)}
            title={
              blocking
                ? 'Records that you have investigated and accepted this, and clears the block'
                : 'Apply this change when the run goes ahead'
            }
          >
            {blocking ? 'Reviewed, continue' : 'Approve'}
          </button>
        </div>
      </div>

      {hasEvidence(change.evidence) ? (
        <div className="mt-2">
          <button
            type="button"
            className="text-xs underline opacity-60 hover:opacity-100"
            onClick={() => setOpen(!open)}
          >
            {open ? 'Hide evidence' : 'Show evidence'}
          </button>
          {open ? (
            // Raw evidence, deliberately. Section 7's promise is that a number
            // can be traced, and a curated summary of the evidence is the thing
            // the accountant would have to take on trust.
            <pre className="mt-2 max-h-64 overflow-auto rounded border border-black/10 bg-black/5 p-3 text-[11px] leading-relaxed dark:border-white/15 dark:bg-white/5">
              {JSON.stringify(change.evidence, null, 2)}
            </pre>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
