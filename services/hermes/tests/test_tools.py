"""
Tool tests, against the deliberately messy fixture.

`fixtures/messy/acme-sales-2026-08.xlsx` carries every pathology PRD section 6
lists, so these assertions are the beginnings of the eval harness that section 8
asks for in week 2 rather than week 8. They need no database and no network: the
tools are pure, which is exactly why they are separated from the handlers.

The expected values are written out rather than computed, because a test that
recomputes the answer the same way the code does will agree with a wrong answer.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from hermes.tools.analyze import QueryError, compile_query, run_query
from hermes.tools.clean import apply_operations, to_parquet
from hermes.tools.parse import parse_workbook
from hermes.tools.profile import profile_table
from hermes.tools.propose import build_proposals, summarise
from hermes.tools.values import (
    entity_key,
    is_subtotal_label,
    normalize_text,
    parse_date,
    parse_number,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "messy" / "acme-sales-2026-08.xlsx"


@pytest.fixture(scope="module")
def parsed():
    return parse_workbook(FIXTURE.read_bytes(), FIXTURE.name)


@pytest.fixture(scope="module")
def table(parsed):
    return parsed.primary


@pytest.fixture(scope="module")
def profile(table):
    return profile_table(table)


# -----------------------------------------------------------------------------
# Value coercion
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,240.00", Decimal("1240.00")),
        ("£880.50", Decimal("880.50")),
        ("(150.00)", Decimal("-150.00")),
        ("-410.25", Decimal("-410.25")),
        ("1,200", Decimal("1200")),
        ("1.234,56", Decimal("1234.56")),        # European
        ("1'234'567", Decimal("1234567")),       # Swiss
        ("120.00 CR", Decimal("-120.00")),       # credit
        ("120.00 DR", Decimal("120.00")),        # debit
        ("15%", Decimal("0.15")),
        ("100-", Decimal("-100")),               # trailing minus
    ],
)
def test_parses_accounting_number_formats(raw, expected):
    parsed = parse_number(raw)
    assert parsed.ok, f"{raw!r} did not parse"
    assert parsed.value == expected


@pytest.mark.parametrize("raw", ["", "-", "n/a", "#REF!", "TBC", "not a number", "12abc"])
def test_rejects_non_numbers(raw):
    # Notably `-` and `n/a`: an accountant's "nothing here". Coercing those to
    # zero would quietly change a total.
    assert not parse_number(raw).ok


def test_parenthesised_negative_records_its_style():
    # The style is the evidence behind a proposal, so it has to survive.
    assert "parentheses" in parse_number("(150.00)").styles
    assert "currency" in parse_number("£880.50").styles
    assert "thousands" in parse_number("1,240.00").styles


@pytest.mark.parametrize(
    ("raw", "prefer", "expected"),
    [
        ("01/08/2026", "dmy", dt.date(2026, 8, 1)),
        ("01/08/2026", "mdy", dt.date(2026, 1, 8)),
        ("31/01/2026", "mdy", dt.date(2026, 1, 31)),  # day > 12 proves DMY
        ("2026-08-01", "dmy", dt.date(2026, 8, 1)),
        ("9 Aug 2026", "dmy", dt.date(2026, 8, 9)),
    ],
)
def test_parses_dates(raw, prefer, expected):
    assert parse_date(raw, prefer=prefer).value == expected


def test_flags_ambiguous_dates_rather_than_guessing_silently():
    ambiguous = parse_date("01/08/2026")
    unambiguous = parse_date("31/01/2026")
    assert ambiguous.ambiguous is True
    assert unambiguous.ambiguous is False


def test_normalises_whitespace_and_unicode():
    assert normalize_text("Fabrikam  Ltd") == "Fabrikam Ltd"
    assert normalize_text("  spaced   out  ") == "spaced out"


def test_entity_key_folds_spelling_variants():
    assert entity_key("Northwind Supplies Ltd") == entity_key("northwind supplies")
    assert entity_key("Contoso Ltd.") == entity_key("CONTOSO LIMITED")
    # And does not fold genuinely different companies.
    assert entity_key("Smith Ltd") != entity_key("Smith Holdings Ltd")


@pytest.mark.parametrize(
    "label", ["Subtotal", "TOTAL", "Grand Total", "Total August 2026", "Subtotal Q3", "Sub-total"]
)
def test_recognises_summary_labels(label):
    assert is_subtotal_label(label)


@pytest.mark.parametrize(
    "label",
    [
        # Real companies. Dropping their rows loses money silently, which is
        # worse than counting a subtotal twice -- nothing downstream flags it.
        "Total Fitness Ltd",
        "Totally Awesome Widgets",
        "Sum Services Ltd",
        "Balance Recruitment",
        "INV-1007",
    ],
)
def test_does_not_mistake_a_company_name_for_a_summary(label):
    assert not is_subtotal_label(label)


# -----------------------------------------------------------------------------
# Structure detection (PRD section 6)
# -----------------------------------------------------------------------------


def test_finds_the_header_below_a_title_block(table):
    assert table.interpretation.header_row == 5


def test_excludes_summary_and_footnote_rows(table):
    reasons = [row.reason for row in table.interpretation.skipped]
    # Two summary rows: the embedded Subtotal and the trailing TOTAL.
    assert reasons.count("subtotal") == 2
    assert reasons.count("footnote") == 2
    assert table.row_count == 9


def test_keeps_the_duplicate_row_for_the_deviation_engine_to_find(table):
    # Deduplication is a decision the accountant makes, so the parser must not
    # quietly make it first.
    assert table.columns["invoice"].count("INV-1007") == 2


def test_infers_column_types(table):
    types = {column.name: column.inferred_type for column in table.interpretation.columns}
    assert types == {
        "date": "date",
        "invoice": "text",
        "supplier": "text",
        "net_sales": "number",
        "vat": "number",
    }


def test_keeps_the_original_text_alongside_coerced_values(table):
    # Section 7's provenance promise: a coerced value has to be traceable back
    # to the characters that produced it.
    assert table.columns["__raw_net_sales"][3] == "(150.00)"
    assert table.columns["net_sales"][3] == -150.0


def test_treats_the_larger_sheet_as_the_main_table(parsed):
    assert parsed.primary.interpretation.sheet_name == "Sales Aug"
    assert {t.interpretation.sheet_name for t in parsed.tables} == {"Sales Aug", "Notes"}


def test_source_signature_is_stable_across_reparses():
    first = parse_workbook(FIXTURE.read_bytes(), FIXTURE.name)
    second = parse_workbook(FIXTURE.read_bytes(), FIXTURE.name)
    assert first.source_signature == second.source_signature


def test_refuses_legacy_xls_rather_than_mis_parsing_it():
    with pytest.raises(ValueError, match="xlsx"):
        parse_workbook(b"\xd0\xcf\x11\xe0", "ledger.xls")


# -----------------------------------------------------------------------------
# Profiling
# -----------------------------------------------------------------------------


def test_finds_the_exact_duplicate(profile):
    duplicates = profile.signals["exact_duplicates"]
    assert duplicates["group_count"] == 1
    assert duplicates["duplicate_rows"] == 1


def test_finds_both_supplier_spelling_groups(profile):
    variants = profile.signals["entity_variants"]["columns"]
    assert len(variants) == 1
    assert variants[0]["column"] == "supplier"
    assert variants[0]["group_count"] == 2


def test_reconciles_against_the_files_own_declared_total(profile):
    """
    The fixture's TOTAL row says 10,361.35; its transactions add to 10,361.10.

    That 25p is the single most valuable thing the profiler finds: the client's
    own spreadsheet does not add up. A profiler that only reported null counts
    would sail past it.
    """
    totals = profile.signals["declared_totals"]
    assert totals["checked"] is True
    assert totals["all_reconcile"] is False

    net = next(check for check in totals["checks"] if check["column"] == "net_sales")
    assert net["computed"] == 10361.10
    assert net["declared"] == 10361.35
    assert net["difference"] == -0.25


def test_checks_vat_rates(profile):
    vat = profile.signals["vat_consistency"]
    assert vat["checked"] is True
    assert vat["anomaly_count"] == 0
    assert vat["rate_distribution"] == {"20.00%": 9}


def test_notices_the_ambiguous_date_landing_outside_the_period(profile):
    coverage = profile.signals["date_coverage"]
    assert coverage["spans_multiple_months"] is True
    assert coverage["months"]["2026-03"] == 1
    assert coverage["ambiguous_dates"] == 9


# -----------------------------------------------------------------------------
# Proposals (PRD section 5)
# -----------------------------------------------------------------------------


def test_proposes_the_expected_set(table, profile):
    keys = {proposal.group_key for proposal in build_proposals(table, profile)}
    assert keys == {
        "number:net_sales",
        "number:vat",
        "date:date",
        "duplicates:exact",
        "entity:supplier",
        "date_ambiguity:date",
        "invariant:declared_totals",
    }


def test_blocking_items_sort_above_everything(table, profile):
    proposals = build_proposals(table, profile)
    assert proposals[0].group_key == "invariant:declared_totals"
    assert proposals[0].confidence == "low"


def test_review_items_are_ranked_by_money_not_row_count(table, profile):
    review = [p for p in build_proposals(table, profile) if p.confidence == "medium"]
    amounts = [p.materiality_gbp or 0 for p in review]
    assert amounts == sorted(amounts, reverse=True)
    # The date proposal touches one row and outranks the two-row supplier merge
    # because it is worth more. Section 5.2's whole point.
    assert review[0].group_key == "date_ambiguity:date"


def test_entity_merges_are_never_auto_applied(table, profile):
    entity = next(p for p in build_proposals(table, profile) if p.group_key == "entity:supplier")
    assert entity.confidence == "medium"


def test_automation_rate_counts_only_the_auto_tier(table, profile):
    summary = summarise(build_proposals(table, profile))
    assert summary["auto"] == 3
    assert summary["review"] == 3
    assert summary["blocking"] == 1
    assert summary["blocked"] is True


# -----------------------------------------------------------------------------
# Applying changes
# -----------------------------------------------------------------------------


def test_applying_approved_changes_writes_a_corrected_table(table, profile):
    operations = [
        p.operation for p in build_proposals(table, profile) if p.confidence != "low"
    ]
    result = apply_operations(table, operations)

    assert result.row_count == 8                       # the duplicate is gone
    assert table.row_count == 9                        # and the input is untouched
    assert result.columns["supplier"].count("Northwind Supplies Ltd") == 2
    assert result.columns["supplier"].count("Contoso Ltd.") == 2


def test_an_unknown_operation_is_skipped_rather_than_crashing_the_run(table):
    result = apply_operations(table, [{"op": "definitely_not_a_real_operation"}])
    assert result.row_count == table.row_count
    assert "unknown operation" in result.operations[0].warnings[0]


def test_a_missing_column_warns_instead_of_failing(table):
    # Month 2 may not have the column month 1's recipe names. One dead step
    # must not lose the other nine.
    result = apply_operations(table, [{"op": "normalize_whitespace", "column": "nope"}])
    assert "not present" in result.operations[0].warnings[0]


# -----------------------------------------------------------------------------
# Analytics
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parquet(table, profile):
    operations = [
        p.operation for p in build_proposals(table, profile) if p.confidence != "low"
    ]
    result = apply_operations(table, operations)
    return to_parquet(result.columns, result.source_rows)


def test_query_returns_answers_with_their_source_rows(parquet):
    result = run_query(
        parquet,
        {
            "select": [{"column": "net_sales", "agg": "sum", "alias": "total_net"}],
            "group_by": ["supplier"],
            "order_by": [{"column": "total_net", "direction": "desc"}],
        },
    )

    assert result.rows[0] == {"supplier": "Fabrikam Ltd", "total_net": 4385.1}
    # Provenance: the drill-down is a lookup, not a re-run.
    refs = next(ref for ref in result.row_refs if ref["group"]["supplier"] == "Fabrikam Ltd")
    assert refs["source_rows"] == [11, 14]


def test_query_rejects_an_unknown_column(parquet):
    with pytest.raises(QueryError, match="unknown column"):
        run_query(parquet, {"select": [{"column": "definitely_not_here", "agg": "sum"}]})


def test_compiler_binds_values_instead_of_interpolating_them():
    columns = {"supplier", "net_sales"}
    sql, params, _ = compile_query(
        {
            "select": [{"column": "net_sales", "agg": "sum"}],
            "filters": [{"column": "supplier", "op": "eq", "value": "'; drop table dataset; --"}],
        },
        columns,
    )
    assert "drop table" not in sql
    assert params == ["'; drop table dataset; --"]


def test_compiler_refuses_an_ungrouped_non_aggregate():
    with pytest.raises(QueryError, match="neither grouped nor aggregated"):
        compile_query(
            {"select": [{"column": "net_sales"}], "group_by": ["supplier"]},
            {"supplier", "net_sales"},
        )
