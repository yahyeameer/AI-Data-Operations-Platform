"""
Recipes: turning one approved session into every future month (PRD section 4).

The PRD is blunt about why this matters. Criteria 6 and 9 "are the product. If
those two work, everything else is a matter of polish." Cleaning one file well
is a tool; doing it again next month without asking the same questions is the
thing worth paying for.

Three pieces here.

**Capture** takes the operations an accountant approved and writes them down as
an ordered, versioned step list. The one transformation it performs is
important: an inline `map_values` mapping becomes a reference to a *mapping
table*. Section 4 insists on that — a mapping frozen into recipe v3 means every
new supplier needs a recipe v4, and the automation rate never climbs past the
first month's vocabulary.

**Replay** runs those steps against a new month's file and reports what it
could not handle. Anything unresolved becomes a deviation rather than a guess.

**Invariants** (section 5.3) run *after* the steps and can fail a run that had
zero deviations. This is the silent-failure guard: a recipe that executes
perfectly against a file whose meaning has changed produces confidently wrong
numbers, and nothing else in the system would notice.

Nothing here talks to Supabase or to a model. Replay is a pure function of
(steps, table, mappings), which is what makes it testable against two fixtures
with no database at all.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Literal

from .clean import CleanResult, Table, apply_operations
from .profile import Profile
from .values import entity_key, normalize_text

Severity = Literal["auto", "review", "block"]

# How close an unknown value has to be to a known one before it is worth
# offering as a suggestion. Tuned deliberately high: a wrong suggestion that
# gets accepted in a hurry is worse than no suggestion, because it enters the
# mapping table and silently applies to every future month.
FUZZY_SUGGEST_THRESHOLD = 0.86

# Steps whose whole job is to ask a person something. They carry no
# transformation, so replaying them is a no-op -- but they stay in the recipe
# because the recipe is a record of what was decided, including the decisions
# that were "look at this".
REVIEW_STEPS = {
    "review_ambiguous_dates",
    "review_key_conflicts",
    "review_outliers",
    "review_vat_rate",
    "block_totals_mismatch",
}


@dataclass
class Deviation:
    """Something the recipe could not do on its own."""

    type: str
    severity: Severity
    group_key: str
    title: str
    detail: str = ""
    column_name: str | None = None
    source_value: str | None = None
    suggested_value: str | None = None
    affected_rows: int = 0
    materiality_gbp: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "severity": self.severity,
            "group_key": self.group_key,
            "title": self.title,
            "detail": self.detail,
            "column_name": self.column_name,
            "source_value": self.source_value,
            "suggested_value": self.suggested_value,
            "affected_rows": self.affected_rows,
            "materiality_gbp": self.materiality_gbp,
            "evidence": self.evidence,
        }


@dataclass
class ReplayResult:
    cleaned: CleanResult
    deviations: list[Deviation]
    rows_processed: int
    rows_matched: int
    auto_corrections: int
    # Mapping keys that resolved something, so their hit counts can be bumped.
    mapping_hits: dict[str, list[str]] = field(default_factory=dict)
    invariants: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(deviation.severity == "block" for deviation in self.deviations)

    @property
    def needs_review(self) -> bool:
        return any(deviation.severity == "review" for deviation in self.deviations)

    @property
    def automation_rate(self) -> float:
        """
        Section 5.4 warns that this metric is gameable — a recipe that
        auto-applies wrong transformations scores 100%. Measured here as the
        share of *rows* that passed through without needing a person, which at
        least cannot be improved by doing more damage quietly.
        """
        if self.rows_processed == 0:
            return 1.0
        touched = sum(deviation.affected_rows for deviation in self.deviations
                      if deviation.severity != "auto")
        return round(max(0.0, 1.0 - touched / self.rows_processed), 4)

    def summary(self) -> dict[str, Any]:
        by_severity: dict[str, int] = {}
        for deviation in self.deviations:
            by_severity[deviation.severity] = by_severity.get(deviation.severity, 0) + 1

        return {
            "rows_processed": self.rows_processed,
            "rows_matched": self.rows_matched,
            "auto_corrections": self.auto_corrections,
            "deviations": len(self.deviations),
            "by_severity": by_severity,
            "review_materiality_gbp": round(
                sum(
                    deviation.materiality_gbp or 0
                    for deviation in self.deviations
                    if deviation.severity != "auto"
                ),
                2,
            ),
            "automation_rate": self.automation_rate,
            "invariants": self.invariants,
            "operations": [operation.to_dict() for operation in self.cleaned.operations],
        }


# -----------------------------------------------------------------------------
# Capture
# -----------------------------------------------------------------------------


def capture_steps(
    approved: list[dict[str, Any]],
    mapping_table_ids: dict[str, str] | None = None,
    learned_from_run: str | None = None,
) -> list[dict[str, Any]]:
    """
    Turn approved operations into a recipe step list.

    `approved` arrives in the order the operations were applied, and that order
    is preserved rather than re-derived — trimming whitespace before mapping
    vendor names finds matches the reverse order misses, and the run that was
    approved is the one that should be repeatable.

    `mapping_table_ids` maps a column name to the workspace mapping table that
    should own its vocabulary. Where one exists, an inline mapping is replaced
    by a reference to it: the values themselves have already been written to
    that table, and the step now reads whatever the table knows *at replay
    time* rather than what it knew in month one.
    """
    steps: list[dict[str, Any]] = []
    mapping_table_ids = mapping_table_ids or {}

    for index, item in enumerate(approved, start=1):
        operation = dict(item.get("operation") or {})
        op = operation.get("op")
        if not op:
            continue

        column = operation.get("column")
        params = {key: value for key, value in operation.items() if key != "op"}

        if op == "map_values" and column and column in mapping_table_ids:
            # The mapping moves out of the step and into the growable table.
            params.pop("mapping", None)
            params["mapping_table_id"] = mapping_table_ids[column]

        step: dict[str, Any] = {
            "id": f"step_{index:02d}",
            "op": op,
            "params": params,
            # Section 5.1's tiers, carried from what the accountant approved.
            "confidence_tier": _tier_for(item.get("confidence")),
            "on_ambiguous": "review",
            "enabled": True,
        }
        if learned_from_run:
            step["learned_from_run"] = learned_from_run
        if item.get("group_key"):
            step["group_key"] = item["group_key"]

        steps.append(step)

    return steps


def build_vocabulary_entries(
    values: list[Any], mapping: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """
    The mapping-table rows a captured month should leave behind.

    The subtlety that is easy to get wrong: a mapping table holding only the
    *corrections* is not a vocabulary. August merged two supplier spellings, so
    a corrections-only table knows about exactly those two — and next month
    every supplier that was always spelled correctly looks brand new, which
    buries the two that genuinely are.

    So the table gets both: the merges a person approved, and an identity entry
    for every value that survived cleaning. After that, "not in the table"
    means "this workspace has never seen this", which is the question actually
    worth asking.

    The two are distinguished by `confirmed`. A merge is a decision somebody
    made; an identity entry is an observation. Section 5.4 asks for automation
    to be measured honestly, and conflating the two would overstate it.
    """
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for source, canonical in (mapping or {}).items():
        key = normalize_text(source).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "source_key": key,
                "source_value": source,
                "canonical_value": canonical,
                "confirmed": True,
            }
        )

    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        key = normalize_text(value).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "source_key": key,
                "source_value": value,
                "canonical_value": value,
                "confirmed": False,
            }
        )

    return entries


def _tier_for(confidence: str | None) -> str:
    return {"high": "auto", "medium": "review", "low": "block"}.get(confidence or "", "review")


def default_invariants(profile: Profile) -> list[dict[str, Any]]:
    """
    The post-run checks a recipe carries (section 5.3).

    Derived from the month the recipe was learned in, because that is the only
    baseline available at capture time. They are tolerances, not equalities:
    a monthly file that never varies is not a real monthly file.
    """
    invariants: list[dict[str, Any]] = [
        {
            "id": "row_count",
            "type": "row_count_within",
            "baseline": profile.row_count,
            # Wide, because a genuine month can be half or double the last. The
            # check is for a file that has become a different kind of thing,
            # not for a quiet month.
            "tolerance_pct": 60,
            "severity": "review",
        },
        {
            "id": "required_columns",
            "type": "columns_present",
            "columns": [
                column.name
                for column in profile.columns
                if column.inferred_type != "empty" and column.non_null > 0
            ],
            "severity": "block",
        },
    ]

    for column in profile.columns:
        if column.is_money and column.total is not None:
            invariants.append(
                {
                    "id": f"total_{column.name}",
                    "type": "total_within",
                    "column": column.name,
                    "baseline": column.total,
                    "tolerance_pct": 200,
                    "severity": "review",
                }
            )
        if column.inferred_type in {"number", "date"}:
            invariants.append(
                {
                    "id": f"type_{column.name}",
                    "type": "column_type",
                    "column": column.name,
                    "expected": column.inferred_type,
                    "severity": "block",
                }
            )

    return invariants


# -----------------------------------------------------------------------------
# Replay
# -----------------------------------------------------------------------------


def replay(
    table: Any,
    steps: list[dict[str, Any]],
    profile: Profile,
    mappings: dict[str, dict[str, str]] | None = None,
    money_column: str | None = None,
    expected_columns: list[str] | None = None,
) -> ReplayResult:
    """
    Run a recipe against a new month's parsed table.

    `mappings` is keyed by mapping_table_id, each holding source_key ->
    canonical_value as the table currently stands. Passing it in rather than
    fetching it keeps this function pure and lets a test drive the "the human
    resolved it last month" case directly.

    `expected_columns` is the column list the recipe was learned against. It
    has to be supplied rather than inferred from the steps: a column that
    needed no cleaning is touched by no step, and inferring the expectation
    from step parameters would report every such column as new every month.
    """
    mappings = mappings or {}
    deviations: list[Deviation] = []
    mapping_hits: dict[str, list[str]] = {}

    available = set(table.columns)
    operations: list[dict[str, Any]] = []

    for step in steps:
        if not step.get("enabled", True):
            continue

        op = step["op"]
        params = dict(step.get("params") or {})
        column = params.get("column")

        if op in REVIEW_STEPS:
            # Nothing to apply. The step's presence says a person looked at
            # this class of thing once; it does not say they must again.
            continue

        if column and column not in available:
            deviations.append(
                Deviation(
                    type="missing_column",
                    severity="block",
                    group_key=f"missing_column:{column}",
                    title=f"The file no longer has a {column} column",
                    detail=(
                        f"Step {step['id']} ({op}) works on {column}, which is not in this "
                        f"month's file. Either the export changed or a column was renamed — "
                        f"replaying without it would produce a result that looks complete and "
                        f"is not."
                    ),
                    column_name=column,
                    evidence={"step": step["id"], "available_columns": sorted(available)},
                )
            )
            continue

        if op == "map_values":
            operation, step_deviations, hits = _resolve_mapping_step(
                table, step, params, mappings, profile, money_column
            )
            deviations.extend(step_deviations)
            if hits:
                mapping_hits.setdefault(params.get("mapping_table_id", ""), []).extend(hits)
            if operation:
                operations.append(operation)
            continue

        operations.append({"op": op, **params})

    # New columns are worth mentioning but never worth blocking on: an extra
    # column changes nothing about the steps that already ran.
    # Taken from the table rather than the profile: the table is what the steps
    # actually ran against, and a profile is a derived view that can lag it.
    present = {
        name
        for name in table.columns
        if not name.startswith("__raw_") and name != "__source_row"
    }
    unexpected = sorted(present - set(expected_columns)) if expected_columns else []
    if unexpected:
        deviations.append(
            Deviation(
                type="new_column",
                severity="review",
                group_key="new_column",
                title=f"{len(unexpected)} column(s) this recipe has not seen before",
                detail=(
                    f"{', '.join(unexpected)} appeared in this month's file. Nothing was done "
                    f"with them. If they matter, add a step."
                ),
                evidence={"columns": unexpected},
            )
        )

    result = apply_operations(table, operations)

    auto_corrections = sum(
        operation.rows_changed + operation.rows_removed for operation in result.operations
    )
    for operation in result.operations:
        for warning in operation.warnings:
            deviations.append(
                Deviation(
                    type="step_failed",
                    severity="review",
                    group_key=f"step_warning:{operation.op}",
                    title=f"{operation.op} reported a problem",
                    detail=warning,
                    column_name=operation.column,
                    evidence={"op": operation.op},
                )
            )

    rows_touched = sum(
        deviation.affected_rows for deviation in deviations if deviation.severity != "auto"
    )

    return ReplayResult(
        cleaned=result,
        deviations=deviations,
        rows_processed=table.row_count,
        rows_matched=max(0, table.row_count - rows_touched),
        auto_corrections=auto_corrections,
        mapping_hits={key: sorted(set(value)) for key, value in mapping_hits.items() if key},
    )


def _resolve_mapping_step(
    table: Any,
    step: dict[str, Any],
    params: dict[str, Any],
    mappings: dict[str, dict[str, str]],
    profile: Profile,
    money_column: str | None,
) -> tuple[dict[str, Any] | None, list[Deviation], list[str]]:
    """
    Turn a mapping-table reference into a concrete mapping for this month.

    This is where criterion 9 pays off. Values the table already knows resolve
    silently. Values it does not are *not* guessed: each becomes a deviation,
    carrying a suggestion when it is close enough to something known to be
    worth offering. Resolving one writes it back to the table, so it resolves
    silently next month and never appears here again.
    """
    column = params.get("column")
    table_id = params.get("mapping_table_id")
    deviations: list[Deviation] = []
    hits: list[str] = []

    # An inline mapping is a recipe captured before mapping tables existed, or
    # one edited by hand. Honour it rather than failing.
    known: dict[str, str] = dict(params.get("mapping") or {})
    vocabulary: dict[str, str] = dict(mappings.get(table_id or "", {}))

    if not column or column not in table.columns:
        return None, deviations, hits

    values = table.columns[column]
    resolved: dict[str, str] = {}

    # The canonical values already in use, for suggesting near misses against.
    canonical = sorted({*vocabulary.values(), *known.values()})
    canonical_by_key = {entity_key(value): value for value in canonical}

    counts: dict[str, int] = {}
    for value in values:
        if isinstance(value, str) and value:
            counts[value] = counts.get(value, 0) + 1

    for raw, count in sorted(counts.items(), key=lambda item: -item[1]):
        key = normalize_text(raw).lower()

        if raw in known:
            resolved[raw] = known[raw]
            continue
        if key in vocabulary:
            resolved[raw] = vocabulary[key]
            hits.append(key)
            continue
        if raw in canonical:
            # Already canonical; nothing to do.
            continue

        # Does it fold onto something we know? That is the ambiguous-match case
        # section 5.1 puts firmly in the review tier.
        folded = entity_key(raw)
        suggestion = canonical_by_key.get(folded)

        if suggestion is None and canonical:
            close = difflib.get_close_matches(raw, canonical, n=1, cutoff=FUZZY_SUGGEST_THRESHOLD)
            suggestion = close[0] if close else None

        rows = [index for index, value in enumerate(values) if value == raw]
        materiality = _materiality(table, profile, rows, money_column)

        if suggestion:
            deviations.append(
                Deviation(
                    type="ambiguous_match",
                    severity="review",
                    group_key=f"ambiguous:{column}:{key}",
                    title=f"“{raw}” looks like “{suggestion}”",
                    detail=(
                        f"{count} row(s) use “{raw}”, which is close to “{suggestion}” but not "
                        f"identical. Confirming it here records the decision, so next month's "
                        f"file resolves it without asking."
                    ),
                    column_name=column,
                    source_value=raw,
                    suggested_value=suggestion,
                    affected_rows=count,
                    materiality_gbp=materiality,
                    evidence={"occurrences": count, "candidates": canonical[:10]},
                )
            )
        else:
            deviations.append(
                Deviation(
                    type="unmapped_value",
                    severity="review",
                    group_key=f"unmapped:{column}:{key}",
                    title=f"“{raw}” is new in {column}",
                    detail=(
                        f"{count} row(s) use a value this workspace has not seen before. If it "
                        f"is a new {column}, accept it as-is; if it is another spelling of an "
                        f"existing one, map it and it will resolve automatically from now on."
                    ),
                    column_name=column,
                    source_value=raw,
                    affected_rows=count,
                    materiality_gbp=materiality,
                    evidence={"occurrences": count},
                )
            )

    if not resolved:
        return None, deviations, hits

    return {"op": "map_values", "column": column, "mapping": resolved}, deviations, hits


def _materiality(
    table: Any, profile: Profile, row_indices: list[int], money_column: str | None
) -> float | None:
    """What the affected rows are worth, for section 5.2's ranking."""
    if money_column is None:
        money = [column.name for column in profile.columns if column.is_money]
        money_column = money[0] if money else None
    if money_column is None or money_column not in table.columns:
        return None

    values = table.columns[money_column]
    total = sum(
        abs(float(values[index]))
        for index in row_indices
        if index < len(values) and isinstance(values[index], (int, float))
    )
    return round(total, 2) if total else None


