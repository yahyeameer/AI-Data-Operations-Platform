"""
Job handlers: one per kind in the queue.

Each handler takes a claimed job and returns a JSON-serialisable result that
lands in `agent_jobs.result` and is read straight by the dashboard. The
handlers are where the tools in `hermes/tools/` meet the database, and they are
deliberately the only place that does both -- a tool never talks to Supabase,
and a handler never implements a transformation.

The pipeline chains rather than doing everything in one job, and after the
first month it takes a different route entirely:

    month 1   parse -> profile -> propose -> (a person approves) -> apply
                                                                      |
                                                             a recipe is saved
                                                                      |
    month 2+  parse -> (signature matches the recipe) -> replay -> deviations only

Which route a file takes is decided by `parse_workbook`, from the source
signature it computes. That branch is MVP criterion 6.

Each step is separately retryable and separately visible. A profiling failure
does not discard a four-minute parse, and the dashboard can show which stage a
dataset has reached instead of one opaque "working…".
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .llm.redact import build_context
from .llm.router import LLMRouter
from .supabase import SupabaseClient, SupabaseError
from .tools import analyze, report
from .tools.clean import apply_operations, column_hash, to_parquet
from .tools.parse import ParsedTable, SheetInterpretation, SkippedRow, parse_workbook
from .tools.profile import Profile, profile_table
from .tools.propose import build_proposals, summarise
from .tools.recipe import (
    build_vocabulary_entries,
    capture_steps,
    check_invariants,
    default_invariants,
    invariant_status,
    replay,
)

log = logging.getLogger("hermes.jobs")

RAW_BUCKET = "raw"
PARQUET_BUCKET = "parquet"
EXPORTS_BUCKET = "exports"


class JobError(RuntimeError):
    """
    A failure whose message is safe and useful to show the accountant.

    Distinct from an unexpected exception: "Legacy .xls files are not supported"
    belongs on screen, whereas a KeyError does not. The worker shows the first
    verbatim and replaces the second with a generic message plus a log line.

    `retryable` defaults to False because a JobError describes a *conclusion*,
    not an accident -- the file really is an .xls, the blocking issue really is
    unresolved, and running it twice more produces the same sentence three
    times while the accountant waits to read it once. Failures worth retrying
    are the ones that raise something else.
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class JobContext:
    config: Config
    supabase: SupabaseClient
    llm: LLMRouter
    job: dict[str, Any]
    # Extends the lease and reports progress. Called by anything slow enough to
    # risk the lease expiring underneath it.
    heartbeat: Callable[[dict[str, Any]], None]

    @property
    def job_id(self) -> str:
        return self.job["id"]

    @property
    def workspace_id(self) -> str:
        return self.job["workspace_id"]

    @property
    def payload(self) -> dict[str, Any]:
        return self.job.get("payload") or {}

    def requested_by(self) -> str | None:
        return self.job.get("requested_by")


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------


def _load_version(context: JobContext, version_id: str) -> dict[str, Any]:
    rows = context.supabase.select(
        "dataset_versions",
        columns=(
            "id,dataset_id,version_no,kind,parquet_path,row_count,"
            "parent_version_id,produced_by_run_id"
        ),
        filters={"id": f"eq.{version_id}"},
        limit=1,
    )
    if not rows:
        raise JobError(f"dataset version {version_id} no longer exists")
    return rows[0]


def _load_parquet(context: JobContext, version: dict[str, Any]) -> bytes:
    path = version.get("parquet_path")
    if not path:
        raise JobError(
            "This dataset version has no parsed data yet. Run the parser on the upload first."
        )

    try:
        return context.supabase.download(
            PARQUET_BUCKET, path, context.config.max_download_bytes
        )
    except SupabaseError as error:
        # A missing object is a different problem from an unreachable one, and
        # conflating them sends the reader to the wrong place. Storage returns
        # the "not found" inside a 400 body rather than as a 404 status, so the
        # body is what has to be inspected.
        missing = error.status == 404 or "not_found" in (error.body or "")
        if missing:
            raise JobError(
                "The stored data for this dataset version is missing from storage. "
                "Re-run the parser on the original upload to rebuild it."
            ) from error
        # Anything else is transient until proven otherwise, so it retries.
        raise JobError(
            f"Could not read the stored data for this version ({error.status}). "
            f"This will be retried.",
            retryable=True,
        ) from error


def _parquet_path(org_id: str, workspace_id: str, dataset_id: str, job_id: str) -> str:
    """
    Mirrors the raw bucket's layout: org first, then workspace.

    The storage policy reads the tenant out of the first two path segments, so
    a derived object that did not follow the same shape would be unreadable by
    the very users who own it.

    Keyed by job rather than by version number. The version number is allocated
    inside the database transaction that records the version, which is *after*
    the object has to exist -- so naming the object by a predicted version
    number is a guess, and two uploads into one dataset would guess the same
    number and silently overwrite each other's Parquet. The job id is already
    unique, and it makes the object traceable back to the run that wrote it.
    """
    period = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    return f"{org_id}/{workspace_id}/{period}/{dataset_id}__{job_id}.parquet"


