"""
Analytics over a cleaned dataset version.

PRD section 8 splits the work precisely: "Natural-language → structured query"
is the model's job, "Running the query" is not. This module is the second half,
and it never accepts SQL from anywhere.

That is not a stylistic preference. A model that emits SQL can emit
`DROP TABLE`, can emit a join that silently duplicates rows and inflates a
total, and can emit a `WHERE` clause that quietly excludes the credit notes. A
model that emits `{"select": [...], "group_by": [...]}` against a validated
column list can do none of those things, because the SQL is written here, by
code, from a closed grammar.

Every result carries the SQL that produced it and the source rows behind it.
Section 7 is explicit that this is the trust feature -- an accountant asking
"where did this come from" gets an answer, not a re-run that might disagree.
"""

from __future__ import annotations

import io
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger("hermes.analyze")

AGGREGATES = {
    "sum": "sum({col})",
    "avg": "avg({col})",
    "min": "min({col})",
    "max": "max({col})",
    "count": "count({col})",
    "count_distinct": "count(distinct {col})",
    "median": "median({col})",
}

FILTER_OPS = {
    "eq": "=",
    "neq": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "like": "ilike",
}

# Identifiers are validated against the dataset's real column list, so this is
# belt and braces -- but a name that reaches SQL construction must be provably
# inert regardless of how it got there.
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

MAX_LIMIT = 1000


class QueryError(ValueError):
    """A structured query that cannot be compiled. Always safe to show a user."""


@dataclass
class QueryResult:
    rows: list[dict[str, Any]]
    sql: str
    params: list[Any]
    row_refs: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    row_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "sql": self.sql,
            "params": self.params,
            "row_refs": self.row_refs,
            "duration_ms": self.duration_ms,
            "row_count": self.row_count,
        }


def _quote(identifier: str, allowed: set[str]) -> str:
    if identifier not in allowed:
        raise QueryError(
            f"unknown column {identifier!r}; available columns are {', '.join(sorted(allowed))}"
        )
    if not SAFE_IDENTIFIER.match(identifier):
        raise QueryError(f"column name {identifier!r} is not a usable identifier")
    return f'"{identifier}"'