# -----------------------------------------------------------------------------
# Post-run invariants (section 5.3)
# -----------------------------------------------------------------------------


def check_invariants(
    invariants: list[dict[str, Any]], result: CleanResult, profile: Profile
) -> tuple[list[dict[str, Any]], list[Deviation]]:
    """
    Run the checks that fire *after* the steps.

    "A recipe matching 100% of rows is not evidence of correctness." These are
    the guard against a file that kept its shape and changed its meaning — a
    column repurposed, a date convention flipped, a subtotal row that now looks
    like a transaction. A run with zero deviations can still fail here, and
    that is the entire point.
    """
    outcomes: list[dict[str, Any]] = []
    deviations: list[Deviation] = []

    columns = {name for name in result.columns if not name.startswith("__raw_")}

    for invariant in invariants:
        kind = invariant.get("type")
        severity: Severity = invariant.get("severity", "review")  # type: ignore[assignment]
        outcome: dict[str, Any] = {"id": invariant.get("id"), "type": kind, "passed": True}

        if kind == "row_count_within":
            baseline = float(invariant.get("baseline") or 0)
            tolerance = float(invariant.get("tolerance_pct") or 50) / 100
            actual = result.row_count
            low, high = baseline * (1 - tolerance), baseline * (1 + tolerance)
            outcome.update({"baseline": baseline, "actual": actual})
            if baseline > 0 and not (low <= actual <= high):
                outcome["passed"] = False
                deviations.append(
                    Deviation(
                        type="invariant_failure",
                        severity=severity,
                        group_key="invariant:row_count",
                        title=f"Row count is {actual}, against {baseline:.0f} last time",
                        detail=(
                            f"Outside the expected range of {low:.0f} to {high:.0f}. A monthly "
                            f"file that changes size this much is usually a different export, "
                            f"a partial one, or one covering a different period."
                        ),
                        evidence=outcome,
                    )
                )

        elif kind == "columns_present":
            expected = set(invariant.get("columns") or [])
            missing = sorted(expected - columns)
            outcome.update({"missing": missing})
            if missing:
                outcome["passed"] = False
                deviations.append(
                    Deviation(
                        type="missing_column",
                        severity=severity,
                        group_key="invariant:columns_present",
                        title=f"{len(missing)} expected column(s) are absent",
                        detail=f"The recipe expects {', '.join(missing)}.",
                        evidence=outcome,
                    )
                )

        elif kind == "total_within":
            column = invariant.get("column")
            baseline = float(invariant.get("baseline") or 0)
            tolerance = float(invariant.get("tolerance_pct") or 100) / 100
            values = result.columns.get(column, [])
            actual = sum(float(v) for v in values if isinstance(v, (int, float)))
            outcome.update({"column": column, "baseline": baseline, "actual": round(actual, 2)})
            if baseline != 0:
                low = baseline * (1 - tolerance)
                high = baseline * (1 + tolerance)
                if not (min(low, high) <= actual <= max(low, high)):
                    outcome["passed"] = False
                    deviations.append(
                        Deviation(
                            type="invariant_failure",
                            severity=severity,
                            group_key=f"invariant:total:{column}",
                            title=f"{column} totals {actual:,.2f}, against {baseline:,.2f} last time",
                            detail=(
                                "A swing this large is either a real change in the business or a "
                                "parsing problem, and the two look identical in a spreadsheet."
                            ),
                            column_name=column,
                            materiality_gbp=round(abs(actual - baseline), 2),
                            evidence=outcome,
                        )
                    )

        elif kind == "column_type":
            column = invariant.get("column")
            expected = invariant.get("expected")
            values = [v for v in result.columns.get(column, []) if v is not None]
            if expected == "number":
                ok = all(isinstance(v, (int, float)) for v in values)
            elif expected == "date":
                ok = all(
                    isinstance(v, str) and len(v) == 10 and v[4] == "-" for v in values
                )
            else:
                ok = True
            outcome.update({"column": column, "expected": expected})
            if not ok:
                outcome["passed"] = False
                deviations.append(
                    Deviation(
                        type="type_drift",
                        severity=severity,
                        group_key=f"invariant:type:{column}",
                        title=f"{column} is no longer consistently {expected}",
                        detail=(
                            f"Values in {column} did not all read as {expected} after cleaning. "
                            f"A column that has changed what it holds will produce numbers that "
                            f"look right and are not."
                        ),
                        column_name=column,
                        evidence=outcome,
                    )
                )

        outcomes.append(outcome)

    return outcomes, deviations


def invariant_status(outcomes: list[dict[str, Any]]) -> str:
    passed = sum(1 for outcome in outcomes if outcome.get("passed"))
    return f"{passed}/{len(outcomes)}" if outcomes else "0/0"


__all__ = [
    "Deviation",
    "ReplayResult",
    "capture_steps",
    "check_invariants",
    "default_invariants",
    "invariant_status",
    "replay",
]