def _stored_interpretation(context: JobContext, version: dict[str, Any]) -> dict[str, Any] | None:
    """
    Recover the parser's structural findings for a version.

    Parquet stores the table. It cannot store what the parser *learned* getting
    there -- that the header was on row 5, that rows 12 and 19 were summary
    rows declaring their own totals, that the date column was ambiguous and
    read day-first, that the amounts arrived as text with parentheses.

    Losing that is not cosmetic. The declared-totals invariant reads the
    summary rows, and it is the check that catches a file whose own arithmetic
    disagrees with ours -- the single most valuable finding the parser makes.
    Without this lookup, profiling a stored version silently skips it.

    `dataset_versions.produced_by_run_id` already points at the job that wrote
    the version, and that job's result holds the interpretation, so nothing new
    needs storing -- it only needs reading.

    Restricted to parse jobs on purpose. A version produced by *cleaning* has
    deliberately different rows from the file it came from, so re-running the
    file's own totals check against it would report a discrepancy that the
    accountant themselves approved.
    """
    job_id = version.get("produced_by_run_id")
    if not job_id:
        return None

    rows = context.supabase.select(
        "agent_jobs", columns="id,kind,result", filters={"id": f"eq.{job_id}"}, limit=1
    )
    if not rows or rows[0].get("kind") != "parse_workbook":
        return None

    result = rows[0].get("result") or {}
    sheets = (result.get("interpretation") or {}).get("sheets") or []
    return sheets[0] if sheets else None


def _profile_from_parquet(
    parquet_bytes: bytes, stored_interpretation: dict[str, Any] | None = None
) -> tuple[Profile, ParsedTable]:
    """
    Rebuild a profile from a stored version.

    Profiling reads the same code path whether the data has just been parsed or
    was written last month, which is what keeps a month-2 profile comparable
    with a month-1 one. Anything else and the invariants in section 5.3 would
    be comparing two different measurements.
    """
    import io

    import polars as pl

    frame = pl.read_parquet(io.BytesIO(parquet_bytes))
    source_rows = (
        frame["__source_row"].to_list() if "__source_row" in frame.columns else list(range(frame.height))
    )
    columns = {
        name: frame[name].to_list() for name in frame.columns if name != "__source_row"
    }
    return _profile_from_columns(columns, source_rows, stored_interpretation)


def _profile_from_columns(
    columns: dict[str, list[Any]],
    source_rows: list[int],
    stored_interpretation: dict[str, Any] | None = None,
) -> tuple[Profile, ParsedTable]:
    """
    Profile an in-memory table.

    Split out from the Parquet path so that a result that has just been
    computed can be profiled without a serialise-and-reread round trip. Recipe
    capture needs exactly that: the invariants for the recipe are derived from
    the cleaned output, which at that point exists only as columns in memory.
    """
    # A synthetic interpretation: the structural work happened at parse time and
    # is recorded on the version, but profiling only needs the column list and
    # the types.
    from .tools.parse import ColumnInterpretation

    business = [name for name in columns if not name.startswith("__raw_")]
    row_count = len(source_rows)

    column_interpretations = []
    for index, name in enumerate(business):
        values = columns[name]
        non_null = [value for value in values if value is not None]
        inferred = "text"
        if non_null and all(isinstance(value, bool) for value in non_null):
            inferred = "boolean"
        elif non_null and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in non_null
        ):
            inferred = "number"
        elif _looks_iso_date(values):
            inferred = "date"

        column_interpretations.append(
            ColumnInterpretation(
                index=index,
                source_header=name.replace("_", " ").title(),
                name=name,
                inferred_type=inferred,  # type: ignore[arg-type]
                type_confidence=1.0,
                non_null=len(non_null),
                parse_failures=0,
            )
        )

    skipped: list[SkippedRow] = []
    notes: list[str] = []

    if stored_interpretation:
        _restore_column_metadata(column_interpretations, stored_interpretation)
        skipped = [
            SkippedRow(
                source_row=int(entry.get("source_row", 0)),
                reason=entry.get("reason", "blank"),
                preview=entry.get("preview", ""),
            )
            for entry in stored_interpretation.get("skipped") or []
        ]
        notes = list(stored_interpretation.get("notes") or [])

    interpretation = SheetInterpretation(
        sheet_name=(stored_interpretation or {}).get("sheet_name") or "dataset",
        header_row=(stored_interpretation or {}).get("header_row") or 1,
        first_data_row=2,
        last_data_row=row_count + 1,
        first_column=1,
        last_column=len(column_interpretations),
        data_rows=row_count,
        columns=column_interpretations,
        skipped=skipped,
        confidence=(stored_interpretation or {}).get("confidence") or 1.0,
        notes=notes,
    )

    table = ParsedTable(interpretation=interpretation, columns=columns, source_rows=source_rows)
    return profile_table(table, context_max_samples()), table


def _restore_column_metadata(
    columns: list[Any], stored_interpretation: dict[str, Any]
) -> None:
    """
    Put the parser's per-column findings back onto the rebuilt columns.

    Matched by name, not position: a cleaning step may have dropped a column,
    and re-applying the fourth column's date convention to what is now a
    different fourth column would be worse than having no metadata at all.

    The *type* is not restored. Parquet's schema is the authority on what the
    stored data actually is, and if a cleaning step turned a text column into
    numbers then the stored type is right and the parse-time one is stale.
    """
    by_name = {
        column.get("name"): column for column in stored_interpretation.get("columns") or []
    }

    for column in columns:
        stored = by_name.get(column.name)
        if not stored:
            continue
        column.source_header = stored.get("source_header") or column.source_header
        column.type_confidence = stored.get("type_confidence", column.type_confidence)
        column.number_styles = list(stored.get("number_styles") or [])
        column.date_order = stored.get("date_order")
        column.ambiguous_dates = stored.get("ambiguous_dates", 0)
        column.parse_failures = stored.get("parse_failures", 0)
        column.failure_samples = list(stored.get("failure_samples") or [])


def context_max_samples() -> int:
    return 5


def _looks_iso_date(values: list[Any]) -> bool:
    sample = [value for value in values if isinstance(value, str)][:20]
    if not sample:
        return False
    return all(
        len(value) == 10 and value[4] == "-" and value[7] == "-" for value in sample
    )


def _money_columns(profile: Profile) -> list[str]:
    return [column.name for column in profile.columns if column.is_money and column.inferred_type == "number"]


