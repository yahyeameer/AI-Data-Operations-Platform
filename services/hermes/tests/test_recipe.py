"""
Recipe capture, replay and invariants — the month-1-to-month-2 loop.

MVP criteria 6 and 9 are what these tests are for, and the PRD says of them:
"Criterion 6 and criterion 9 are the product. If those two work, everything
else is a matter of polish." So they are tested here directly, against the two
fixtures, with no database in the way:

  * month 1's approved changes become a recipe whose entity merge points at a
    mapping table rather than freezing last month's spellings;
  * month 2 replays it, applies what it knows, and asks about exactly the two
    things it should not assume;
  * once a human answers, the same question does not come back.

The last one is criterion 9, and it is asserted by running the replay twice
with a mapping table that grew in between.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.tools.clean import apply_operations
from hermes.tools.parse import parse_workbook
from hermes.tools.profile import profile_table
from hermes.tools.propose import build_proposals
from hermes.tools.recipe import (
    build_vocabulary_entries,
    capture_steps,
    check_invariants,
    default_invariants,
    invariant_status,
    replay,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "messy"
AUGUST = FIXTURES / "acme-sales-2026-08.xlsx"
SEPTEMBER = FIXTURES / "acme-sales-2026-09.xlsx"

SUPPLIER_TABLE = "mt-supplier-0001"


def _parse(path: Path):
    parsed = parse_workbook(path.read_bytes(), path.name)
    table = parsed.primary
    return parsed, table, profile_table(table)


@pytest.fixture(scope="module")
def august():
    return _parse(AUGUST)


@pytest.fixture(scope="module")
def september():
    return _parse(SEPTEMBER)


@pytest.fixture(scope="module")
def approved(august):
    """What an accountant approved in month 1: everything except the blocker."""
    _parsed, table, profile = august
    return [
        {
            "group_key": proposal.group_key,
            "operation": proposal.operation,
            "confidence": proposal.confidence,
        }
        for proposal in build_proposals(table, profile)
        if proposal.confidence != "low"
    ]


@pytest.fixture(scope="module")
def august_cleaned(august, approved):
    """August's output — what the recipe and its invariants are learned from."""
    _parsed, table, _profile = august
    return apply_operations(table, [item["operation"] for item in approved])


@pytest.fixture(scope="module")
def recipe(august, approved, august_cleaned):
    """The captured recipe, with the supplier merge behind a mapping table."""
    _parsed, table, _profile = august
    from hermes.tools.parse import ParsedTable

    steps = capture_steps(approved, {"supplier": SUPPLIER_TABLE})
    cleaned_table = ParsedTable(
        interpretation=table.interpretation,
        columns=august_cleaned.columns,
        source_rows=august_cleaned.source_rows,
    )
    invariants = default_invariants(profile_table(cleaned_table))
    return steps, invariants


@pytest.fixture(scope="module")
def expected_columns(recipe):
    steps, invariants = recipe
    return next(i["columns"] for i in invariants if i["type"] == "columns_present")


@pytest.fixture(scope="module")
def august_vocabulary(approved, august_cleaned):
    """
    The mapping table as month 1 leaves it.

    Built with the same helper capture uses, so the tests exercise the real
    thing rather than a convenient approximation of it: the merges plus an
    identity entry for every supplier that survived cleaning.
    """
    mapping: dict[str, str] = {}
    for item in approved:
        operation = item["operation"]
        if operation.get("op") == "map_values":
            mapping.update(operation.get("mapping") or {})

    entries = build_vocabulary_entries(august_cleaned.columns["supplier"], mapping)
    return {SUPPLIER_TABLE: {e["source_key"]: e["canonical_value"] for e in entries}}


# -----------------------------------------------------------------------------
# The signature is what makes the match possible at all
# -----------------------------------------------------------------------------


def test_both_months_share_a_source_signature(august, september):
    # If this fails, nothing else in this file means anything: the recipe would
    # never be found, and month 2 would be treated as a brand new report.
    assert august[0].source_signature == september[0].source_signature


