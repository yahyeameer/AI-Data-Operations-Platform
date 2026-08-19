"""
Report generation.

The output is Markdown rather than PDF. An accountant's month-end pack goes
into an email, a working paper or a client portal, and Markdown survives all
three; PDF generation would add a rendering dependency to a VPS in exchange for
a format nobody can edit.

Every figure in a report comes from `analyze`, and every one of them carries
the row count behind it. Section 7's promise is that any displayed number can
be traced, so a report that states a total without saying how many rows it
covers is already outside the design.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any


# Column names that a naive .title() mangles. An accountant reading "Vat" in a
# report they are about to send to a client notices immediately.
_ACRONYMS = {"vat": "VAT", "gbp": "GBP", "usd": "USD", "eur": "EUR", "id": "ID", "po": "PO"}


def _money(value: float | None, symbol: str = "£") -> str:
    """
    Parentheses for negatives, which is what a set of accounts uses.

    The source files already write credit notes as `(150.00)`; rendering them
    back as `-£150.00` would be correct and still read as foreign to the person
    checking the report against their own spreadsheet.
    """
    if value is None:
        return "—"
    if value < 0:
        return f"({symbol}{abs(value):,.2f})"
    return f"{symbol}{value:,.2f}"


def _label(name: str) -> str:
    return " ".join(
        _ACRONYMS.get(word.lower(), word.title()) for word in name.replace("_", " ").split()
    )


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_No rows._\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines) + "\n"


def build_markdown_report(
    *,
    workspace_name: str,
    dataset_name: str,
    version_no: int,
    kpis: dict[str, Any],
    profile_signals: dict[str, Any],
    proposals_summary: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
    narrative: str | None = None,
    generated_at: dt.datetime | None = None,
) -> str:
    generated = generated_at or dt.datetime.now(dt.timezone.utc)
    parts: list[str] = []

    parts.append(f"# {dataset_name}\n")
    parts.append(
        f"**{workspace_name}** · dataset version {version_no} · "
        f"generated {generated.strftime('%d %B %Y %H:%M UTC')}\n"
    )

    # The caveat goes at the top, not in a footnote. A report whose totals do
    # not reconcile against the source file must say so before anyone reads the
    # totals -- section 5.3's blocking behaviour, carried into the document.
    totals = profile_signals.get("declared_totals", {})
    if totals.get("checked") and totals.get("all_reconcile") is False:
        failing = [check for check in totals.get("checks", []) if not check["reconciles"]]
        parts.append("\n> **These figures do not reconcile to the source file.**\n>")
        for check in failing:
            parts.append(
                f"> {check['column']}: computed {_money(check['computed'])} against a declared "
                f"{_money(check['declared'])} — a difference of {_money(check['difference'])}.\n>"
            )
        parts.append("> Resolve this before the figures are relied on.\n")

    if narrative:
        parts.append(f"\n## Summary\n\n{narrative}\n")

    parts.append("\n## Headline figures\n")
    rows: list[list[str]] = [["Rows", f"{kpis.get('row_count', 0):,}"]]

    period = kpis.get("period") or {}
    if period.get("earliest"):
        rows.append(["Period", f"{period['earliest']} to {period['latest']}"])

    for key, value in kpis.items():
        if not isinstance(value, dict) or "total" not in value:
            continue
        rows.append([_label(key), _money(value["total"])])
        if value.get("negative_rows"):
            rows.append(["— of which credits", f"{value['negative_rows']} row(s)"])

    parts.append(_table(["Measure", "Value"], rows))

    for key, value in kpis.items():
        if not key.startswith("top_by_") or not isinstance(value, list):
            continue
        dimension = key.removeprefix("top_by_").replace("_", " ")
        parts.append(f"\n## By {dimension}\n")
        parts.append(
            _table(
                [_label(dimension), "Total", "Rows"],
                [
                    [str(item["label"]), _money(item["total"]), str(item["rows"])]
                    for item in value
                ],
            )
        )

    if comparison:
        parts.append("\n## Period comparison\n")
        change = comparison.get("percent_change")
        change_text = f"{change:+.1f}%" if change is not None else "n/a"
        parts.append(
            f"{comparison['metric']}: {_money(comparison['total_a'])} → "
            f"{_money(comparison['total_b'])} "
            f"({_money(comparison['difference'])}, {change_text})\n"
        )
        if comparison.get("drivers"):
            parts.append("\nLargest movements:\n")
            parts.append(
                _table(
                    ["", "Previous", "Current", "Change"],
                    [
                        [
                            str(driver["label"]),
                            _money(driver["period_a"]),
                            _money(driver["period_b"]),
                            _money(driver["difference"]),
                        ]
                        for driver in comparison["drivers"]
                    ],
                )
            )

    parts.append("\n## Data quality\n")
    quality_rows: list[list[str]] = []

    duplicates = profile_signals.get("exact_duplicates", {})
    quality_rows.append(
        ["Exact duplicate rows", str(duplicates.get("duplicate_rows", 0))]
    )

    variants = profile_signals.get("entity_variants", {}).get("columns", [])
    quality_rows.append(
        ["Name-variant groups", str(sum(item["group_count"] for item in variants))]
    )

    vat = profile_signals.get("vat_consistency", {})
    if vat.get("checked"):
        quality_rows.append(["VAT rate anomalies", str(vat.get("anomaly_count", 0))])
        quality_rows.append(["VAT rates seen", str(vat.get("rate_distribution", {}))])

    dates = profile_signals.get("date_coverage", {})
    if dates.get("checked") and dates.get("ambiguous_dates"):
        quality_rows.append(
            [
                "Ambiguous dates",
                f"{dates['ambiguous_dates']} read as {str(dates.get('assumed_order', '')).upper()}",
            ]
        )

    if proposals_summary:
        quality_rows.append(
            [
                "Changes applied automatically",
                str(proposals_summary.get("auto", 0)),
            ]
        )
        quality_rows.append(
            ["Changes needing review", str(proposals_summary.get("review", 0))]
        )
        quality_rows.append(
            [
                "Value under review",
                _money(proposals_summary.get("review_materiality_gbp")),
            ]
        )

    parts.append(_table(["Check", "Result"], quality_rows))

    parts.append(
        "\n---\n\n_Produced by the Hermes agent from dataset version "
        f"{version_no}. Every figure above is computed from the stored dataset, not estimated. "
        "A copilot, not an autonomous accountant — review before use._\n"
    )

    return "\n".join(parts)


def rows_to_csv(rows: list[dict[str, Any]]) -> bytes:
    """Export helper for the `exports` bucket."""
    if not rows:
        return b""

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    # BOM so Excel opens it as UTF-8 rather than mangling the pound sign, which
    # is the first thing anyone will notice in a UK accounting export.
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


__all__ = ["build_markdown_report", "rows_to_csv"]