def _first_of(profile: Profile, kind: str) -> str | None:
    return next((column.name for column in profile.columns if column.inferred_type == kind), None)


def _categorical(profile: Profile) -> str | None:
    """The best column to break figures down by: text, repeated, not a reference."""
    candidates = [
        column
        for column in profile.columns
        if column.inferred_type == "text"
        and column.non_null
        and 1 < column.distinct_count < max(2, column.non_null * 0.8)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda column: column.distinct_count).name


# -----------------------------------------------------------------------------
# parse_workbook
# -----------------------------------------------------------------------------


def handle_parse_workbook(context: JobContext) -> dict[str, Any]:
    upload_id = context.job.get("raw_upload_id")
    if not upload_id:
        raise JobError("this job has no upload attached")

    uploads = context.supabase.select(
        "raw_uploads",
        columns="id,workspace_id,dataset_id,storage_path,original_filename,status,byte_size",
        filters={"id": f"eq.{upload_id}"},
        limit=1,
    )
    if not uploads:
        raise JobError("the upload no longer exists")
    upload = uploads[0]

    if upload["status"] != "stored":
        raise JobError(f"the upload is {upload['status']}, not stored; nothing to parse")
    if not upload.get("dataset_id"):
        raise JobError("this upload is not attached to a dataset")

    context.heartbeat({"stage": "downloading", "file": upload["original_filename"]})
    data = context.supabase.download(
        RAW_BUCKET, upload["storage_path"], context.config.max_download_bytes
    )

    context.heartbeat({"stage": "parsing", "bytes": len(data)})
    try:
        parsed = parse_workbook(data, upload["original_filename"])
    except ValueError as error:
        # ValueError from the parser is always a message written for a human.
        raise JobError(str(error)) from error

    table = parsed.primary

    context.heartbeat({"stage": "writing", "rows": table.row_count})

    org_id = context.job["org_id"]
    dataset_id = upload["dataset_id"]

    # Allocate the version first so the object key can carry its number, then
    # upload, then... except that ordering would leave a version pointing at an
    # object that failed to upload. So: upload to a path derived from the row
    # count and time, then record the version pointing at it. A stray object
    # with no version is inert; a version with no object is not.
    parquet_bytes = to_parquet(table.columns, table.source_rows)
    object_path = _parquet_path(org_id, context.workspace_id, dataset_id, context.job_id)
    stored = context.supabase.upload(
        PARQUET_BUCKET,
        object_path,
        parquet_bytes,
        content_type="application/vnd.apache.parquet",
        upsert=True,
    )

    version = context.supabase.rpc(
        "record_dataset_version",
        {
            "p_dataset_id": dataset_id,
            "p_kind": "cleaned",
            "p_parquet_path": stored.path,
            "p_row_count": table.row_count,
            "p_column_hash": column_hash(table.columns),
            "p_raw_upload_id": upload_id,
            "p_produced_by_job": context.job_id,
            "p_created_by": context.requested_by(),
            "p_metadata": {
                "stage": "parsed",
                "source_signature": parsed.source_signature,
                "confidence": table.interpretation.confidence,
                "sheet": table.interpretation.sheet_name,
                "header_row": table.interpretation.header_row,
                "excluded_rows": len(table.interpretation.skipped),
            },
        },
    )

    context.supabase.rpc(
        "set_dataset_signature",
        {"p_dataset_id": dataset_id, "p_signature": parsed.source_signature},
    )

    # The branch that makes month 2 cheap (MVP criterion 6). If this file's
    # shape matches a recipe the workspace already has, replay it and surface
    # only what deviates; otherwise fall through to profiling and proposing
    # from scratch, which is what month 1 needs.
    matched = context.supabase.rpc(
        "match_recipe",
        {"p_workspace_id": context.workspace_id, "p_source_signature": parsed.source_signature},
    )
    recipe = matched[0] if isinstance(matched, list) and matched else None

    next_kind = "replay_recipe" if recipe else "profile_dataset"

    # Higher priority than a fresh parse so a pipeline in flight finishes
    # before another one starts, which keeps the dashboard's per-dataset
    # progress monotonic.
    context.supabase.rpc(
        "enqueue_agent_job_internal",
        {
            "p_workspace_id": context.workspace_id,
            "p_kind": next_kind,
            "p_dataset_id": dataset_id,
            "p_dataset_version_id": version["id"],
            "p_requested_by": context.requested_by(),
            "p_priority": 50,
        },
    )

    return {
        "dataset_version_id": version["id"],
        "version_no": version["version_no"],
        "parquet_path": stored.path,
        "rows": table.row_count,
        "columns": len(table.interpretation.columns),
        "source_signature": parsed.source_signature,
        "confidence": table.interpretation.confidence,
        "interpretation": parsed.to_dict(),
        "matched_recipe": (
            {
                "recipe_id": recipe["recipe_id"],
                "name": recipe["recipe_name"],
                "version_no": recipe["version_no"],
                "previous_runs": recipe["run_count"],
            }
            if recipe
            else None
        ),
        "next": next_kind,
    }


# -----------------------------------------------------------------------------
# profile_dataset
# -----------------------------------------------------------------------------


