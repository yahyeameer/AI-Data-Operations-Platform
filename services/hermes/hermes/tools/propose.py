"""
The deviation engine (PRD section 5): turning a profile into decisions a human
can actually make.

Three things this module refuses to do, each for a stated reason.

**It does not apply anything.** A proposal is a row in `proposed_changes` with
an `operation` describing what *would* happen. Applying is a separate job, and
in between sits a person. That separation is the product's legal position
(section 13: "a copilot, not an autonomous accountant"), so it is enforced by
the module boundary rather than by discipline.

**It does not ask the model what to do.** Every proposal here is produced by a
rule reading the profile. The LLM is offered the finished proposal and asked to
write the *rationale* -- the sentence the accountant reads. If the model is
absent, unreachable or wrong, the proposals are identical and the wording is
merely plainer. Section 8: the LLM is never the source of a financial number,
and "which 400 rows get changed" is a financial number.

**It does not present 400 decisions.** Section 5.2 is explicit that a queue
sorted by row count gets abandoned by run 3. Everything is grouped by
`group_key` and weighted by `materiality_gbp`.

The confidence tiers come straight from section 5.1:

    high   -> Auto     applied without asking (whitespace, currency, parentheses)
    medium -> Review   queued for a human (duplicates, fuzzy vendor matches)
    low    -> Block    halts the run (totals mismatch, invariant failure)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .parse import ParsedTable
from .profile import Profile

Confidence = Literal["high", "medium", "low"]

# The closed vocabulary of things a recipe step can do. A proposal naming an
# operation outside this set is dropped rather than stored: the applier
# dispatches on it, and an unknown key would be either a silent no-op or an
# injection point if a model ever gets to influence this field.
KNOWN_OPERATIONS = {
    "normalize_whitespace",
    "normalize_case",
    "map_values",
    "drop_duplicate_rows",
    "coerce_number",
    "normalize_date",
    "review_ambiguous_dates",
    "review_key_conflicts",
    "block_totals_mismatch",
    "review_outliers",
    "review_vat_rate",
}


@dataclass
class Proposal:
    group_key: str
    step_type: str
    title: str
    rationale: str
    operation: dict[str, Any]
    confidence: Confidence
    affected_rows: int
    column_name: str | None = None
    materiality_gbp: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_row(self, workspace_id: str, dataset_version_id: str, job_id: str) -> dict[str, Any]:
        return {
            "workspace_id": workspace_id,
            "dataset_version_id": dataset_version_id,
            "job_id": job_id,
            "group_key": self.group_key,
            "step_type": self.step_type,
            "column_name": self.column_name,
            "title": self.title,
            "rationale": self.rationale,
            "operation": self.operation,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "affected_rows": self.affected_rows,
            "materiality_gbp": self.materiality_gbp,
        }


def _money_at_stake(table: ParsedTable, profile: Profile, source_rows: list[int]) -> float | None:
    """
    What the affected rows are worth.

    Section 5.2 ranks the queue on this, so it has to mean something specific:
    the absolute value of the largest money column across the rows a change
    touches. Absolute, because a £40,000 credit note deserves the same
    attention as a £40,000 invoice, and summing them signed would net a
    material pair down to nothing.
    """
    money_columns = [column.name for column in profile.columns if column.is_money]
    if not money_columns or not source_rows:
        return None

    index_by_source = {source: index for index, source in enumerate(table.source_rows)}
    best = 0.0

    for name in money_columns:
        values = table.columns.get(name, [])
        total = 0.0
        for source in source_rows:
            index = index_by_source.get(source)
            if index is None:
                continue
            value = values[index] if index < len(values) else None
            if isinstance(value, (int, float)):
                total += abs(float(value))
        best = max(best, total)

    return round(best, 2) if best else None


def build_proposals(table: ParsedTable, profile: Profile) -> list[Proposal]:
    """Everything the rule engine can say about this dataset, ranked."""
    proposals: list[Proposal] = []
    signals = profile.signals

    proposals.extend(_auto_tier_proposals(profile))
    proposals.extend(_duplicate_proposals(table, profile, signals))
    proposals.extend(_entity_proposals(table, profile, signals))
    proposals.extend(_date_proposals(table, profile, signals))
    proposals.extend(_totals_proposals(signals))
    proposals.extend(_vat_proposals(table, profile, signals))
    proposals.extend(_outlier_proposals(table, profile, signals))

    proposals = [p for p in proposals if p.operation.get("op") in KNOWN_OPERATIONS]

    # Blocking items first regardless of value -- a totals mismatch is not
    # something to scroll past -- then by money, then by rows.
    tier_order = {"low": 0, "medium": 1, "high": 2}
    proposals.sort(
        key=lambda p: (
            tier_order[p.confidence],
            -(p.materiality_gbp or 0),
            -p.affected_rows,
        )
    )
    return proposals


# -----------------------------------------------------------------------------
# Auto tier: deterministic normalisations with no judgement in them
# -----------------------------------------------------------------------------


def _auto_tier_proposals(profile: Profile) -> list[Proposal]:
    proposals: list[Proposal] = []

    for column in profile.columns:
        if column.inferred_type == "text" and column.whitespace_issues:
            proposals.append(
                Proposal(
                    group_key=f"whitespace:{column.name}",
                    step_type="normalize_whitespace",
                    column_name=column.name,
                    title=f"Trim whitespace in {column.source_header or column.name}",
                    rationale=(
                        f"{column.whitespace_issues} value(s) carry leading, trailing or repeated "
                        f"spaces. Left alone they split what should be one group when the column "
                        f"is used as a key."
                    ),
                    operation={"op": "normalize_whitespace", "column": column.name},
                    confidence="high",
                    affected_rows=column.whitespace_issues,
                    evidence={"examples": column.top_values[:3]},
                )
            )

        if column.inferred_type == "number" and column.number_styles:
            styles = ", ".join(column.number_styles)
            proposals.append(
                Proposal(
                    group_key=f"number:{column.name}",
                    step_type="coerce_number",
                    column_name=column.name,
                    title=f"Read {column.source_header or column.name} as a number",
                    rationale=(
                        f"The column arrives as text using {styles}. Converting once means the "
                        f"totals are arithmetic rather than string comparison, and the original "
                        f"text is kept alongside it."
                    ),
                    operation={
                        "op": "coerce_number",
                        "column": column.name,
                        "styles": column.number_styles,
                    },
                    confidence="high",
                    affected_rows=column.non_null,
                    evidence={
                        "styles": column.number_styles,
                        "total": column.total,
                        "negatives": column.negative_count,
                    },
                )
            )

        if column.inferred_type == "date" and column.date_order:
            proposals.append(
                Proposal(
                    group_key=f"date:{column.name}",
                    step_type="normalize_date",
                    column_name=column.name,
                    title=f"Normalise {column.source_header or column.name} to ISO dates",
                    rationale=(
                        f"Dates are written {column.date_order.upper()} and are converted to "
                        f"YYYY-MM-DD so they sort and compare correctly."
                    ),
                    operation={
                        "op": "normalize_date",
                        "column": column.name,
                        "order": column.date_order,
                    },
                    confidence="high",
                    affected_rows=column.non_null,
                    evidence={"earliest": column.earliest, "latest": column.latest},
                )
            )

    return proposals


# -----------------------------------------------------------------------------
# Review tier
# -----------------------------------------------------------------------------


def _duplicate_proposals(
    table: ParsedTable, profile: Profile, signals: dict[str, Any]
) -> list[Proposal]:
    proposals: list[Proposal] = []

    exact = signals.get("exact_duplicates", {})
    if exact.get("duplicate_rows"):
        rows = [
            source
            for example in exact.get("examples", [])
            for source in example.get("source_rows", [])[1:]
        ]
        proposals.append(
            Proposal(
                group_key="duplicates:exact",
                step_type="drop_duplicate_rows",
                title=f"Remove {exact['duplicate_rows']} exact duplicate row(s)",
                rationale=(
                    f"{exact['group_count']} group(s) of rows are identical in every column. "
                    f"Keeping the first occurrence of each removes the double-count without "
                    f"losing any distinct transaction."
                ),
                operation={"op": "drop_duplicate_rows", "keep": "first", "columns": None},
                # Review rather than auto: an identical row can be a genuine
                # second transaction (two identical £4.50 fees on one day), and
                # only the client knows which.
                confidence="medium",
                affected_rows=exact["duplicate_rows"],
                materiality_gbp=_money_at_stake(table, profile, rows),
                evidence={"groups": exact.get("examples", [])},
            )
        )

    conflicts = signals.get("key_duplicates", {})
    if conflicts.get("group_count"):
        rows = [
            source
            for example in conflicts.get("examples", [])
            for source in example.get("source_rows", [])
        ]
        proposals.append(
            Proposal(
                group_key="duplicates:key_conflict",
                step_type="review_key_conflicts",
                column_name=conflicts.get("chosen_key"),
                title=(
                    f"{conflicts['group_count']} identifier(s) appear on rows that disagree"
                ),
                rationale=(
                    f"The same {conflicts.get('chosen_key')} value appears on rows whose other "
                    f"fields differ. That is not a duplicate to drop -- it is two different "
                    f"records claiming one reference, and the client has to say which is right."
                ),
                operation={
                    "op": "review_key_conflicts",
                    "column": conflicts.get("chosen_key"),
                    "groups": conflicts.get("examples", []),
                },
                confidence="medium",
                affected_rows=len(rows),
                materiality_gbp=_money_at_stake(table, profile, rows),
                evidence={"groups": conflicts.get("examples", [])},
            )
        )

    return proposals


def _entity_proposals(
    table: ParsedTable, profile: Profile, signals: dict[str, Any]
) -> list[Proposal]:
    """
    Vendor normalisation -- section 4's mapping tables, and MVP criterion 9.

    One proposal per column rather than per name group: the accountant is
    deciding "yes, fold supplier spellings" once, and the expandable detail
    shows which. The mapping that results is the thing that stops this
    recurring next month.
    """
    proposals: list[Proposal] = []

    for finding in signals.get("entity_variants", {}).get("columns", []):
        column = finding["column"]
        mapping: dict[str, str] = {}
        for group in finding["groups"]:
            for spelling in group["spellings"]:
                if spelling["value"] != group["suggested"]:
                    mapping[spelling["value"]] = group["suggested"]

        if not mapping:
            continue

        affected_source_rows = [
            table.source_rows[index]
            for index, value in enumerate(table.columns.get(column, []))
            if value in mapping
        ]

        proposals.append(
            Proposal(
                group_key=f"entity:{column}",
                step_type="map_values",
                column_name=column,
                title=f"Merge {finding['group_count']} spelling group(s) in {column}",
                rationale=(
                    f"{len(mapping)} spelling(s) differ only by case, punctuation or a legal-form "
                    f"suffix from another value in the same column. Merging them means one line "
                    f"per supplier in the report instead of several. Each decision is remembered, "
                    f"so next month's file resolves without asking again."
                ),
                operation={"op": "map_values", "column": column, "mapping": mapping},
                # Never auto. "Smith Ltd" and "Smith Holdings Ltd" fold together
                # under any rule loose enough to catch the real duplicates.
                confidence="medium",
                affected_rows=len(affected_source_rows),
                materiality_gbp=_money_at_stake(table, profile, affected_source_rows),
                evidence={"groups": finding["groups"]},
            )
        )

    return proposals


def _date_proposals(
    table: ParsedTable, profile: Profile, signals: dict[str, Any]
) -> list[Proposal]:
    """
    Ambiguous dates.

    The interesting case is when two signals combine: the column has dates that
    could be read either way, *and* the resulting dates scatter across months
    that the rest of the file does not use. One row landing in March in an
    August export is not a coincidence -- it is a DD/MM value read as MM/DD.

    This is the proposal that most justifies the whole profiling step, because
    nothing about the row itself looks wrong.
    """
    coverage = signals.get("date_coverage", {})
    if not coverage.get("checked") or not coverage.get("ambiguous_dates"):
        return []

    column = coverage["column"]
    months: dict[str, int] = coverage.get("months", {})
    if len(months) < 2:
        return []

    dominant, dominant_count = max(months.items(), key=lambda item: item[1])
    outliers = {month: count for month, count in months.items() if month != dominant}
    outlier_rows = sum(outliers.values())

    # Only worth raising when the file is overwhelmingly one period and a
    # handful of rows escape it.
    if dominant_count < 3 or outlier_rows > dominant_count / 2:
        return []

    values = table.columns.get(column, [])
    raw_values = table.columns.get(f"__raw_{column}", [])
    suspects = [
        {
            "source_row": table.source_rows[index],
            "raw": raw_values[index] if index < len(raw_values) else None,
            "read_as": value,
        }
        for index, value in enumerate(values)
        if isinstance(value, str) and value[:7] in outliers
    ]

    return [
        Proposal(
            group_key=f"date_ambiguity:{column}",
            step_type="review_ambiguous_dates",
            column_name=column,
            title=f"{outlier_rows} date(s) fall outside {dominant} and may be month-first",
            rationale=(
                f"Every date in {column} was read as {coverage.get('assumed_order', 'dmy').upper()}, "
                f"which puts {dominant_count} row(s) in {dominant} and {outlier_rows} elsewhere. "
                f"Where both parts are 12 or less the file itself does not say which convention "
                f"it used, so the stray row(s) are more likely mis-read than genuinely out of "
                f"period. Confirm before the figures are used for a return."
            ),
            operation={
                "op": "review_ambiguous_dates",
                "column": column,
                "assumed_order": coverage.get("assumed_order"),
                "dominant_period": dominant,
                "suspects": suspects,
            },
            confidence="medium",
            affected_rows=len(suspects),
            materiality_gbp=_money_at_stake(
                table, profile, [item["source_row"] for item in suspects]
            ),
            evidence={"months": months, "suspects": suspects},
        )
    ]


# -----------------------------------------------------------------------------
# Block tier
# -----------------------------------------------------------------------------


def _totals_proposals(signals: dict[str, Any]) -> list[Proposal]:
    """
    The file's own total against ours (section 5.3's last invariant).

    Blocking, not advisory. If the client's spreadsheet says £10,361.35 and the
    rows add to £10,361.10, then either the parse dropped something or their
    file is wrong -- and filing either number without knowing which is the
    failure mode this product exists to prevent.
    """
    totals = signals.get("declared_totals", {})
    if not totals.get("checked"):
        return []

    failing = [check for check in totals.get("checks", []) if not check["reconciles"]]
    if not failing:
        return []

    lines = ", ".join(
        f"{check['column']} computes to {check['computed']:,.2f} against a declared "
        f"{check['declared']:,.2f} (out by {check['difference']:+,.2f})"
        for check in failing
    )

    return [
        Proposal(
            group_key="invariant:declared_totals",
            step_type="block_totals_mismatch",
            title=f"Totals do not reconcile in {len(failing)} column(s)",
            rationale=(
                f"The file states its own totals on row "
                f"{failing[0]['source_row']}, and they disagree with the sum of the transaction "
                f"rows: {lines}. Either a row was excluded that should not have been, or the "
                f"source spreadsheet does not add up. This has to be resolved before the cleaned "
                f"data is used."
            ),
            operation={"op": "block_totals_mismatch", "checks": failing},
            confidence="low",
            affected_rows=0,
            materiality_gbp=round(max(abs(check["difference"]) for check in failing), 2),
            evidence={"checks": totals.get("checks", []), "summary_rows": totals.get("summary_rows", [])},
        )
    ]


def _vat_proposals(
    table: ParsedTable, profile: Profile, signals: dict[str, Any]
) -> list[Proposal]:
    vat = signals.get("vat_consistency", {})
    if not vat.get("checked") or not vat.get("anomaly_count"):
        return []

    rows = [item["source_row"] for item in vat.get("anomalies", [])]

    return [
        Proposal(
            group_key="vat:rate_anomaly",
            step_type="review_vat_rate",
            column_name=vat.get("vat_column"),
            title=f"{vat['anomaly_count']} row(s) have a VAT rate outside the standard bands",
            rationale=(
                f"{vat['vat_column']} divided by {vat['net_column']} gives a rate that is not "
                f"20%, 5% or 0% on these rows. The distribution across the file is "
                f"{vat.get('rate_distribution')}. Usually a keying error or a foreign supplier; "
                f"either way it is the first thing to check before a return."
            ),
            operation={
                "op": "review_vat_rate",
                "net_column": vat.get("net_column"),
                "vat_column": vat.get("vat_column"),
                "anomalies": vat.get("anomalies", []),
            },
            confidence="medium",
            affected_rows=vat["anomaly_count"],
            materiality_gbp=_money_at_stake(table, profile, rows),
            evidence={"rate_distribution": vat.get("rate_distribution"), "anomalies": vat.get("anomalies", [])},
        )
    ]


def _outlier_proposals(
    table: ParsedTable, profile: Profile, signals: dict[str, Any]
) -> list[Proposal]:
    proposals: list[Proposal] = []

    for finding in signals.get("outliers", {}).get("columns", []):
        rows = [item["source_row"] for item in finding.get("examples", [])]
        proposals.append(
            Proposal(
                group_key=f"outlier:{finding['column']}",
                step_type="review_outliers",
                column_name=finding["column"],
                title=f"{finding['count']} unusually large value(s) in {finding['column']}",
                rationale=(
                    f"These sit outside three interquartile ranges of the rest of the column "
                    f"({finding['bounds']['low']:,.2f} to {finding['bounds']['high']:,.2f}). "
                    f"Not necessarily wrong -- but worth a look before they move a total."
                ),
                operation={
                    "op": "review_outliers",
                    "column": finding["column"],
                    "bounds": finding["bounds"],
                    "examples": finding.get("examples", []),
                },
                confidence="medium",
                affected_rows=finding["count"],
                materiality_gbp=_money_at_stake(table, profile, rows),
                evidence=finding,
            )
        )

    return proposals


def summarise(proposals: list[Proposal]) -> dict[str, Any]:
    """
    The run summary from section 4, computed rather than claimed.

    `automation_rate` counts only the auto tier, because section 5.4 is explicit
    that a number which improves when the tool silently does more is a gameable
    number. Anything a human has to look at counts against it.
    """
    auto = [p for p in proposals if p.confidence == "high"]
    review = [p for p in proposals if p.confidence == "medium"]
    blocking = [p for p in proposals if p.confidence == "low"]

    return {
        "total": len(proposals),
        "auto": len(auto),
        "review": len(review),
        "blocking": len(blocking),
        "review_materiality_gbp": round(
            sum(p.materiality_gbp or 0 for p in review + blocking), 2
        ),
        "blocked": bool(blocking),
        "automation_rate": round(len(auto) / len(proposals), 3) if proposals else 1.0,
        "groups": [
            {
                "group_key": p.group_key,
                "title": p.title,
                "confidence": p.confidence,
                "affected_rows": p.affected_rows,
                "materiality_gbp": p.materiality_gbp,
            }
            for p in proposals
        ],
    }


__all__ = ["Proposal", "build_proposals", "summarise", "KNOWN_OPERATIONS"]
