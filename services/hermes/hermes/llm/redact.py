"""
The boundary between the dataset and the model.

PRD section 8: "Never send raw rows to the model. Send schema, profile
statistics, and a small redacted sample. This is cheaper, faster, and
structurally solves most of the 'minimize sensitive data sent to external APIs'
requirement rather than solving it by policy."

The word doing the work there is *structurally*. A rule that says "don't send
rows" is obeyed until someone adds a feature in a hurry. A function that takes
a Profile and returns a dict, with no code path from a table to a prompt, is
obeyed by construction -- there is nothing to remember.

So: `build_context` is the only thing the router accepts, and it takes a
Profile, which never contained a row in the first place. The samples it does
include are drawn from the profile's `top_values`, which are already
aggregate facts ("this value appears 40 times") rather than records.
"""

from __future__ import annotations

import re
from typing import Any

from ..tools.profile import Profile

# Patterns that must never leave the host even inside a sample value. A profile
# of a "notes" or "reference" column can legitimately surface free text, and
# free text in an accounting export contains exactly these.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[email]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[card]"),
    # UK sort code and account number.
    (re.compile(r"\b\d{2}-\d{2}-\d{2}\b"), "[sort-code]"),
    (re.compile(r"\b\d{8}\b(?!\d)"), "[account-no]"),
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), "[iban]"),
    (re.compile(r"\+?\d[\d\s()-]{9,}\d"), "[phone]"),
    (re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE), "[postcode]"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def build_context(
    profile: Profile,
    interpretation: dict[str, Any] | None = None,
    max_sample_values: int = 5,
    redact_samples: bool = True,
) -> dict[str, Any]:
    """
    The complete set of facts the model is allowed to reason over.

    Note what is here: names, types, counts, ranges, totals, and a handful of
    frequent values per categorical column. Note what is not: any row, and any
    pairing of one column's value with another's. The model can tell you that
    the supplier column has four spellings of Contoso; it cannot tell you what
    Contoso was invoiced, because it was never shown a row.
    """
    columns: list[dict[str, Any]] = []

    for column in profile.columns:
        entry: dict[str, Any] = {
            "name": column.name,
            "header": column.source_header,
            "type": column.inferred_type,
            "type_confidence": column.type_confidence,
            "non_null": column.non_null,
            "nulls": column.null_count,
            "distinct": column.distinct_count,
        }

        if column.inferred_type == "number":
            entry.update(
                {
                    "min": column.minimum,
                    "max": column.maximum,
                    "total": column.total,
                    "mean": column.mean,
                    "median": column.median,
                    "negatives": column.negative_count,
                    "is_money": column.is_money,
                    "formats_seen": column.number_styles,
                }
            )
        elif column.inferred_type == "date":
            entry.update(
                {
                    "earliest": column.earliest,
                    "latest": column.latest,
                    "date_order": column.date_order,
                    "ambiguous": column.ambiguous_dates,
                }
            )
        elif column.top_values:
            samples = [
                {
                    "value": redact(str(item["value"]))[:60] if redact_samples else str(item["value"])[:60],
                    "count": item["count"],
                }
                for item in column.top_values[:max_sample_values]
            ]
            entry["frequent_values"] = samples
            entry["whitespace_issues"] = column.whitespace_issues

        if column.parse_failures:
            entry["parse_failures"] = column.parse_failures

        columns.append(entry)

    context: dict[str, Any] = {
        "row_count": profile.row_count,
        "column_count": profile.column_count,
        "columns": columns,
        "signals": _summarise_signals(profile.signals, redact_samples),
    }

    if interpretation:
        context["structure"] = {
            "sheet": interpretation.get("sheet_name"),
            "header_row": interpretation.get("header_row"),
            "data_rows": interpretation.get("data_rows"),
            "excluded_rows": len(interpretation.get("skipped") or []),
            "notes": interpretation.get("notes"),
        }

    return context


def _summarise_signals(signals: dict[str, Any], redact_samples: bool) -> dict[str, Any]:
    """
    Signals reduced to counts and totals.

    The full signal set carries source row numbers and example previews, which
    are rows by another name. The model needs to know *that* there are three
    duplicate groups worth £4,219, not which rows they are -- it is writing an
    explanation, and the deterministic layer already knows the rows.
    """
    summary: dict[str, Any] = {}

    duplicates = signals.get("exact_duplicates", {})
    if duplicates.get("duplicate_rows"):
        summary["exact_duplicates"] = {
            "groups": duplicates.get("group_count"),
            "rows": duplicates.get("duplicate_rows"),
        }

    conflicts = signals.get("key_duplicates", {})
    if conflicts.get("group_count"):
        summary["conflicting_identifiers"] = {
            "column": conflicts.get("chosen_key"),
            "groups": conflicts.get("group_count"),
        }

    variants = signals.get("entity_variants", {}).get("columns", [])
    if variants:
        summary["name_variants"] = [
            {
                "column": item["column"],
                "groups": item["group_count"],
                "affected_rows": item["affected_rows"],
                "examples": [
                    {
                        "suggested": redact(group["suggested"]) if redact_samples else group["suggested"],
                        "spellings": [
                            redact(spelling["value"]) if redact_samples else spelling["value"]
                            for spelling in group["spellings"]
                        ],
                    }
                    for group in item["groups"][:3]
                ],
            }
            for item in variants
        ]

    totals = signals.get("declared_totals", {})
    if totals.get("checked"):
        summary["declared_totals"] = {
            "all_reconcile": totals.get("all_reconcile"),
            "checks": [
                {
                    "column": check["column"],
                    "computed": check["computed"],
                    "declared": check["declared"],
                    "difference": check["difference"],
                }
                for check in totals.get("checks", [])
            ],
        }

    vat = signals.get("vat_consistency", {})
    if vat.get("checked"):
        summary["vat"] = {
            "net_column": vat.get("net_column"),
            "vat_column": vat.get("vat_column"),
            "rate_distribution": vat.get("rate_distribution"),
            "anomalies": vat.get("anomaly_count"),
        }

    coverage = signals.get("date_coverage", {})
    if coverage.get("checked"):
        summary["dates"] = {
            "earliest": coverage.get("earliest"),
            "latest": coverage.get("latest"),
            "months": coverage.get("months"),
            "ambiguous": coverage.get("ambiguous_dates"),
            "assumed_order": coverage.get("assumed_order"),
        }

    outliers = signals.get("outliers", {}).get("columns", [])
    if outliers:
        summary["outliers"] = [
            {"column": item["column"], "count": item["count"]} for item in outliers
        ]

    return summary


__all__ = ["build_context", "redact"]