def handle_profile_dataset(context: JobContext) -> dict[str, Any]:
    version_id = context.job.get("dataset_version_id")
    if not version_id:
        raise JobError("this job has no dataset version attached")

    version = _load_version(context, version_id)
    context.heartbeat({"stage": "downloading"})
    parquet_bytes = _load_parquet(context, version)

    context.heartbeat({"stage": "profiling"})
    profile, _table = _profile_from_parquet(
        parquet_bytes, _stored_interpretation(context, version)
    )

    context.supabase.rpc(
        "record_dataset_profile",
        {
            "p_dataset_version_id": version_id,
            "p_row_count": profile.row_count,
            "p_column_count": profile.column_count,
            "p_columns": [column.__dict__ for column in profile.columns],
            "p_signals": profile.signals,
            "p_job_id": context.job_id,
        },
    )

    context.supabase.rpc(
        "enqueue_agent_job_internal",
        {
            "p_workspace_id": context.workspace_id,
            "p_kind": "propose_cleaning",
            "p_dataset_id": version["dataset_id"],
            "p_dataset_version_id": version_id,
            "p_requested_by": context.requested_by(),
            "p_priority": 50,
        },
    )

    return {
        "dataset_version_id": version_id,
        "rows": profile.row_count,
        "columns": profile.column_count,
        "signals": profile.signals,
        "next": "propose_cleaning",
    }


# -----------------------------------------------------------------------------
# propose_cleaning
# -----------------------------------------------------------------------------


def handle_propose_cleaning(context: JobContext) -> dict[str, Any]:
    version_id = context.job.get("dataset_version_id")
    if not version_id:
        raise JobError("this job has no dataset version attached")

    version = _load_version(context, version_id)
    parquet_bytes = _load_parquet(context, version)

    context.heartbeat({"stage": "profiling"})
    profile, table = _profile_from_parquet(
        parquet_bytes, _stored_interpretation(context, version)
    )

    context.heartbeat({"stage": "proposing"})
    proposals = build_proposals(table, profile)
    rows = [
        proposal.to_row(context.workspace_id, version_id, context.job_id)
        for proposal in proposals
    ]

    # The model rewrites the wording, never the decision. Failure here costs
    # prose and nothing else.
    model_used = None
    if proposals and context.llm.enabled:
        context.heartbeat({"stage": "explaining"})
        redacted = build_context(
            profile,
            max_sample_values=context.config.max_sample_values,
            redact_samples=context.config.redact_samples,
        )
        rationales, model_used = context.llm.explain_proposals(redacted, rows)
        for row in rows:
            improved = rationales.get(row["group_key"])
            if improved:
                row["rationale"] = improved

    count = context.supabase.rpc(
        "replace_proposed_changes",
        {
            "p_dataset_version_id": version_id,
            "p_job_id": context.job_id,
            "p_proposals": rows,
        },
    )

    summary = summarise(proposals)
    summary["model_used"] = model_used
    summary["stored"] = count

    return {
        "dataset_version_id": version_id,
        "proposals": count,
        "summary": summary,
    }


# -----------------------------------------------------------------------------
# apply_cleaning
# -----------------------------------------------------------------------------


def handle_apply_cleaning(context: JobContext) -> dict[str, Any]:
    """
    Apply what a human approved, and nothing else.

    The approved set is read from the database rather than taken from the job
    payload. A payload could be stale, or could name a group the accountant
    rejected thirty seconds ago; the table is the record of what was decided.
    """
    version_id = context.job.get("dataset_version_id")
    if not version_id:
        raise JobError("this job has no dataset version attached")

    version = _load_version(context, version_id)

    approved = context.supabase.select(
        "proposed_changes",
        columns="id,group_key,step_type,operation,confidence,affected_rows",
        filters={
            "dataset_version_id": f"eq.{version_id}",
            "status": "eq.approved",
        },
        order="created_at.asc",
    )
    if not approved:
        raise JobError("nothing has been approved for this version yet")

    blocking = context.supabase.select(
        "proposed_changes",
        columns="id,group_key,title",
        filters={
            "dataset_version_id": f"eq.{version_id}",
            "confidence": "eq.low",
            "status": "eq.pending",
        },
    )
    if blocking and not context.payload.get("override_block"):
        # Section 5.1: a Block-tier finding halts the run. It can be overridden,
        # but only deliberately and only with the override recorded on the job.
        titles = "; ".join(item["title"] for item in blocking)
        raise JobError(
            f"{len(blocking)} blocking issue(s) are unresolved and must be approved or rejected "
            f"first: {titles}"
        )

    context.heartbeat({"stage": "downloading"})
    parquet_bytes = _load_parquet(context, version)
    _profile, table = _profile_from_parquet(
        parquet_bytes, _stored_interpretation(context, version)
    )

    context.heartbeat({"stage": "applying", "operations": len(approved)})
    operations = [item["operation"] for item in approved]
    result = apply_operations(table, operations)

    context.heartbeat({"stage": "writing", "rows": result.row_count})
    new_parquet = to_parquet(result.columns, result.source_rows)
    path = _parquet_path(
        context.job["org_id"],
        context.workspace_id,
        version["dataset_id"],
        context.job_id,
    )
    stored = context.supabase.upload(
        PARQUET_BUCKET, path, new_parquet, content_type="application/vnd.apache.parquet", upsert=True
    )

    new_version = context.supabase.rpc(
        "record_dataset_version",
        {
            "p_dataset_id": version["dataset_id"],
            "p_kind": "cleaned",
            "p_parquet_path": stored.path,
            "p_row_count": result.row_count,
            "p_column_hash": column_hash(result.columns),
            "p_parent_version_id": version_id,
            "p_produced_by_job": context.job_id,
            "p_created_by": context.requested_by(),
            "p_metadata": {
                "stage": "cleaned",
                "applied_groups": [item["group_key"] for item in approved],
                "rows_in": table.row_count,
                "rows_out": result.row_count,
                "override_block": bool(context.payload.get("override_block")),
            },
        },
    )

    context.supabase.rpc(
        "mark_changes_applied",
        {
            "p_dataset_version_id": version_id,
            "p_group_keys": [item["group_key"] for item in approved],
        },
    )

    # Profile the output too. Section 5.3's invariants compare a run's result
    # against what came before, and that comparison needs both sides measured
    # the same way.
    context.supabase.rpc(
        "enqueue_agent_job_internal",
        {
            "p_workspace_id": context.workspace_id,
            "p_kind": "profile_dataset",
            "p_dataset_id": version["dataset_id"],
            "p_dataset_version_id": new_version["id"],
            "p_requested_by": context.requested_by(),
            "p_priority": 50,
        },
    )

    # Capture what was approved as a recipe (MVP criterion 5). This is the
    # moment month 1 becomes reusable, and it is deliberately automatic: an
    # accountant who has just spent twenty minutes approving changes should not
    # then have to remember to press "save as recipe".
    recipe = _capture_recipe_from(context, version, approved, table, result)

    summary = result.summary()
    summary["dataset_version_id"] = new_version["id"]
    summary["version_no"] = new_version["version_no"]
    summary["rows_in"] = table.row_count
    summary["recipe"] = recipe
    return summary


