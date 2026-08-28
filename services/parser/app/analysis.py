"""
The deterministic analysis.

What the accountant reads after pressing Analyze. Every finding here is computed
in code -- no model is involved and none is needed. That is not an efficiency
choice: a figure that decides whether a month-end run proceeds has to be
reproducible, and "the model said so" is not reproducible.

Findings are tiered by consequence rather than by how sure we are:

    block   the run should not proceed until a human resolves this
    review  a person should look, and it changes numbers
    routine we did this; it is recorded so nothing is silent

Ranking is by money, not by row count. One unreconciled total outranks two
hundred whitespace fixes, and an interface that sorted the other way would bury
the thing that matters under the things that do not.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# Column-name heuristics, kept in one place so the tiering and the reconciliation
# agree about what counts as money.
_MONEY_HINTS = ("net", "vat", "amount", "gross", "total", "price", "value", "sales")


def _is_money_column(name: str) -> bool:
    lc = name.lower()
    return any(hint in lc for hint in _MONEY_HINTS)


def _money_columns(df) -> list[str]:
    return [c for c in df.columns if _is_money_column(c) and df[c].dtype.is_numeric()]


def _finding(
    tier: str,
    key: str,
    title: str,
    detail: str,
    rows: int = 0,
    value: float | None = None,
) -> dict[str, Any]:
    return {
        "tier": tier,
        "key": key,
        "title": title,
        "detail": detail,
        "affected_rows": rows,
        "value_gbp": round(value, 2) if value is not None else None,
    }


def _declared_totals(dropped: list[list[Any]], columns: list[str]) -> dict[int, float]:
    """
    Pull the numbers out of the file's own TOTAL row.

    Returned by column index rather than name: a total row rarely repeats the
    headers, it just puts figures under them, so position is the only thing
    linking the declaration to the column it describes.
    """
    from .main import money

    for row in dropped:
        first = next((c for c in row if c is not None), None)
        if not isinstance(first, str):
            continue
        if first.strip().lower() not in {"total", "grand total"}:
            continue
        found: dict[int, float] = {}
        for index in range(len(columns)):
            value = money(row[index]) if index < len(row) else None
            if value is not None:
                found[index] = value
        if found:
            return found
    return {}


def check_declared_totals(df, dropped: list[list[Any]], columns: list[str]) -> list[dict[str, Any]]:
    """
    Compare what we computed against what the file says about itself.

    This is the check that separates an automation tool from a liability. A
    workbook whose transaction rows do not add up to its own stated total has a
    problem somewhere, and the honest response is to stop rather than to publish
    a number that disagrees with the source it came from.
    """
    declared = _declared_totals(dropped, columns)
    if not declared:
        return []

    mismatches: list[tuple[str, float, float]] = []
    for index, stated in declared.items():
        if index >= len(columns):
            continue
        name = columns[index]
        if name not in df.columns or not df[name].dtype.is_numeric():
            continue
        computed = float(df[name].sum() or 0.0)
        # Half a penny, to absorb float noise rather than real discrepancies.
        if abs(computed - stated) > 0.005:
            mismatches.append((name, computed, stated))

    if not mismatches:
        return []

    worst = max(abs(c - s) for _, c, s in mismatches)
    detail = "; ".join(
        f"{name}: the rows add up to {computed:,.2f} but the file declares {stated:,.2f}"
        for name, computed, stated in mismatches
    )
    return [
        _finding(
            "block",
            "declared_totals",
            f"Totals do not reconcile in {len(mismatches)} column"
            + ("s" if len(mismatches) != 1 else ""),
            detail + ". Resolve this before the figures are used.",
            value=worst,
        )
    ]


def check_duplicates(df) -> list[dict[str, Any]]:
    if df.height == 0:
        return []
    duplicated = df.filter(df.is_duplicated())
    if duplicated.height == 0:
        return []

    # Every row after the first in each identical group is the removable one.
    removable = duplicated.height - duplicated.unique().height
    if removable <= 0:
        return []

    money_cols = _money_columns(df)
    value = None
    if money_cols:
        extra = duplicated.unique(keep="first")
        value = float(sum(float(extra[c].sum() or 0.0) for c in money_cols[:1]))

    return [
        _finding(
            "review",
            "exact_duplicates",
            f"Remove {removable} exact duplicate row" + ("s" if removable != 1 else ""),
            "These rows are identical to another row in every column. Keeping both "
            "double-counts them.",
            rows=removable,
            value=value,
        )
    ]


def check_entity_variants(df, originals: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """
    Two spellings of one supplier are two suppliers to every downstream total.

    Read from the spellings the file actually held, not from the parsed column:
    the parser already collapsed them with `normalize_vendor`, so by this point
    the column shows one value per party and there is nothing left to notice.
    That collapse is the right call for a total and the wrong thing to do in
    silence, and this finding is what breaks the silence -- it tells the
    accountant which spellings were treated as one party, so they can say
    whether that was correct.

    Grouping is `normalize_vendor`'s own -- case, punctuation and the
    Ltd/Limited suffix -- because those are the differences that are reliably
    cosmetic. Anything more aggressive starts merging genuinely different names.
    """
    from .main import normalize_vendor

    findings: list[dict[str, Any]] = []
    money_cols = _money_columns(df)

    for column, values in originals.items():
        groups: dict[str, set[str]] = defaultdict(set)
        rows_by_group: dict[str, list[int]] = defaultdict(list)
        for index, value in enumerate(values):
            if value is None:
                continue
            key = normalize_vendor(value)
            groups[key].add(str(value).strip())
            rows_by_group[key].append(index)

        multi = {key: spellings for key, spellings in groups.items() if len(spellings) > 1}
        if not multi:
            continue

        # The rows whose spelling was rewritten -- what the merge actually
        # touched, rather than how many distinct spellings there were.
        affected_rows = sorted(
            index for key in multi for index in rows_by_group[key]
        )

        value_gbp = None
        if money_cols and affected_rows:
            series = df[money_cols[0]]
            value_gbp = float(
                sum(
                    series[index] or 0.0
                    for index in affected_rows
                    if index < df.height
                )
            )

        examples = "; ".join(
            " / ".join(sorted(spellings)) for spellings in list(multi.values())[:3]
        )
        findings.append(
            _finding(
                "review",
                f"entity_variants:{column}",
                f"Merge {len(multi)} spelling group"
                + ("s" if len(multi) != 1 else "")
                + f" in {column}",
                f"The same party appears under more than one spelling and these rows were "
                f"read as one party: {examples}. Left unmerged, each spelling totals "
                f"separately.",
                rows=len(affected_rows),
                value=value_gbp,
            )
        )

    return findings


def check_missing_values(df) -> list[dict[str, Any]]:
    """Nulls in a money column change a total silently; nulls elsewhere do not."""
    findings: list[dict[str, Any]] = []
    for column in _money_columns(df):
        nulls = int(df[column].null_count())
        if nulls:
            findings.append(
                _finding(
                    "review",
                    f"missing_values:{column}",
                    f"{nulls} row" + ("s" if nulls != 1 else "") + f" have no {column}",
                    f"These rows contribute nothing to the {column} total. If that is "
                    f"wrong, the total is wrong.",
                    rows=nulls,
                )
            )
    return findings


def check_conversions(df, columns: list[str]) -> list[dict[str, Any]]:
    """
    What the parser already did, stated rather than assumed.

    These are routine by definition -- reading "£1,200.00" as 1200 is not a
    judgement call. They are reported so that nothing the parser changed is
    invisible, which is the difference between a tool an accountant checks once
    and one they have to check every month.
    """
    findings: list[dict[str, Any]] = []

    for column in _money_columns(df):
        non_null = df.height - int(df[column].null_count())
        if non_null:
            findings.append(
                _finding(
                    "routine",
                    f"read_number:{column}",
                    f"Read {column} as a number",
                    "Currency symbols, thousands separators and parenthesised "
                    "negatives were interpreted.",
                    rows=non_null,
                )
            )

    for column in columns:
        if column not in df.columns or "date" not in column.lower():
            continue
        values = [v for v in df[column].to_list() if v is not None]
        if values and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)) for v in values):
            findings.append(
                _finding(
                    "routine",
                    f"normalise_date:{column}",
                    f"Normalised {column} to ISO dates",
                    "Dates were read day-first and rewritten as YYYY-MM-DD.",
                    rows=len(values),
                )
            )

    return findings


_TIER_ORDER = {"block": 0, "review": 1, "routine": 2}


def analyze(parsed: dict[str, Any]) -> dict[str, Any]:
    """
    Run every check against a parsed workbook and rank the result.

    Takes the dict `parse_sheet` returns, so the analysis and the parse share
    one interpretation of the file rather than each forming their own.
    """
    df = parsed["dataframe"]
    columns: list[str] = parsed["columns"]
    dropped: list[list[Any]] = parsed.get("dropped") or []
    originals: dict[str, list[Any]] = parsed.get("original_names") or {}

    findings: list[dict[str, Any]] = []
    findings += check_declared_totals(df, dropped, columns)
    findings += check_duplicates(df)
    findings += check_entity_variants(df, originals)
    findings += check_missing_values(df)
    findings += check_conversions(df, columns)

    findings.sort(
        key=lambda f: (_TIER_ORDER.get(f["tier"], 3), -abs(f["value_gbp"] or 0), -f["affected_rows"])
    )

    blocking = [f for f in findings if f["tier"] == "block"]

    return {
        "rows": df.height,
        "columns": list(df.columns),
        "header_row": parsed.get("header_row"),
        "excluded_rows": parsed.get("dropped_rows", 0),
        "findings": findings,
        "blocked": bool(blocking),
        "summary": {
            "total": len(findings),
            "block": len(blocking),
            "review": len([f for f in findings if f["tier"] == "review"]),
            "routine": len([f for f in findings if f["tier"] == "routine"]),
            "at_stake_gbp": round(
                sum(abs(f["value_gbp"] or 0) for f in findings if f["tier"] != "routine"), 2
            ),
        },
    }


__all__ = ["analyze"]
