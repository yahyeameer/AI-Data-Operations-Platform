"""
Dataset profiling.

Two jobs, and it is worth being explicit that they are different.

**For the accountant**, the profile is the first honest answer about a file:
how many rows, what is in each column, what looks wrong. It is what the review
screen is built from.

**For the model**, the profile is the *only* thing it is allowed to see. PRD
section 8 is categorical -- "never send raw rows to the model" -- and this is
where that stops being a policy and becomes a structure. The LLM layer accepts
a Profile, not a table. It cannot leak rows it was never handed.

The signals below lean deliberately towards accounting rather than generic data
quality. A generic profiler reports null counts and cardinality. An accountant
needs to know that the file's own TOTAL row disagrees with the sum of its
transactions by 25p, that INV-1007 appears twice, and that the same supplier is
spelled four ways -- because those three facts are the month's actual work.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from .parse import ParsedTable
from .values import entity_key, normalize_text, parse_number

# A column whose name matches these is money, and money gets materiality
# weighting that a quantity column does not.
MONEY_HINTS = (
    "amount", "net", "gross", "total", "value", "sales", "cost", "price",
    "balance", "debit", "credit", "vat", "tax", "fee", "charge", "revenue",
    "expense", "payment", "invoice_value", "subtotal", "gbp", "usd", "eur",
)

# Columns that plausibly identify a transaction. Used to look for duplicates
# that are not byte-identical -- the same invoice entered twice with a typo in
# the supplier name.
KEY_HINTS = ("invoice", "reference", "ref", "id", "number", "no", "doc", "voucher", "txn")

# UK VAT rates a sales ledger realistically carries.
UK_VAT_RATES = (0.20, 0.05, 0.0)


@dataclass
class ColumnProfile:
    name: str
    source_header: str
    inferred_type: str
    type_confidence: float
    non_null: int
    null_count: int
    distinct_count: int
    is_money: bool = False
    # Numeric summary. Absent for non-numeric columns rather than zeroed, so a
    # column of zeros is distinguishable from a column that has no numbers.
    minimum: float | None = None
    maximum: float | None = None
    total: float | None = None
    mean: float | None = None
    median: float | None = None
    negative_count: int = 0
    zero_count: int = 0
    # Dates.
    earliest: str | None = None
    latest: str | None = None
    # Text.
    top_values: list[dict[str, Any]] = field(default_factory=list)
    blank_like_count: int = 0
    whitespace_issues: int = 0
    case_variants: int = 0
    # Carried through from parsing so the review screen can explain itself.
    number_styles: list[str] = field(default_factory=list)
    date_order: str | None = None
    ambiguous_dates: int = 0
    parse_failures: int = 0
    failure_samples: list[str] = field(default_factory=list)


@dataclass
class Profile:
    row_count: int
    column_count: int
    columns: list[ColumnProfile]
    signals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "column_count": self.column_count,
            "columns": [asdict(column) for column in self.columns],
            "signals": self.signals,
        }


def _is_money_column(name: str, header: str) -> bool:
    haystack = f"{name} {header}".lower()
    return any(hint in haystack for hint in MONEY_HINTS)


def _is_key_column(name: str, header: str) -> bool:
    haystack = f"{name} {header}".lower()
    return any(hint in haystack for hint in KEY_HINTS)


def _round(value: float | None, places: int = 2) -> float | None:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    return round(float(value), places)


def profile_table(table: ParsedTable, max_samples: int = 5) -> Profile:
    interpretation = table.interpretation
    row_count = table.row_count

    columns: list[ColumnProfile] = []

    for column in interpretation.columns:
        values = table.columns.get(column.name, [])
        non_null = [value for value in values if value is not None and value != ""]

        profile = ColumnProfile(
            name=column.name,
            source_header=column.source_header,
            inferred_type=column.inferred_type,
            type_confidence=column.type_confidence,
            non_null=len(non_null),
            null_count=row_count - len(non_null),
            distinct_count=len({str(value) for value in non_null}),
            is_money=_is_money_column(column.name, column.source_header),
            number_styles=list(column.number_styles),
            date_order=column.date_order,
            ambiguous_dates=column.ambiguous_dates,
            parse_failures=column.parse_failures,
            failure_samples=list(column.failure_samples),
        )

        if column.inferred_type == "number" and non_null:
            numbers = [float(value) for value in non_null if isinstance(value, (int, float))]
            if numbers:
                profile.minimum = _round(min(numbers))
                profile.maximum = _round(max(numbers))
                profile.total = _round(sum(numbers))
                profile.mean = _round(statistics.fmean(numbers), 4)
                profile.median = _round(statistics.median(numbers), 4)
                profile.negative_count = sum(1 for n in numbers if n < 0)
                profile.zero_count = sum(1 for n in numbers if n == 0)

        elif column.inferred_type == "date" and non_null:
            dates = sorted(str(value) for value in non_null)
            profile.earliest = dates[0]
            profile.latest = dates[-1]

        elif column.inferred_type in {"text", "boolean"} and non_null:
            texts = [str(value) for value in non_null]
            counts = Counter(texts)
            profile.top_values = [
                {"value": value, "count": count}
                for value, count in counts.most_common(max_samples)
            ]
            profile.whitespace_issues = sum(
                1 for text in texts if text != text.strip() or "  " in text
            )
            # Distinct spellings that fold to the same entity key. The count is
            # the interesting number: 4 raw values collapsing to 2 keys means
            # two suppliers are each spelled two ways.
            folded = {entity_key(text) for text in texts}
            profile.case_variants = max(0, len(set(texts)) - len(folded))

        columns.append(profile)

    signals = _compute_signals(table, columns, max_samples)

    return Profile(
        row_count=row_count,
        column_count=len(columns),
        columns=columns,
        signals=signals,
    )


def _compute_signals(
    table: ParsedTable, columns: list[ColumnProfile], max_samples: int
) -> dict[str, Any]:
    """Whole-dataset findings — the ones that need more than one column to see."""
    signals: dict[str, Any] = {}

    signals["exact_duplicates"] = _exact_duplicates(table, max_samples)
    signals["key_duplicates"] = _key_duplicates(table, columns, max_samples)
    signals["entity_variants"] = _entity_variants(table, columns, max_samples)
    signals["declared_totals"] = _declared_totals(table, columns)
    signals["vat_consistency"] = _vat_consistency(table, columns)
    signals["date_coverage"] = _date_coverage(table, columns)
    signals["outliers"] = _outliers(table, columns, max_samples)

    money_columns = [column for column in columns if column.is_money and column.total is not None]
    signals["money_columns"] = [
        {"name": column.name, "total": column.total, "negatives": column.negative_count}
        for column in money_columns
    ]

    return signals


def _row_tuple(table: ParsedTable, index: int, names: list[str]) -> tuple[Any, ...]:
    return tuple(table.columns[name][index] for name in names)


def _business_columns(table: ParsedTable) -> list[str]:
    """Real columns, excluding the __raw_ provenance companions."""
    return [name for name in table.columns if not name.startswith("__raw_")]


def _preview_row(table: ParsedTable, index: int, names: list[str], limit: int = 80) -> str:
    """A one-line rendering of a row, for evidence shown next to a proposal."""
    parts = [
        normalize_text(table.columns[name][index])
        for name in names
        if table.columns[name][index] not in (None, "")
    ]
    text = " | ".join(parts)
    return text[:limit] + ("…" if len(text) > limit else "")


def _exact_duplicates(table: ParsedTable, max_samples: int) -> dict[str, Any]:
    names = _business_columns(table)
    seen: dict[tuple[Any, ...], list[int]] = defaultdict(list)

    for index in range(table.row_count):
        seen[_row_tuple(table, index, names)].append(index)

    groups = [indices for indices in seen.values() if len(indices) > 1]
    duplicate_rows = sum(len(group) - 1 for group in groups)

    return {
        "group_count": len(groups),
        "duplicate_rows": duplicate_rows,
        "examples": [
            {
                "source_rows": [table.source_rows[i] for i in group],
                "preview": _preview_row(table, group[0], names),
            }
            for group in groups[:max_samples]
        ],
    }


def _key_duplicates(
    table: ParsedTable, columns: list[ColumnProfile], max_samples: int
) -> dict[str, Any]:
    """
    Rows sharing an identifier but differing elsewhere.

    Distinct from an exact duplicate and much more dangerous: an exact duplicate
    is obviously a double-entry, while the same invoice number against two
    different amounts is a question only the client can answer.
    """
    key_columns = [
        column.name
        for column in columns
        if _is_key_column(column.name, column.source_header)
        and column.inferred_type in {"text", "number"}
        and column.distinct_count > 1
    ]
    if not key_columns:
        return {"key_columns": [], "group_count": 0, "examples": []}

    names = _business_columns(table)
    key = key_columns[0]
    by_key: dict[Any, list[int]] = defaultdict(list)

    for index in range(table.row_count):
        value = table.columns[key][index]
        if value is not None and value != "":
            by_key[value].append(index)

    conflicting: list[dict[str, Any]] = []
    for value, indices in by_key.items():
        if len(indices) < 2:
            continue
        rows = {_row_tuple(table, index, names) for index in indices}
        if len(rows) > 1:
            conflicting.append(
                {
                    "key": str(value),
                    "source_rows": [table.source_rows[i] for i in indices],
                }
            )

    return {
        "key_columns": key_columns,
        "chosen_key": key,
        "group_count": len(conflicting),
        "examples": conflicting[:max_samples],
    }


def _entity_variants(
    table: ParsedTable, columns: list[ColumnProfile], max_samples: int
) -> dict[str, Any]:
    """
    Text columns where several spellings denote one entity.

    This is the mapping-table opportunity from PRD section 4, and criterion 9 of
    the MVP: a human resolves it once and it must not recur next month.
    """
    findings: list[dict[str, Any]] = []

    for column in columns:
        if column.inferred_type != "text":
            continue
        # A near-unique column is a reference, not a category; folding it would
        # produce thousands of meaningless "variants".
        if column.non_null == 0 or column.distinct_count / max(column.non_null, 1) > 0.9:
            continue

        values = [
            str(value)
            for value in table.columns[column.name]
            if value is not None and value != ""
        ]
        grouped: dict[str, Counter[str]] = defaultdict(Counter)
        for value in values:
            grouped[entity_key(value)][value] += 1

        variants = [
            {
                "canonical_key": key,
                # The most frequent spelling is the suggested canonical form.
                # Frequency beats alphabetical: the version typed most often is
                # usually the one the client's own system produces.
                "suggested": spellings.most_common(1)[0][0],
                "spellings": [
                    {"value": value, "count": count} for value, count in spellings.most_common()
                ],
            }
            for key, spellings in grouped.items()
            if len(spellings) > 1
        ]

        if variants:
            findings.append(
                {
                    "column": column.name,
                    "group_count": len(variants),
                    "affected_rows": sum(
                        sum(item["count"] for item in variant["spellings"])
                        for variant in variants
                    ),
                    "groups": variants[:max_samples],
                }
            )

    return {"columns": findings}


def _declared_totals(table: ParsedTable, columns: list[ColumnProfile]) -> dict[str, Any]:
    """
    Reconcile the file's own TOTAL rows against the transactions beneath them.

    The parser excluded those rows from the data, but it kept them, and they are
    the most valuable thing in the file: the client has already told us what the
    answer should be. If our sum disagrees with theirs, either we parsed
    something wrong or their spreadsheet is wrong, and both are worth knowing
    before anything is filed.

    This is the check that makes section 5.3's post-run invariants concrete.
    """
    summary_rows = [
        row for row in table.interpretation.skipped if row.reason == "subtotal"
    ]
    if not summary_rows:
        return {"checked": False, "reason": "the file declares no totals"}

    money_columns = [column for column in columns if column.inferred_type == "number"]
    if not money_columns:
        return {"checked": False, "reason": "no numeric columns to reconcile"}

    # The declared figures live in the preview text the parser kept. Pull every
    # number out of the last summary row -- conventionally the grand total.
    grand = summary_rows[-1]
    declared = [
        parsed.as_float
        for parsed in (parse_number(part) for part in grand.preview.split("|"))
        if parsed.ok
    ]

    checks: list[dict[str, Any]] = []
    for index, column in enumerate(money_columns):
        if index >= len(declared) or column.total is None:
            continue
        declared_value = declared[index]
        if declared_value is None:
            continue
        difference = round(column.total - declared_value, 2)
        checks.append(
            {
                "column": column.name,
                "computed": column.total,
                "declared": declared_value,
                "difference": difference,
                # A penny either way is float noise or a rounding convention.
                # Anything more is a real disagreement.
                "reconciles": abs(difference) < 0.01,
                "source_row": grand.source_row,
            }
        )

    return {
        "checked": bool(checks),
        "summary_rows": [
            {"source_row": row.source_row, "preview": row.preview} for row in summary_rows
        ],
        "checks": checks,
        "all_reconcile": all(check["reconciles"] for check in checks) if checks else None,
    }


def _vat_consistency(table: ParsedTable, columns: list[ColumnProfile]) -> dict[str, Any]:
    """
    Where a net column and a VAT column both exist, check the rate per row.

    Not a generic data-quality test -- a specifically accounting one, and the
    kind of thing the pilot customer would otherwise do by eye. A row at 19%
    among a column of 20% rows is either a foreign supplier or a typo, and in
    both cases it is the row to look at first.
    """
    net_column = next(
        (
            column
            for column in columns
            if column.inferred_type == "number"
            and any(hint in column.name for hint in ("net", "sales", "goods", "amount"))
            and "vat" not in column.name
        ),
        None,
    )
    vat_column = next(
        (
            column
            for column in columns
            if column.inferred_type == "number" and ("vat" in column.name or "tax" in column.name)
        ),
        None,
    )

    if not net_column or not vat_column:
        return {"checked": False, "reason": "no net/VAT column pair found"}

    nets = table.columns[net_column.name]
    vats = table.columns[vat_column.name]

    rates: Counter[str] = Counter()
    anomalies: list[dict[str, Any]] = []

    for index in range(table.row_count):
        net = nets[index]
        vat = vats[index]
        if not isinstance(net, (int, float)) or not isinstance(vat, (int, float)) or net == 0:
            continue

        rate = round(vat / net, 4)
        matched = next(
            (known for known in UK_VAT_RATES if abs(rate - known) < 0.005), None
        )
        rates[f"{rate:.2%}"] += 1

        if matched is None:
            anomalies.append(
                {
                    "source_row": table.source_rows[index],
                    "net": net,
                    "vat": vat,
                    "rate": rate,
                }
            )

    return {
        "checked": True,
        "net_column": net_column.name,
        "vat_column": vat_column.name,
        "rate_distribution": dict(rates.most_common(5)),
        "anomaly_count": len(anomalies),
        "anomalies": anomalies[:5],
    }


def _date_coverage(table: ParsedTable, columns: list[ColumnProfile]) -> dict[str, Any]:
    date_columns = [column for column in columns if column.inferred_type == "date"]
    if not date_columns:
        return {"checked": False}

    column = date_columns[0]
    values = [
        str(value) for value in table.columns[column.name] if value is not None and value != ""
    ]
    if not values:
        return {"checked": False}

    parsed = sorted(dt.date.fromisoformat(value) for value in values)
    months = Counter(f"{date.year}-{date.month:02d}" for date in parsed)

    # A monthly export that spills into a neighbouring month is usually a
    # cut-off issue, which is a real accounting question rather than a parsing
    # one.
    return {
        "checked": True,
        "column": column.name,
        "earliest": parsed[0].isoformat(),
        "latest": parsed[-1].isoformat(),
        "distinct_months": len(months),
        "months": dict(months.most_common()),
        "spans_multiple_months": len(months) > 1,
        "ambiguous_dates": column.ambiguous_dates,
        "assumed_order": column.date_order,
    }


def _outliers(
    table: ParsedTable, columns: list[ColumnProfile], max_samples: int
) -> dict[str, Any]:
    """
    Interquartile-range outliers in money columns.

    IQR rather than standard deviations because ledger amounts are not normally
    distributed -- one £2m invoice in a column of £900 ones would inflate the
    deviation enough to hide itself.
    """
    findings: list[dict[str, Any]] = []

    for column in columns:
        if column.inferred_type != "number" or not column.is_money:
            continue
        numbers = [
            (index, float(value))
            for index, value in enumerate(table.columns[column.name])
            if isinstance(value, (int, float))
        ]
        if len(numbers) < 8:
            # Below this, quartiles describe the sample rather than the
            # population and every point looks extreme.
            continue

        values = sorted(value for _, value in numbers)
        q1 = statistics.quantiles(values, n=4)[0]
        q3 = statistics.quantiles(values, n=4)[2]
        iqr = q3 - q1
        if iqr == 0:
            continue

        low, high = q1 - 3 * iqr, q3 + 3 * iqr
        flagged = [
            {"source_row": table.source_rows[index], "value": value}
            for index, value in numbers
            if value < low or value > high
        ]
        if flagged:
            findings.append(
                {
                    "column": column.name,
                    "bounds": {"low": _round(low), "high": _round(high)},
                    "count": len(flagged),
                    "examples": flagged[:max_samples],
                }
            )

    return {"columns": findings}


__all__ = ["ColumnProfile", "Profile", "profile_table"]