def _capture_recipe_from(
    context: JobContext,
    version: dict[str, Any],
    approved: list[dict[str, Any]],
    table: ParsedTable,
    result: Any,
) -> dict[str, Any] | None:
    """
    Write the approved operations down as a recipe.

    The one non-obvious step is what happens to an entity merge. Approving
    "merge CONTOSO LIMITED into Contoso Ltd." produces an inline mapping, and
    freezing that into the recipe would mean every new supplier next month
    needs a new recipe version. Instead the pairs go into a workspace mapping
    table and the step keeps only a reference to it — so the step reads
    whatever the table knows at replay time, and section 4's "shared, growable"
    requirement is satisfied by construction rather than by intent.
    """
    dataset_id = version["dataset_id"]

    datasets = context.supabase.select(
        "datasets", columns="id,name,source_signature", filters={"id": f"eq.{dataset_id}"}, limit=1
    )
    if not datasets:
        return None
    dataset = datasets[0]
    signature = dataset.get("source_signature")

    # Move every inline entity mapping into a workspace mapping table.
    mapping_table_ids: dict[str, str] = {}
    for item in approved:
        operation = item.get("operation") or {}
        if operation.get("op") != "map_values":
            continue
        column = operation.get("column")
        mapping = operation.get("mapping") or {}
        if not column or not mapping:
            continue

        table_row = context.supabase.rpc(
            "ensure_mapping_table",
            {
                "p_workspace_id": context.workspace_id,
                "p_name": f"{column} mappings",
                "p_kind": "entity",
                "p_created_by": context.requested_by(),
            },
        )
        table_id = table_row["id"]
        mapping_table_ids[column] = table_id

        # Both halves of the vocabulary: the merges a person approved, and an
        # identity entry for every value that survived cleaning. Without the
        # second half the table holds only corrections, and next month every
        # supplier that was always spelled correctly would be reported as new.
        entries = build_vocabulary_entries(result.columns.get(column, []), mapping)

        confirmed = [entry for entry in entries if entry.get("confirmed")]
        observed = [entry for entry in entries if not entry.get("confirmed")]

        if confirmed:
            context.supabase.rpc(
                "upsert_mapping_entries",
                {
                    "p_mapping_table_id": table_id,
                    "p_entries": confirmed,
                    # A person approved this merge, which is stronger evidence
                    # than anything the run inferred on its own.
                    "p_confirmed_by": context.requested_by(),
                },
            )
        if observed:
            context.supabase.rpc(
                "upsert_mapping_entries",
                {"p_mapping_table_id": table_id, "p_entries": observed, "p_confirmed_by": None},
            )

    steps = capture_steps(approved, mapping_table_ids)
    if not steps:
        return None

    # Invariants are derived from the cleaned output, because that is the shape
    # future months are expected to match.
    cleaned_profile, _cleaned_table = _profile_from_columns(result.columns, result.source_rows)
    invariants = default_invariants(cleaned_profile)

    captured = context.supabase.rpc(
        "capture_recipe",
        {
            "p_workspace_id": context.workspace_id,
            "p_dataset_id": dataset_id,
            "p_source_signature": signature,
            "p_name": f"{dataset['name']} cleanup",
            "p_steps": steps,
            "p_invariants": invariants,
            "p_change_note": f"Learned from the review of version {version['version_no']}",
            "p_learned_from": context.job_id,
            "p_created_by": context.requested_by(),
        },
    )

    return {
        "recipe_version_id": captured["id"],
        "version_no": captured["version_no"],
        "steps": len(steps),
        "invariants": len(invariants),
        "mapping_tables": mapping_table_ids,
    }


# -----------------------------------------------------------------------------
# replay_recipe
# -----------------------------------------------------------------------------