def _alias(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower()
    if not cleaned or not SAFE_IDENTIFIER.match(cleaned):
        raise QueryError(f"alias {name!r} is not usable")
    return f'"{cleaned}"'


def compile_query(spec: dict[str, Any], columns: set[str]) -> tuple[str, list[Any], list[str]]:
    """
    Turn a structured query into SQL, parameters and the output column names.

    Values are always bound as parameters, never interpolated. Identifiers are
    always checked against `columns`, never quoted-and-hoped.
    """
    select_specs = spec.get("select") or []
    if not select_specs:
        raise QueryError("the query selects nothing")

    group_by: list[str] = spec.get("group_by") or []
    select_sql: list[str] = []
    output_names: list[str] = []
    has_aggregate = False

    for item in group_by:
        column = _quote(item, columns)
        select_sql.append(column)
        output_names.append(item)

    for item in select_specs:
        if isinstance(item, str):
            item = {"column": item}
        column_name = item.get("column")
        aggregate = item.get("agg")

        if aggregate == "count" and column_name in (None, "*"):
            expression = "count(*)"
            alias = item.get("alias") or "row_count"
            has_aggregate = True
        else:
            if not column_name:
                raise QueryError("each selected item needs a column")
            column = _quote(column_name, columns)
            if aggregate:
                if aggregate not in AGGREGATES:
                    raise QueryError(
                        f"unsupported aggregate {aggregate!r}; use one of {', '.join(AGGREGATES)}"
                    )
                expression = AGGREGATES[aggregate].format(col=column)
                alias = item.get("alias") or f"{aggregate}_{column_name}"
                has_aggregate = True
            else:
                if group_by and column_name not in group_by:
                    raise QueryError(
                        f"{column_name!r} is neither grouped nor aggregated; "
                        f"add an aggregate or include it in group_by"
                    )
                if group_by:
                    continue  # already emitted by the group_by loop
                expression = column
                alias = item.get("alias") or column_name

        select_sql.append(f"{expression} as {_alias(alias)}")
        output_names.append(re.sub(r"[^A-Za-z0-9_]+", "_", alias).strip("_").lower())

    # Provenance. For a grouped aggregate the interesting reference is the set
    # of rows behind each group, which is precisely what makes the drill-down a
    # lookup. Capped, because a group covering 40,000 rows does not need all of
    # them to answer "show me where this came from".
    if has_aggregate and "__source_row" in columns:
        select_sql.append('list("__source_row")[1:200] as __row_refs')

    where_sql, params = _compile_filters(spec.get("filters") or [], columns)

    sql = f"select {', '.join(select_sql)} from dataset"
    if where_sql:
        sql += f" where {where_sql}"
    if group_by:
        sql += " group by " + ", ".join(_quote(item, columns) for item in group_by)

    order_by = spec.get("order_by") or []
    if order_by:
        clauses = []
        for item in order_by:
            if isinstance(item, str):
                item = {"column": item}
            name = item.get("column")
            if not name:
                raise QueryError("order_by needs a column")
            # An ordering key may be an output alias (sum_net_sales) rather than
            # a source column, so accept either.
            if name in columns:
                target = _quote(name, columns)
            elif re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower() in output_names:
                target = _alias(name)
            else:
                raise QueryError(f"cannot order by unknown column {name!r}")
            direction = "desc" if str(item.get("direction", "asc")).lower() == "desc" else "asc"
            clauses.append(f"{target} {direction}")
        sql += " order by " + ", ".join(clauses)

    limit = spec.get("limit")
    limit = MAX_LIMIT if limit is None else min(int(limit), MAX_LIMIT)
    sql += f" limit {limit}"

    return sql, params, output_names


def _compile_filters(
    filters: list[dict[str, Any]], columns: set[str]
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    for filter_spec in filters:
        name = filter_spec.get("column")
        op = str(filter_spec.get("op", "eq")).lower()
        value = filter_spec.get("value")

        if not name:
            raise QueryError("each filter needs a column")
        column = _quote(name, columns)

        if op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise QueryError("a 'between' filter needs a two-element value")
            clauses.append(f"{column} between ? and ?")
            params.extend(value)
        elif op == "in":
            if not isinstance(value, (list, tuple)) or not value:
                raise QueryError("an 'in' filter needs a non-empty list")
            placeholders = ", ".join("?" for _ in value)
            clauses.append(f"{column} in ({placeholders})")
            params.extend(value)
        elif op == "is_null":
            clauses.append(f"{column} is null")
        elif op == "not_null":
            clauses.append(f"{column} is not null")
        elif op in FILTER_OPS:
            clauses.append(f"{column} {FILTER_OPS[op]} ?")
            params.append(value)
        else:
            raise QueryError(f"unsupported filter {op!r}")

    return " and ".join(clauses), params


def _connect(parquet_bytes: bytes):
    import duckdb
    import polars as pl

    # Read once into memory and register the frame rather than writing a temp
    # file. The VPS may have a small disk, and a crashed job that leaves temp
    # Parquet behind is an operational problem nobody will notice until it is
    # a full disk at month end.
    dataset = pl.read_parquet(io.BytesIO(parquet_bytes))  # noqa: F841 - registered below
    connection = duckdb.connect(":memory:")
    connection.register("dataset", dataset)
    return connection, set(dataset.columns)


def run_query(parquet_bytes: bytes, spec: dict[str, Any]) -> QueryResult:
    """Compile and execute a structured query against a dataset version."""
    connection, columns = _connect(parquet_bytes)
    try:
        sql, params, _names = compile_query(spec, columns)

        started = time.perf_counter()
        cursor = connection.execute(sql, params)
        column_names = [description[0] for description in cursor.description]
        raw_rows = cursor.fetchall()
        duration_ms = int((time.perf_counter() - started) * 1000)

        rows: list[dict[str, Any]] = []
        row_refs: list[dict[str, Any]] = []

        for raw in raw_rows:
            record = dict(zip(column_names, raw))
            refs = record.pop("__row_refs", None)
            rows.append(_jsonable(record))
            if refs:
                row_refs.append(
                    {
                        "group": {
                            key: value
                            for key, value in _jsonable(record).items()
                            if key in (spec.get("group_by") or [])
                        },
                        "source_rows": list(refs),
                    }
                )

        return QueryResult(
            rows=rows,
            sql=sql,
            params=params,
            row_refs=row_refs,
            duration_ms=duration_ms,
            row_count=len(rows),
        )
    finally:
        connection.close()


def _jsonable(record: dict[str, Any]) -> dict[str, Any]:
    import datetime as dt
    from decimal import Decimal

    output: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, Decimal):
            output[key] = float(value)
        elif isinstance(value, (dt.date, dt.datetime)):
            output[key] = value.isoformat()
        elif isinstance(value, (list, tuple)):
            output[key] = list(value)
        else:
            output[key] = value
    return output


# -----------------------------------------------------------------------------
# Accounting analytics
# -----------------------------------------------------------------------------


@dataclass
class PeriodComparison:
    metric: str
    period_a: str
    period_b: str
    total_a: float
    total_b: float
    difference: float
    percent_change: float | None
    drivers: list[dict[str, Any]]
    sql: str


def compare_periods(
    parquet_bytes: bytes,
    date_column: str,
    metric_column: str,
    period_a: tuple[str, str],
    period_b: tuple[str, str],
    breakdown_column: str | None = None,
    top_n: int = 5,
) -> PeriodComparison:
    """
    "What changed this month?" -- the question in section 2's diagram, and the
    one the pilot actually asks.

    The answer is not the two totals. It is the *drivers*: which suppliers or
    products moved, ranked by how much they moved. A month-on-month figure with
    no attribution tells an accountant that something happened and leaves them
    to find out what, which is the work they were trying to avoid.
    """
    connection, columns = _connect(parquet_bytes)
    try:
        for name in (date_column, metric_column):
            if name not in columns:
                raise QueryError(f"unknown column {name!r}")
        if breakdown_column and breakdown_column not in columns:
            raise QueryError(f"unknown column {breakdown_column!r}")

        date_sql = f'"{date_column}"'
        metric_sql = f'"{metric_column}"'

        totals_sql = (
            f"select "
            f"sum(case when {date_sql} between ? and ? then {metric_sql} else 0 end) as total_a, "
            f"sum(case when {date_sql} between ? and ? then {metric_sql} else 0 end) as total_b "
            f"from dataset"
        )
        params = [*period_a, *period_b]
        total_a, total_b = connection.execute(totals_sql, params).fetchone()
        total_a = float(total_a or 0)
        total_b = float(total_b or 0)

        drivers: list[dict[str, Any]] = []
        driver_sql = ""

        if breakdown_column:
            driver_sql = (
                f'select "{breakdown_column}" as label, '
                f"sum(case when {date_sql} between ? and ? then {metric_sql} else 0 end) as a, "
                f"sum(case when {date_sql} between ? and ? then {metric_sql} else 0 end) as b "
                f"from dataset "
                f'group by "{breakdown_column}" '
                f"order by abs(b - a) desc limit {int(top_n)}"
            )
            for label, a, b in connection.execute(driver_sql, params).fetchall():
                a, b = float(a or 0), float(b or 0)
                drivers.append(
                    {
                        "label": label,
                        "period_a": round(a, 2),
                        "period_b": round(b, 2),
                        "difference": round(b - a, 2),
                        "percent_change": round((b - a) / a * 100, 2) if a else None,
                    }
                )

        difference = round(total_b - total_a, 2)

        return PeriodComparison(
            metric=metric_column,
            period_a=f"{period_a[0]}..{period_a[1]}",
            period_b=f"{period_b[0]}..{period_b[1]}",
            total_a=round(total_a, 2),
            total_b=round(total_b, 2),
            difference=difference,
            # None rather than 0 when there is no base. A percentage change from
            # nothing is undefined, and rendering it as 0% or infinity are both
            # lies an accountant would have to unpick.
            percent_change=round(difference / total_a * 100, 2) if total_a else None,
            drivers=drivers,
            sql=totals_sql + ("; " + driver_sql if driver_sql else ""),
        )
    finally:
        connection.close()


def headline_kpis(
    parquet_bytes: bytes,
    money_columns: list[str],
    date_column: str | None = None,
    breakdown_column: str | None = None,
) -> dict[str, Any]:
    """The figures a dashboard shows without being asked."""
    connection, columns = _connect(parquet_bytes)
    try:
        kpis: dict[str, Any] = {"row_count": connection.execute("select count(*) from dataset").fetchone()[0]}

        for name in money_columns:
            if name not in columns:
                continue
            total, average, minimum, maximum, negatives = connection.execute(
                f'select sum("{name}"), avg("{name}"), min("{name}"), max("{name}"), '
                f'count(*) filter (where "{name}" < 0) from dataset'
            ).fetchone()
            kpis[name] = {
                "total": round(float(total or 0), 2),
                "average": round(float(average or 0), 2),
                "minimum": round(float(minimum or 0), 2),
                "maximum": round(float(maximum or 0), 2),
                "negative_rows": negatives,
            }

        if date_column and date_column in columns:
            earliest, latest, months = connection.execute(
                f'select min("{date_column}"), max("{date_column}"), '
                f'count(distinct substr(cast("{date_column}" as varchar), 1, 7)) from dataset'
            ).fetchone()
            kpis["period"] = {
                "earliest": str(earliest) if earliest else None,
                "latest": str(latest) if latest else None,
                "distinct_months": months,
            }

        if breakdown_column and breakdown_column in columns and money_columns:
            metric = money_columns[0]
            rows = connection.execute(
                f'select "{breakdown_column}" as label, sum("{metric}") as total, count(*) as rows '
                f'from dataset group by "{breakdown_column}" order by abs(sum("{metric}")) desc limit 10'
            ).fetchall()
            kpis["top_by_" + breakdown_column] = [
                {"label": label, "total": round(float(total or 0), 2), "rows": row_count}
                for label, total, row_count in rows
            ]

        return kpis
    finally:
        connection.close()


def reconcile(
    parquet_a: bytes,
    parquet_b: bytes,
    key_columns: list[str],
    amount_column: str,
    tolerance: float = 0.01,
) -> dict[str, Any]:
    """
    Two-sided reconciliation: ledger against bank, invoices against payments.

    Three buckets, and the third is the one that matters. Matched-with-a-
    difference is where the real work is -- an unmatched row is obvious, but a
    row that matched on reference and differs by £4.20 is the kind of thing that
    survives to year end.
    """
    import duckdb
    import polars as pl

    left = pl.read_parquet(io.BytesIO(parquet_a))
    right = pl.read_parquet(io.BytesIO(parquet_b))

    for name in [*key_columns, amount_column]:
        if name not in left.columns:
            raise QueryError(f"source A has no column {name!r}")
        if name not in right.columns:
            raise QueryError(f"source B has no column {name!r}")

    connection = duckdb.connect(":memory:")
    try:
        connection.register("a", left)
        connection.register("b", right)

        join = " and ".join(f'a."{name}" = b."{name}"' for name in key_columns)
        keys = ", ".join(f'a."{name}" as "{name}"' for name in key_columns)
        keys_b = ", ".join(f'b."{name}" as "{name}"' for name in key_columns)

        matched = connection.execute(
            f'select {keys}, a."{amount_column}" as amount_a, b."{amount_column}" as amount_b, '
            f'a."{amount_column}" - b."{amount_column}" as difference '
            f"from a join b on {join}"
        ).fetchall()
        matched_columns = [description[0] for description in connection.description]

        # `not exists` rather than a left join with a null test: the join
        # duplicates a left row once per right match, which would report a
        # perfectly matched row as unmatched the moment the key is not unique.
        only_a = connection.execute(
            f'select {keys}, a."{amount_column}" as amount from a '
            f"where not exists (select 1 from b where {join})"
        ).fetchall()
        only_a_columns = [description[0] for description in connection.description]

        only_b = connection.execute(
            f'select {keys_b}, b."{amount_column}" as amount from b '
            f"where not exists (select 1 from a where {join})"
        ).fetchall()
        only_b_columns = [description[0] for description in connection.description]

        matched_rows = [dict(zip(matched_columns, row)) for row in matched]
        exact = [row for row in matched_rows if abs(float(row["difference"] or 0)) <= tolerance]
        differing = [row for row in matched_rows if abs(float(row["difference"] or 0)) > tolerance]

        return {
            "matched": len(exact),
            "matched_with_difference": len(differing),
            "only_in_a": len(only_a),
            "only_in_b": len(only_b),
            "difference_total": round(sum(float(row["difference"] or 0) for row in differing), 2),
            "unmatched_value_a": round(
                sum(float(row[-1] or 0) for row in only_a), 2
            ),
            "unmatched_value_b": round(
                sum(float(row[-1] or 0) for row in only_b), 2
            ),
            "examples": {
                "differing": [_jsonable(row) for row in differing[:10]],
                "only_in_a": [_jsonable(dict(zip(only_a_columns, row))) for row in only_a[:10]],
                "only_in_b": [_jsonable(dict(zip(only_b_columns, row))) for row in only_b[:10]],
            },
            "key_columns": key_columns,
            "amount_column": amount_column,
            "tolerance": tolerance,
        }
    finally:
        connection.close()


__all__ = [
    "PeriodComparison",
    "QueryError",
    "QueryResult",
    "compare_periods",
    "compile_query",
    "headline_kpis",
    "reconcile",
    "run_query",
]