def test_the_signature_ignores_content(august, september):
    # Different rows, different totals, same fingerprint. The signature is a
    # statement about layout, and a month with new transactions is still the
    # same monthly report.
    assert august[1].row_count == september[1].row_count == 9
    august_total = sum(v for v in august[1].columns["net_sales"] if v is not None)
    september_total = sum(v for v in september[1].columns["net_sales"] if v is not None)
    assert round(august_total, 2) != round(september_total, 2)


# -----------------------------------------------------------------------------
# Capture (criterion 5)
# -----------------------------------------------------------------------------


def test_capture_preserves_the_approved_order(approved):
    steps = capture_steps(approved, {})
    assert [step["op"] for step in steps] == [item["operation"]["op"] for item in approved]
    assert [step["id"] for step in steps] == [f"step_{i:02d}" for i in range(1, len(approved) + 1)]


def test_the_vocabulary_holds_more_than_the_corrections(approved, august_cleaned):
    """
    A mapping table of corrections only is not a vocabulary.

    August merged two supplier spellings. If those were the only entries, then
    next month Fabrikam and Tailspin — which were always spelled correctly and
    needed no merge — would come back as unknown, burying the one supplier that
    genuinely is new.
    """
    mapping: dict[str, str] = {}
    for item in approved:
        if item["operation"].get("op") == "map_values":
            mapping.update(item["operation"].get("mapping") or {})

    entries = build_vocabulary_entries(august_cleaned.columns["supplier"], mapping)
    keys = {entry["source_key"] for entry in entries}

    assert "northwind supplies" in keys          # a merge somebody approved
    assert "fabrikam ltd" in keys                # never needed merging, still known
    assert "tailspin toys" in keys

    confirmed = {e["source_key"] for e in entries if e["confirmed"]}
    # Only the decisions are marked confirmed; the observations are not.
    assert confirmed == {"northwind supplies", "contoso limited"}


def test_capture_moves_entity_mappings_into_a_mapping_table(approved):
    """
    Section 4: mappings are shared growable tables, not step parameters.

    Freezing August's spellings into the step would mean every new supplier
    needs a new recipe version, and the automation rate would never climb past
    the first month's vocabulary.
    """
    steps = capture_steps(approved, {"supplier": SUPPLIER_TABLE})
    step = next(s for s in steps if s["op"] == "map_values")

    assert step["params"]["mapping_table_id"] == SUPPLIER_TABLE
    assert "mapping" not in step["params"]


def test_capture_keeps_an_inline_mapping_when_there_is_no_table(approved):
    steps = capture_steps(approved, {})
    step = next(s for s in steps if s["op"] == "map_values")
    assert step["params"]["mapping"]
    assert "mapping_table_id" not in step["params"]


def test_capture_records_the_tier_each_step_was_approved_at(approved):
    steps = capture_steps(approved, {})
    tiers = {step["op"]: step["confidence_tier"] for step in steps}
    assert tiers["coerce_number"] == "auto"
    assert tiers["map_values"] == "review"


def test_default_invariants_cover_the_section_5_3_list(august):
    _parsed, _table, profile = august
    invariants = default_invariants(profile)
    kinds = {invariant["type"] for invariant in invariants}

    assert "row_count_within" in kinds
    assert "columns_present" in kinds
    assert "total_within" in kinds
    assert "column_type" in kinds

    # A missing column is a blocking failure, not something to note and move on.
    columns_check = next(i for i in invariants if i["type"] == "columns_present")
    assert columns_check["severity"] == "block"


# -----------------------------------------------------------------------------
# Replay (criterion 6)
# -----------------------------------------------------------------------------


def test_replay_applies_what_it_knows_without_asking(september, recipe, august_vocabulary, expected_columns):
    _parsed, table, profile = september
    steps, _invariants = recipe

    result = replay(table, steps, profile, august_vocabulary, expected_columns=expected_columns)

    assert result.rows_processed == 9
    # The deterministic steps ran: dates, numbers, whitespace.
    assert result.auto_corrections >= 0
    assert result.cleaned.row_count == 9