def handle_replay_recipe(context: JobContext) -> dict[str, Any]:
    """
    Month 2 (MVP criterion 6).

    Instead of profiling from scratch and asking the accountant to approve the
    same eight things again, the recipe learned last month runs against this
    month's file and reports only what it could not handle.

    The order of the last three operations is the part that matters. Apply the
    steps, then check the invariants, and only then decide whether to write an
    output version. A run that fails an invariant must produce nothing: a
    cleaned version sitting in storage is a version something downstream will
    eventually read, and "we wrote it but marked the run blocked" is the shape
    of every silent-failure incident this product exists to prevent.
    """
    version_id = context.job.get("dataset_version_id")
    if not version_id:
        raise JobError("this job has no dataset version attached")

    version = _load_version(context, version_id)

    datasets = context.supabase.select(
        "datasets",
        columns="id,name,source_signature",
        filters={"id": f"eq.{version['dataset_id']}"},
        limit=1,
    )
    signature = datasets[0].get("source_signature") if datasets else None

    matched = context.supabase.rpc(
        "match_recipe",
        {"p_workspace_id": context.workspace_id, "p_source_signature": signature},
    )
    recipe = matched[0] if isinstance(matched, list) and matched else None
    if not recipe:
        raise JobError(
            "No recipe matches this file's layout. Analyse it from scratch and approve the "
            "changes; that will save a recipe for next month."
        )

    context.heartbeat({"stage": "downloading", "recipe": recipe["recipe_name"]})
    parquet_bytes = _load_parquet(context, version)
    profile, table = _profile_from_parquet(
        parquet_bytes, _stored_interpretation(context, version)
    )

    # Everything the workspace currently knows about this client's vocabulary.
    # Read now rather than baked into the recipe: that is the whole point of a
    # growable mapping table, and it is what makes last month's resolutions
    # apply to this month's file.
    mappings = _load_mappings(context, recipe["steps"])

    run = context.supabase.rpc(
        "start_recipe_run",
        {
            "p_workspace_id": context.workspace_id,
            "p_recipe_version_id": recipe["recipe_version_id"],
            "p_dataset_version_in": version_id,
            "p_job_id": context.job_id,
        },
    )

    try:
        context.heartbeat({"stage": "replaying", "steps": len(recipe["steps"])})
        # The recipe already records the columns it was learned against, in its
        # columns_present invariant. Reusing that beats inferring the
        # expectation from step parameters, which would report every column
        # that needed no cleaning as new, every month.
        expected_columns = next(
            (
                invariant.get("columns")
                for invariant in (recipe.get("invariants") or [])
                if invariant.get("type") == "columns_present"
            ),
            None,
        )
        result = replay(
            table, recipe["steps"], profile, mappings, expected_columns=expected_columns
        )

        context.heartbeat({"stage": "checking invariants"})
        outcomes, invariant_deviations = check_invariants(
            recipe.get("invariants") or [], result.cleaned, profile
        )
        result.deviations.extend(invariant_deviations)
        result.invariants = outcomes

        deviation_rows = [deviation.to_row() for deviation in result.deviations]
        if deviation_rows:
            context.supabase.rpc(
                "record_deviations", {"p_run_id": run["id"], "p_deviations": deviation_rows}
            )

        # Bump hit counts so a mapping entry can show it is earning its keep.
        for table_id, keys in result.mapping_hits.items():
            context.supabase.rpc(
                "record_mapping_hits", {"p_mapping_table_id": table_id, "p_source_keys": keys}
            )

        summary = result.summary()
        status_text = invariant_status(outcomes)

        if result.blocked:
            status = "blocked"
            output_version = None
        elif result.needs_review:
            status = "needs_review"
            output_version = None
        else:
            context.heartbeat({"stage": "writing", "rows": result.cleaned.row_count})
            new_parquet = to_parquet(result.cleaned.columns, result.cleaned.source_rows)
            path = _parquet_path(
                context.job["org_id"],
                context.workspace_id,
                version["dataset_id"],
                context.job_id,
            )
            stored = context.supabase.upload(
                PARQUET_BUCKET,
                path,
                new_parquet,
                content_type="application/vnd.apache.parquet",
                upsert=True,
            )
            written = context.supabase.rpc(
                "record_dataset_version",
                {
                    "p_dataset_id": version["dataset_id"],
                    "p_kind": "cleaned",
                    "p_parquet_path": stored.path,
                    "p_row_count": result.cleaned.row_count,
                    "p_column_hash": column_hash(result.cleaned.columns),
                    "p_parent_version_id": version_id,
                    "p_produced_by_job": context.job_id,
                    "p_created_by": context.requested_by(),
                    "p_metadata": {
                        "stage": "cleaned",
                        "replayed_recipe": recipe["recipe_name"],
                        "recipe_version_no": recipe["version_no"],
                        "automation_rate": result.automation_rate,
                    },
                },
            )
            status = "succeeded"
            output_version = written["id"]

        context.supabase.rpc(
            "finish_recipe_run",
            {
                "p_run_id": run["id"],
                "p_status": status,
                "p_dataset_version_out": output_version,
                "p_rows_processed": result.rows_processed,
                "p_rows_matched": result.rows_matched,
                "p_auto_corrections": result.auto_corrections,
                "p_deviations_count": len(result.deviations),
                "p_automation_rate": result.automation_rate,
                "p_invariant_status": status_text,
                "p_summary": summary,
            },
        )

    except Exception as error:
        # A run row that stays 'running' forever is worse than one marked
        # failed: the dashboard cannot tell it apart from work in progress.
        context.supabase.rpc(
            "finish_recipe_run",
            {
                "p_run_id": run["id"],
                "p_status": "failed",
                "p_summary": {"error": str(error)[:500]},
            },
        )
        raise

    return {
        "run_id": run["id"],
        "recipe": recipe["recipe_name"],
        "recipe_version_no": recipe["version_no"],
        "status": status,
        "dataset_version_out": output_version,
        "rows_processed": result.rows_processed,
        "auto_corrections": result.auto_corrections,
        "deviations": len(result.deviations),
        "automation_rate": result.automation_rate,
        "invariants": status_text,
        "summary": summary,
    }


