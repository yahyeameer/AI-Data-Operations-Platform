"""
Applying approved changes.

Cleaning never mutates (PRD section 3). This module takes a parsed table and a
list of approved operations and produces a *new* set of columns plus a
changelog; the caller writes that as a new dataset version with a parent
pointer. Rollback is then "use the parent", and diffing two months is a
question about two rows in one table.

Every operation is a pure function of (columns, params). That is section 4's
requirement -- "steps are pure functions over a dataset version" -- and it is
what makes a recipe replayable next month against a file nobody has seen yet.

The changelog is not logging. It is the evidence that a specific row changed
from a specific value to another, which is what section 7's drill-down reads.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .parse import ParsedTable
from .values import normalize_text, parse_date, parse_number

log = logging.getLogger("hermes.clean")

# A single operation may not rewrite more than this fraction of the dataset
# without being flagged. A mapping that touches 95% of rows is not a
# normalisation, it is a mistake, and it should not pass quietly just because
# somebody clicked approve on a summary line.
BULK_CHANGE_WARNING_RATIO = 0.5


@dataclass
class ChangeRecord:
    source_row: int
    column: str | None
    before: Any
    after: Any


@dataclass
class OperationResult:
    op: str
    column: str | None
    rows_changed: int
    rows_removed: int = 0
    warnings: list[str] = field(default_factory=list)
    samples: list[ChangeRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "column": self.column,
            "rows_changed": self.rows_changed,
            "rows_removed": self.rows_removed,
            "warnings": self.warnings,
            "samples": [
                {
                    "source_row": sample.source_row,
                    "column": sample.column,
                    "before": sample.before,
                    "after": sample.after,
                }
                for sample in self.samples
            ],
        }


@dataclass
class CleanResult:
    columns: dict[str, list[Any]]
    source_rows: list[int]
    operations: list[OperationResult]

    @property
    def row_count(self) -> int:
        return len(self.source_rows)

    def summary(self) -> dict[str, Any]:
        return {
            "rows_out": self.row_count,
            "operations": [operation.to_dict() for operation in self.operations],
            "rows_changed": sum(operation.rows_changed for operation in self.operations),
            "rows_removed": sum(operation.rows_removed for operation in self.operations),
            "warnings": [
                warning
                for operation in self.operations
                for warning in operation.warnings
            ],
        }


class Table:
    """
    Mutable working copy.

    Deliberately a plain dict of lists rather than a DataFrame. The datasets are
    tens of thousands of rows, the operations are row-wise and need the original
    row number attached to every change, and a columnar library would buy speed
    we do not need at the cost of provenance we do.
    """

    def __init__(self, columns: dict[str, list[Any]], source_rows: list[int]):
        self.columns = {name: list(values) for name, values in columns.items()}
        self.source_rows = list(source_rows)

    @property
    def row_count(self) -> int:
        return len(self.source_rows)

    def business_columns(self) -> list[str]:
        return [name for name in self.columns if not name.startswith("__raw_")]

    def keep(self, indices: list[int]) -> None:
        keep_set = sorted(set(indices))
        self.source_rows = [self.source_rows[i] for i in keep_set]
        for name, values in self.columns.items():
            self.columns[name] = [values[i] for i in keep_set]


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def _op_normalize_whitespace(table: Table, params: dict[str, Any]) -> OperationResult:
    column = params["column"]
    values = table.columns.get(column)
    result = OperationResult(op="normalize_whitespace", column=column, rows_changed=0)

    if values is None:
        result.warnings.append(f"column {column!r} is not present; step skipped")
        return result

    for index, value in enumerate(values):
        if value is None or not isinstance(value, str):
            continue
        cleaned = normalize_text(value)
        if cleaned != value:
            if len(result.samples) < 5:
                result.samples.append(ChangeRecord(table.source_rows[index], column, value, cleaned))
            values[index] = cleaned
            result.rows_changed += 1

    return result


def _op_normalize_case(table: Table, params: dict[str, Any]) -> OperationResult:
    column = params["column"]
    style = params.get("style", "title")
    values = table.columns.get(column)
    result = OperationResult(op="normalize_case", column=column, rows_changed=0)

    if values is None:
        result.warnings.append(f"column {column!r} is not present; step skipped")
        return result

    transform: Callable[[str], str] = {
        "title": str.title,
        "upper": str.upper,
        "lower": str.lower,
    }.get(style, str.title)

    for index, value in enumerate(values):
        if not isinstance(value, str):
            continue
        cleaned = transform(value)
        if cleaned != value:
            if len(result.samples) < 5:
                result.samples.append(ChangeRecord(table.source_rows[index], column, value, cleaned))
            values[index] = cleaned
            result.rows_changed += 1

    return result


def _op_map_values(table: Table, params: dict[str, Any]) -> OperationResult:
    """
    Apply an explicit value mapping — the vendor-normalisation step.

    The mapping is exact-match only. Fuzzy matching happens when the *proposal*
    is built, where a human sees the groups and approves them; re-running the
    fuzzy logic at apply time would let a threshold change silently alter what
    was approved.
    """
    column = params["column"]
    mapping: dict[str, str] = params.get("mapping", {})
    values = table.columns.get(column)
    result = OperationResult(op="map_values", column=column, rows_changed=0)

    if values is None:
        result.warnings.append(f"column {column!r} is not present; step skipped")
        return result
    if not mapping:
        result.warnings.append("mapping is empty; step skipped")
        return result

    for index, value in enumerate(values):
        if not isinstance(value, str):
            continue
        replacement = mapping.get(value)
        if replacement is not None and replacement != value:
            if len(result.samples) < 5:
                result.samples.append(
                    ChangeRecord(table.source_rows[index], column, value, replacement)
                )
            values[index] = replacement
            result.rows_changed += 1

    if table.row_count and result.rows_changed / table.row_count > BULK_CHANGE_WARNING_RATIO:
        result.warnings.append(
            f"this mapping rewrote {result.rows_changed} of {table.row_count} rows; "
            f"check it is a normalisation and not a collapse"
        )

    return result


def _op_drop_duplicate_rows(table: Table, params: dict[str, Any]) -> OperationResult:
    subset: list[str] | None = params.get("columns")
    keep = params.get("keep", "first")
    names = subset or table.business_columns()
    result = OperationResult(op="drop_duplicate_rows", column=None, rows_changed=0)

    missing = [name for name in names if name not in table.columns]
    if missing:
        result.warnings.append(f"columns not present, ignored: {', '.join(missing)}")
        names = [name for name in names if name in table.columns]
    if not names:
        result.warnings.append("no usable columns; step skipped")
        return result

    seen: dict[tuple[Any, ...], int] = {}
    keep_indices: list[int] = []
    removed: list[int] = []

    for index in range(table.row_count):
        key = tuple(table.columns[name][index] for name in names)
        if key in seen:
            removed.append(index)
            if keep == "last":
                # Replace the earlier survivor with this one.
                keep_indices[seen[key]] = index
            continue
        seen[key] = len(keep_indices)
        keep_indices.append(index)

    for index in removed[:5]:
        result.samples.append(
            ChangeRecord(table.source_rows[index], None, "duplicate row", "removed")
        )

    table.keep(keep_indices)
    result.rows_removed = len(removed)
    return result


def _op_coerce_number(table: Table, params: dict[str, Any]) -> OperationResult:
    """
    Idempotent by design.

    The parser already coerced the column, so on the first run this changes
    nothing and reports zero. It stays in the recipe because a recipe is a
    complete description of how the file becomes the dataset -- replaying it
    against raw text next month must produce numbers, not strings.
    """
    column = params["column"]
    values = table.columns.get(column)
    result = OperationResult(op="coerce_number", column=column, rows_changed=0)

    if values is None:
        result.warnings.append(f"column {column!r} is not present; step skipped")
        return result

    failures = 0
    for index, value in enumerate(values):
        if value is None or isinstance(value, (int, float)):
            continue
        parsed = parse_number(value)
        if parsed.ok:
            if len(result.samples) < 5:
                result.samples.append(
                    ChangeRecord(table.source_rows[index], column, value, parsed.as_float)
                )
            values[index] = parsed.as_float
            result.rows_changed += 1
        else:
            values[index] = None
            failures += 1

    if failures:
        result.warnings.append(f"{failures} value(s) could not be read as a number and are now empty")

    return result


def _op_normalize_date(table: Table, params: dict[str, Any]) -> OperationResult:
    column = params["column"]
    order = params.get("order", "dmy")
    values = table.columns.get(column)
    result = OperationResult(op="normalize_date", column=column, rows_changed=0)

    if values is None:
        result.warnings.append(f"column {column!r} is not present; step skipped")
        return result

    failures = 0
    for index, value in enumerate(values):
        if value is None:
            continue
        if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-":
            continue
        parsed = parse_date(value, prefer=order if order in {"dmy", "mdy"} else "dmy")
        if parsed.ok and parsed.value:
            iso = parsed.value.isoformat()
            if len(result.samples) < 5:
                result.samples.append(ChangeRecord(table.source_rows[index], column, value, iso))
            values[index] = iso
            result.rows_changed += 1
        else:
            failures += 1

    if failures:
        result.warnings.append(f"{failures} value(s) could not be read as a date and were left as-is")

    return result


def _op_noop(table: Table, params: dict[str, Any]) -> OperationResult:
    """
    Review and block operations carry no transformation.

    They exist as recipe steps so that the recipe is a complete record of what
    was decided, including the decisions that were "look at this and confirm".
    Applying one is a no-op; approving one is the point.
    """
    return OperationResult(op=params.get("__op", "review"), column=params.get("column"), rows_changed=0)


OPERATIONS: dict[str, Callable[[Table, dict[str, Any]], OperationResult]] = {
    "normalize_whitespace": _op_normalize_whitespace,
    "normalize_case": _op_normalize_case,
    "map_values": _op_map_values,
    "drop_duplicate_rows": _op_drop_duplicate_rows,
    "coerce_number": _op_coerce_number,
    "normalize_date": _op_normalize_date,
    "review_ambiguous_dates": _op_noop,
    "review_key_conflicts": _op_noop,
    "review_outliers": _op_noop,
    "review_vat_rate": _op_noop,
    "block_totals_mismatch": _op_noop,
}


def apply_operations(table: ParsedTable, operations: list[dict[str, Any]]) -> CleanResult:
    """
    Run approved operations in order.

    Order matters and is the caller's responsibility: trimming whitespace before
    mapping vendor names finds matches that the reverse order misses. The
    recipe stores the order that was approved, so a replay reproduces it rather
    than re-deriving it.
    """
    working = Table(table.columns, table.source_rows)
    results: list[OperationResult] = []

    for operation in operations:
        name = operation.get("op")
        handler = OPERATIONS.get(name or "")
        if handler is None:
            results.append(
                OperationResult(
                    op=name or "unknown",
                    column=operation.get("column"),
                    rows_changed=0,
                    warnings=[f"unknown operation {name!r}; skipped"],
                )
            )
            continue

        params = dict(operation)
        params["__op"] = name
        try:
            results.append(handler(working, params))
        except Exception as error:  # noqa: BLE001 - one bad step must not lose the rest
            log.exception("operation %s failed", name)
            results.append(
                OperationResult(
                    op=name or "unknown",
                    column=operation.get("column"),
                    rows_changed=0,
                    warnings=[f"step failed: {error}"],
                )
            )

    return CleanResult(
        columns=working.columns, source_rows=working.source_rows, operations=results
    )


def to_parquet(columns: dict[str, list[Any]], source_rows: list[int]) -> bytes:
    """
    Serialise to Parquet for the `parquet` bucket.

    `__source_row` is written as a real column rather than kept beside the file.
    Section 7 promises a drill-down from any figure to its source rows, and that
    promise survives a restart, a re-download and a query run by something that
    never saw this process only if the row number travels inside the data.
    """
    import polars as pl

    frame = pl.DataFrame(
        {"__source_row": source_rows, **columns},
        strict=False,
        infer_schema_length=None,
    )

    buffer = io.BytesIO()
    frame.write_parquet(buffer, compression="zstd")
    return buffer.getvalue()


def column_hash(columns: dict[str, list[Any]]) -> str:
    """
    A fingerprint of the shape, stored on the version.

    Comparing two versions' hashes answers "did the columns change?" without
    downloading either file -- which is what the month-over-month invariant in
    section 5.3 needs before it decides whether a comparison is even valid.
    """
    import hashlib

    names = sorted(name for name in columns if not name.startswith("__raw_"))
    return hashlib.sha256("|".join(names).encode("utf-8")).hexdigest()[:32]


__all__ = [
    "CleanResult",
    "OperationResult",
    "apply_operations",
    "column_hash",
    "to_parquet",
]