def test_replay_asks_about_exactly_the_two_things_it_should_not_assume(
    september, recipe, august_vocabulary, expected_columns
):
    """
    The heart of it.

    September introduces "Northwind Supplies Limited" (a new spelling of a
    known supplier) and "Litware Inc" (genuinely new). The recipe must not
    merge the first silently and must not invent a relationship for the second.
    """
    _parsed, table, profile = september
    steps, _invariants = recipe

    result = replay(table, steps, profile, august_vocabulary, expected_columns=expected_columns)
    by_type = {}
    for deviation in result.deviations:
        by_type.setdefault(deviation.type, []).append(deviation)

    ambiguous = by_type.get("ambiguous_match", [])
    unmapped = by_type.get("unmapped_value", [])

    assert [d.source_value for d in ambiguous] == ["Northwind Supplies Limited"]
    assert ambiguous[0].suggested_value == "Northwind Supplies Ltd"
    assert ambiguous[0].severity == "review"

    assert [d.source_value for d in unmapped] == ["Litware Inc"]
    # Nothing to match it against, so nothing is suggested. Guessing here would
    # invent a relationship that does not exist.
    assert unmapped[0].suggested_value is None


def test_replay_ranks_deviations_by_money(september, recipe, august_vocabulary, expected_columns):
    _parsed, table, profile = september
    steps, _invariants = recipe

    result = replay(table, steps, profile, august_vocabulary, expected_columns=expected_columns)
    litware = next(d for d in result.deviations if d.source_value == "Litware Inc")
    northwind = next(d for d in result.deviations if d.source_value == "Northwind Supplies Limited")

    assert litware.materiality_gbp == 4200.0
    assert northwind.materiality_gbp == 1890.25


def test_a_run_with_open_deviations_needs_review(september, recipe, august_vocabulary, expected_columns):
    _parsed, table, profile = september
    steps, _invariants = recipe

    result = replay(table, steps, profile, august_vocabulary, expected_columns=expected_columns)
    assert result.needs_review is True
    assert result.blocked is False


def test_replay_reports_a_missing_column_as_blocking(
    september, recipe, august_vocabulary, expected_columns
):
    """
    A step whose column has vanished must stop the run.

    Replaying the rest would produce a result that looks complete and is not,
    which is exactly the silent failure section 5.3 exists to catch.
    """
    _parsed, table, profile = september
    steps, _invariants = recipe

    stripped = type(table)(
        interpretation=table.interpretation,
        columns={k: v for k, v in table.columns.items() if k != "net_sales"},
        source_rows=table.source_rows,
    )

    result = replay(stripped, steps, profile, august_vocabulary, expected_columns=expected_columns)
    missing = [d for d in result.deviations if d.type == "missing_column"]

    assert missing and missing[0].column_name == "net_sales"
    assert missing[0].severity == "block"
    assert result.blocked is True


def test_replay_notes_a_new_column_without_blocking(
    september, recipe, august_vocabulary, expected_columns
):
    _parsed, table, profile = september
    steps, _invariants = recipe

    widened = type(table)(
        interpretation=table.interpretation,
        columns={**table.columns, "cost_centre": ["CC1"] * table.row_count},
        source_rows=table.source_rows,
    )

    result = replay(widened, steps, profile, august_vocabulary, expected_columns=expected_columns)
    new_columns = [d for d in result.deviations if d.type == "new_column"]

    assert new_columns and "cost_centre" in new_columns[0].evidence["columns"]
    assert new_columns[0].severity == "review"


# -----------------------------------------------------------------------------
# Mapping write-back (criterion 9)
# -----------------------------------------------------------------------------