def _load_mappings(context: JobContext, steps: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Current contents of every mapping table the recipe's steps refer to."""
    table_ids = {
        step.get("params", {}).get("mapping_table_id")
        for step in steps
        if step.get("params", {}).get("mapping_table_id")
    }

    mappings: dict[str, dict[str, str]] = {}
    for table_id in table_ids:
        entries = context.supabase.select(
            "mapping_entries",
            columns="source_key,canonical_value",
            filters={"mapping_table_id": f"eq.{table_id}"},
            limit=10000,
        )
        mappings[table_id] = {
            entry["source_key"]: entry["canonical_value"] for entry in entries
        }

    return mappings


# -----------------------------------------------------------------------------
# query_dataset
# -----------------------------------------------------------------------------


def handle_query_dataset(context: JobContext) -> dict[str, Any]:
    """
    Answer a question about a dataset.

    Two entry points, one execution path. A caller may pass a structured `query`
    directly (the dashboard's chart builder does), or a natural-language
    `question` for the model to translate. Either way the spec is compiled and
    validated here before any SQL exists.
    """
    version_id = context.job.get("dataset_version_id")
    if not version_id:
        raise JobError("this job has no dataset version attached")

    version = _load_version(context, version_id)
    parquet_bytes = _load_parquet(context, version)

    spec = context.payload.get("query")
    question = context.payload.get("question")
    model_used = None

    if not spec:
        if not question:
            raise JobError("ask a question or supply a structured query")
        if not context.llm.enabled:
            raise JobError(
                "No reasoning model is configured, so questions in plain English cannot be "
                "translated. Set OPENAI_API_KEY or KIMI_API_KEY on the agent host."
            )

        context.heartbeat({"stage": "planning"})
        profile, _table = _profile_from_parquet(parquet_bytes)
        redacted = build_context(
            profile,
            max_sample_values=context.config.max_sample_values,
            redact_samples=context.config.redact_samples,
        )
        spec, model_used, error = context.llm.plan_query(question, redacted)
        if not spec:
            raise JobError(error or "the question could not be turned into a query")

    context.heartbeat({"stage": "querying"})
    try:
        result = analyze.run_query(parquet_bytes, spec)
    except analyze.QueryError as error:
        raise JobError(str(error)) from error

    run = context.supabase.rpc(
        "record_analysis_run",
        {
            "p_dataset_version_id": version_id,
            "p_question": question,
            "p_executed_sql": result.sql,
            "p_result": {"rows": result.rows, "spec": spec},
            "p_row_refs": result.row_refs,
            "p_model_used": model_used,
            "p_duration_ms": result.duration_ms,
            "p_job_id": context.job_id,
            "p_created_by": context.requested_by(),
        },
    )

    return {
        "analysis_run_id": run["id"],
        "question": question,
        "query": spec,
        "sql": result.sql,
        "rows": result.rows,
        "row_refs": result.row_refs,
        "row_count": result.row_count,
        "duration_ms": result.duration_ms,
        "model_used": model_used,
    }


# -----------------------------------------------------------------------------
# reconcile_sources
# -----------------------------------------------------------------------------


def handle_reconcile_sources(context: JobContext) -> dict[str, Any]:
    version_a = context.job.get("dataset_version_id")
    version_b = context.payload.get("compare_to_version_id")
    if not version_a or not version_b:
        raise JobError("reconciliation needs two dataset versions")

    left = _load_version(context, version_a)
    right = _load_version(context, version_b)

    # Both versions must belong to this workspace. The enqueue RPC checked the
    # first; the second arrived in the payload and has not been checked by
    # anything yet.
    allowed = {
        row["id"]
        for row in context.supabase.select(
            "datasets", columns="id", filters={"workspace_id": f"eq.{context.workspace_id}"}
        )
    }
    if left["dataset_id"] not in allowed or right["dataset_id"] not in allowed:
        raise JobError("both datasets must belong to this workspace")

    context.heartbeat({"stage": "downloading"})
    parquet_a = _load_parquet(context, left)
    parquet_b = _load_parquet(context, right)

    keys = context.payload.get("key_columns")
    amount = context.payload.get("amount_column")

    if not keys or not amount:
        profile, _table = _profile_from_parquet(parquet_a)
        if not keys:
            key = next(
                (
                    column.name
                    for column in profile.columns
                    if column.inferred_type == "text"
                    and column.distinct_count == column.non_null
                    and column.non_null > 0
                ),
                None,
            )
            if not key:
                raise JobError("no unique key column found; name one in key_columns")
            keys = [key]
        if not amount:
            amount = next((name for name in _money_columns(profile)), None)
            if not amount:
                raise JobError("no money column found; name one in amount_column")

    context.heartbeat({"stage": "reconciling"})
    try:
        result = analyze.reconcile(
            parquet_a,
            parquet_b,
            key_columns=keys,
            amount_column=amount,
            tolerance=float(context.payload.get("tolerance", 0.01)),
        )
    except analyze.QueryError as error:
        raise JobError(str(error)) from error

    result["version_a"] = version_a
    result["version_b"] = version_b
    return result


# -----------------------------------------------------------------------------
# generate_report
# -----------------------------------------------------------------------------


def handle_generate_report(context: JobContext) -> dict[str, Any]:
    version_id = context.job.get("dataset_version_id")
    if not version_id:
        raise JobError("this job has no dataset version attached")

    version = _load_version(context, version_id)
    parquet_bytes = _load_parquet(context, version)

    context.heartbeat({"stage": "profiling"})
    profile, _table = _profile_from_parquet(
        parquet_bytes, _stored_interpretation(context, version)
    )

    money = _money_columns(profile)
    date_column = _first_of(profile, "date")
    breakdown = _categorical(profile)

    context.heartbeat({"stage": "computing"})
    kpis = analyze.headline_kpis(parquet_bytes, money, date_column, breakdown)

    comparison: dict[str, Any] | None = None
    compare_to = context.payload.get("compare_to_version_id")
    if compare_to and date_column and money:
        # Month-on-month against another version of the same dataset.
        other = _load_version(context, compare_to)
        other_parquet = _load_parquet(context, other)
        combined = _concat_parquet(parquet_bytes, other_parquet)
        periods = context.payload.get("periods")
        if combined and periods and len(periods) == 2:
            result = analyze.compare_periods(
                combined,
                date_column,
                money[0],
                tuple(periods[0]),
                tuple(periods[1]),
                breakdown_column=breakdown,
            )
            comparison = result.__dict__

    narrative = None
    model_used = None
    if context.llm.enabled:
        context.heartbeat({"stage": "drafting"})
        redacted = build_context(
            profile,
            max_sample_values=context.config.max_sample_values,
            redact_samples=context.config.redact_samples,
        )
        narrative, model_used = context.llm.narrate(
            redacted, {"kpis": kpis, "comparison": comparison}
        )

    dataset_rows = context.supabase.select(
        "datasets", columns="id,name", filters={"id": f"eq.{version['dataset_id']}"}, limit=1
    )
    workspace_rows = context.supabase.select(
        "workspaces", columns="id,name", filters={"id": f"eq.{context.workspace_id}"}, limit=1
    )

    markdown = report.build_markdown_report(
        workspace_name=workspace_rows[0]["name"] if workspace_rows else "Workspace",
        dataset_name=dataset_rows[0]["name"] if dataset_rows else "Dataset",
        version_no=version["version_no"],
        kpis=kpis,
        profile_signals=profile.signals,
        comparison=comparison,
        narrative=narrative,
    )

    period = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    path = (
        f"{context.job['org_id']}/{context.workspace_id}/{period}/"
        f"{version['dataset_id']}__v{version['version_no']}__report.md"
    )
    stored = context.supabase.upload(
        EXPORTS_BUCKET, path, markdown.encode("utf-8"), content_type="text/markdown", upsert=True
    )

    return {
        "report_path": stored.path,
        "bucket": EXPORTS_BUCKET,
        "markdown": markdown,
        "kpis": kpis,
        "comparison": comparison,
        "narrative": narrative,
        "model_used": model_used,
    }


def _concat_parquet(first: bytes, second: bytes) -> bytes | None:
    """
    Stack two versions so one query can span both periods.

    Returns None when the schemas disagree, which is itself the answer: a
    month-on-month comparison across a changed schema is not a comparison, and
    reporting "columns changed" beats reporting a number built from a
    best-effort alignment.
    """
    import io

    import polars as pl

    try:
        left = pl.read_parquet(io.BytesIO(first))
        right = pl.read_parquet(io.BytesIO(second))
        shared = [name for name in left.columns if name in right.columns]
        if not shared:
            return None
        combined = pl.concat([left.select(shared), right.select(shared)], how="vertical_relaxed")
        buffer = io.BytesIO()
        combined.write_parquet(buffer, compression="zstd")
        return buffer.getvalue()
    except Exception as error:  # noqa: BLE001
        log.warning("could not combine versions for comparison: %s", error)
        return None


# -----------------------------------------------------------------------------
# export_dataset
# -----------------------------------------------------------------------------

_EXPORT_CONTENT_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}


def _rows_from_parquet(parquet_bytes: bytes) -> list[dict[str, Any]]:
    """
    A stored version as plain rows, in column order.

    `__source_row` is dropped. It is the provenance link back into the original
    workbook and it is why an approved change can be traced to a line the
    accountant can point at -- but it is an internal join key, and a column of
    integers labelled `__source_row` in a file someone is about to send a client
    is noise at best and confusing at worst.
    """
    import io

    import polars as pl

    frame = pl.read_parquet(io.BytesIO(parquet_bytes))
    columns = [name for name in frame.columns if name != "__source_row"]
    return frame.select(columns).to_dicts()


def handle_export_dataset(context: JobContext) -> dict[str, Any]:
    """
    Write a cleaned version out in a format a person can open.

    Deliberately not a new table. An export is derived from a version that is
    already immutable, so re-running this job on the same version reproduces the
    same file -- there is nothing to record that the version and the format do
    not already say. The path goes onto the job result, which is where
    generate_report puts its own output and where the download route reads from.
    """
    version_id = context.job.get("dataset_version_id")
    if not version_id:
        raise JobError("this job has no dataset version attached")

    fmt = str(context.payload.get("format") or "xlsx").strip().lower()
    if fmt not in _EXPORT_CONTENT_TYPES:
        raise JobError(f"unknown export format {fmt!r}; expected 'xlsx' or 'csv'")

    version = _load_version(context, version_id)
    parquet_bytes = _load_parquet(context, version)

    context.heartbeat({"stage": "reading"})
    rows = _rows_from_parquet(parquet_bytes)

    dataset_rows = context.supabase.select(
        "datasets", columns="id,name", filters={"id": f"eq.{version['dataset_id']}"}, limit=1
    )
    dataset_name = dataset_rows[0]["name"] if dataset_rows else "Dataset"

    context.heartbeat({"stage": "writing"})
    if fmt == "xlsx":
        body = report.rows_to_xlsx(rows, sheet_name=dataset_name)
    else:
        body = report.rows_to_csv(rows)

    period = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    path = (
        f"{context.job['org_id']}/{context.workspace_id}/{period}/"
        f"{version['dataset_id']}__v{version['version_no']}__export.{fmt}"
    )
    stored = context.supabase.upload(
        EXPORTS_BUCKET,
        path,
        body,
        content_type=_EXPORT_CONTENT_TYPES[fmt],
        upsert=True,
    )

    return {
        "export_path": stored.path,
        "bucket": EXPORTS_BUCKET,
        "format": fmt,
        "row_count": len(rows),
        "byte_size": stored.size,
        "dataset_name": dataset_name,
        "version_no": version["version_no"],
    }


HANDLERS: dict[str, Callable[[JobContext], dict[str, Any]]] = {
    "parse_workbook": handle_parse_workbook,
    "profile_dataset": handle_profile_dataset,
    "propose_cleaning": handle_propose_cleaning,
    "apply_cleaning": handle_apply_cleaning,
    "replay_recipe": handle_replay_recipe,
    "query_dataset": handle_query_dataset,
    "reconcile_sources": handle_reconcile_sources,
    "generate_report": handle_generate_report,
    "export_dataset": handle_export_dataset,
}


__all__ = ["HANDLERS", "JobContext", "JobError"]