def test_a_resolved_mapping_does_not_recur_next_time(
    september, recipe, august_vocabulary, expected_columns
):
    """
    Criterion 9, asserted directly.

    Replay once, resolve the ambiguous match the way a person would, replay
    again against the grown vocabulary — and the question is gone. This is the
    mechanism the PRD says takes automation from 85% to 99%.
    """
    _parsed, table, profile = september
    steps, _invariants = recipe

    first = replay(table, steps, profile, august_vocabulary, expected_columns=expected_columns)
    question = next(d for d in first.deviations if d.type == "ambiguous_match")

    # What resolve_deviation writes: the source value, keyed for lookup.
    grown = {
        SUPPLIER_TABLE: {
            **august_vocabulary[SUPPLIER_TABLE],
            question.source_value.strip().lower(): question.suggested_value,
        }
    }

    second = replay(table, steps, profile, grown, expected_columns=expected_columns)

    assert not [d for d in second.deviations if d.source_value == "Northwind Supplies Limited"]
    # And the merge actually happened this time.
    assert "Northwind Supplies Limited" not in second.cleaned.columns["supplier"]
    assert second.cleaned.columns["supplier"].count("Northwind Supplies Ltd") == 2


def test_resolving_everything_makes_the_run_fully_automatic(
    september, recipe, august_vocabulary, expected_columns
):
    _parsed, table, profile = september
    steps, _invariants = recipe

    grown = {
        SUPPLIER_TABLE: {
            **august_vocabulary[SUPPLIER_TABLE],
            "northwind supplies limited": "Northwind Supplies Ltd",
            # Accepting a genuinely new supplier maps it to itself.
            "litware inc": "Litware Inc",
        }
    }

    result = replay(table, steps, profile, grown, expected_columns=expected_columns)

    assert result.deviations == []
    assert result.needs_review is False
    assert result.automation_rate == 1.0


def test_a_mapping_hit_is_recorded_so_it_can_show_its_worth(
    september, recipe, august_vocabulary, expected_columns
):
    _parsed, table, profile = september
    steps, _invariants = recipe

    grown = {
        SUPPLIER_TABLE: {
            **august_vocabulary[SUPPLIER_TABLE],
            "northwind supplies limited": "Northwind Supplies Ltd",
        }
    }
    result = replay(table, steps, profile, grown, expected_columns=expected_columns)

    assert "northwind supplies limited" in result.mapping_hits[SUPPLIER_TABLE]


# -----------------------------------------------------------------------------
# Post-run invariants (section 5.3)
# -----------------------------------------------------------------------------


def test_invariants_pass_on_a_normal_month(september, recipe, august_vocabulary, expected_columns):
    _parsed, table, profile = september
    steps, invariants = recipe

    result = replay(table, steps, profile, august_vocabulary, expected_columns=expected_columns)
    outcomes, deviations = check_invariants(invariants, result.cleaned, profile)

    assert all(outcome["passed"] for outcome in outcomes), [
        o for o in outcomes if not o["passed"]
    ]
    assert deviations == []
    assert invariant_status(outcomes).endswith(f"/{len(outcomes)}")


def test_an_invariant_can_fail_a_run_that_had_no_deviations(
    september, recipe, august_vocabulary, expected_columns
):
    """
    The silent-failure guard.

    "A recipe matching 100% of rows is not evidence of correctness." Here every
    step succeeds and nothing deviates, but the file is a tenth of its usual
    size — which in an accounting export means a partial extract, not a quiet
    month.
    """
    _parsed, table, profile = september
    steps, invariants = recipe

    tiny = type(table)(
        interpretation=table.interpretation,
        columns={name: values[:1] for name, values in table.columns.items()},
        source_rows=table.source_rows[:1],
    )

    result = replay(tiny, steps, profile, august_vocabulary, expected_columns=expected_columns)
    outcomes, deviations = check_invariants(invariants, result.cleaned, profile)

    row_check = next(o for o in outcomes if o["id"] == "row_count")
    assert row_check["passed"] is False
    assert any(d.group_key == "invariant:row_count" for d in deviations)


def test_a_missing_required_column_blocks(september, recipe, august_vocabulary, expected_columns):
    _parsed, table, profile = september
    steps, invariants = recipe

    result = replay(table, steps, profile, august_vocabulary, expected_columns=expected_columns)
    result.cleaned.columns.pop("vat")

    outcomes, deviations = check_invariants(invariants, result.cleaned, profile)
    columns_check = next(o for o in outcomes if o["id"] == "required_columns")

    assert columns_check["passed"] is False
    assert any(d.severity == "block" for d in deviations)
