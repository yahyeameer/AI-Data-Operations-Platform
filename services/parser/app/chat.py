"""
Chat endpoint for the AnalyzeIt dashboard (PRD v3 §4, §11).

Implements the `POST /api/v1/chat` contract the dashboard's hermes bridge
expects, backed by OpenRouter function-calling over the parser tools.

The LLM never touches client data directly: every answer about numbers comes
from a tool call (query_dataset, profile_dataset, ...) executed inside this
process against workspace-scoped datasets. The model proposes SQL; the
service runs it; the model narrates the result.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

import httpx
from fastapi import Header, HTTPException

try:
    from .main import (
        APP_SECRET, TOOL_SECRET, app, DATASETS, ensure_parsed,
        CLEANED_BUCKET, ensure_bucket, upload_to_supabase, sign_supabase_url,
        df_to_xlsx_bytes, supabase_configured,
    )
except ImportError:
    from main import (
        APP_SECRET, TOOL_SECRET, app, DATASETS, ensure_parsed,
        CLEANED_BUCKET, ensure_bucket, upload_to_supabase, sign_supabase_url,
        df_to_xlsx_bytes, supabase_configured,
    )

from fastapi import Request as FastAPIRequest

# Importing chat registers its routes on the shared FastAPI app.
__all__ = ["chat"]

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
MODEL_PRIMARY = os.environ.get("MODEL_PRIMARY", "z-ai/glm-5.3-flash")
MODEL_FALLBACK = os.environ.get("MODEL_SECONDARY", "")
MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """You are the AnalyzeIt data-operations copilot for a UK accounting practice.
You are an autonomous but careful data analyst working alongside an accountant. You
interpret messy client workbooks and bank statements, clean them, and answer questions
about totals, anomalies, duplicates, vendor patterns, and period comparisons.

## How you work (follow this order)
1. UNDERSTAND FIRST. Before running any analysis that produces figures, decide whether
   the request is unambiguous. If key details are missing or ambiguous, ask ONE concise
   clarifying question and stop — do not guess. Typical ambiguities: which client/file,
   which month or date range, which column is the amount, net vs gross/VAT, how to treat
   duplicates or blanks. If the request is already clear, proceed without asking.
2. INSPECT. Use list_datasets to see what is loaded, and profile_dataset to learn column
   names, types, null counts, and duplicate counts before computing anything.
3. CLEAN WHEN IT MATTERS. If the data has issues that would distort the answer (duplicate
   rows, junk/subtotal rows, blanks in a needed column), propose a cleaning step and use
   clean_dataset / detect_duplicates / validate_dataset. Briefly tell the user what you
   changed and why (e.g. "removed 3 duplicate rows"). Prefer a dry run first for anything
   material; never silently discard data.
   You CAN also ADD a derived column: to categorise/tag/label rows (e.g. a "Category" column
   on bank-statement descriptions), use clean_dataset with a {type:'categorize'} step —
   supply source_column, the target column name, and ordered keyword rules. This writes the
   new column into the cleaned copy so the categorised file downloads with it. Do NOT tell
   the user you cannot add a column; you can.
   IMPORTANT for speed: categorize is ADDITIVE (it removes no rows), so it does NOT need a
   dry-run — when the user asks to categorise and get the file, DO IT IN ONE PASS: quickly
   profile the source column if needed, then call clean_dataset with the categorize step and
   dry_run=FALSE, then call export_dataset, then reply. Design sensible keyword rules
   yourself from the descriptions; do NOT deliberate at length or ask the user to approve
   the rules unless they are genuinely ambiguous. Keep any explanation to 2-3 short lines
   plus the category breakdown — never narrate your rule design in the reply.
4. ANSWER FROM DATA. Use query_dataset with SQL (DuckDB/SQLite dialect) against table `ds`
   for every figure. Money columns are Net Sales and VAT unless profile_dataset shows
   otherwise.
5. DELIVER THE FILE WHEN ASKED. After you persist a clean, or whenever the user asks for
   the cleaned/edited/exported file, call export_dataset (format xlsx or csv) and give the
   user the returned download_url. The link is valid ~1 hour. Never invent a link.

## Hard rules
- Every number you state MUST come from a tool result. Never invent, estimate, or
  round-guess a figure. If a tool cannot produce it, say what you need.
- If no dataset is loaded, say what you need (an upload) — do not fabricate.
- Distinguish observed data from your interpretation. Flag anomalies; do not assert causes.
- You clean and analyze, but the accountant approves material changes and signs off. You
  are a copilot, not the final authority on the numbers.
- Be concise and businesslike. Lead with the answer or the one clarifying question, then
  at most one line of method."""


# ---------------------------------------------------------------------------
# tool implementations shared with /api/v1/tools
# ---------------------------------------------------------------------------

def _active_df(ds: dict[str, Any]):
    """Return the cleaned dataframe if the model has run clean_dataset, else the
    parsed original. So 'clean then analyze' operates on the cleaned data."""
    return ds.get("cleaned", ds["df"])


def _tool_query(ds_id: str, sql: str) -> dict[str, Any]:
    import duckdb
    ds = ensure_parsed(ds_id)
    con = duckdb.connect()
    try:
        con.register("ds", _active_df(ds).to_pandas())
        res = con.execute(sql)
        rows = [list(r) for r in res.fetchall()]
        cols = [d[0] for d in res.description]
    finally:
        con.close()
    return {"columns": cols, "rows": rows[:200], "truncated": len(rows) > 200}


def _tool_profile(ds_id: str) -> dict[str, Any]:
    ds = ensure_parsed(ds_id)
    df = _active_df(ds)
    cols: dict[str, Any] = {}
    for col in df.columns:
        s = df[col]
        entry: dict[str, Any] = {"dtype": str(s.dtype), "nulls": int(s.null_count())}
        if s.dtype.is_numeric():
            entry |= {"sum": round(float(s.sum()), 2), "min": float(s.min()), "max": float(s.max())}
        cols[col] = entry
    return {"rows": df.height, "duplicate_rows": int(df.is_duplicated().sum()), "columns": cols}


def _tool_list_datasets(workspace_id: str | None) -> list[dict[str, Any]]:
    # 1. Sync from Supabase raw_uploads if available
    if supabase_configured() and workspace_id:
        import httpx
        from .main import SUPABASE_URL, _supabase_headers
        try:
            url = f"{SUPABASE_URL}/rest/v1/raw_uploads?workspace_id=eq.{workspace_id}&status=eq.stored&order=created_at.desc&limit=10"
            resp = httpx.get(url, headers=_supabase_headers(), timeout=15)
            if resp.status_code == 200:
                for row in resp.json():
                    key = row.get("dataset_id") or row.get("id")
                    if key and key not in DATASETS:
                        DATASETS[key] = {
                            "bytes": None,
                            "filename": row.get("original_filename"),
                            "storage_path": row.get("storage_path"),
                            "workspace_id": row.get("workspace_id"),
                            "dataset_id": row.get("dataset_id"),
                        }
                    if key in DATASETS and "df" not in DATASETS[key] and DATASETS[key].get("storage_path"):
                        try:
                            ensure_parsed(key)
                        except Exception:
                            pass
        except Exception:
            pass

    out = []
    seen = set()
    for ds_id, ds in DATASETS.items():
        if ds_id in seen:
            continue
        if workspace_id and ds.get("workspace_id") not in (None, workspace_id):
            continue
        seen.add(ds_id)
        if "df" not in ds and ds.get("storage_path"):
            try:
                ensure_parsed(ds_id)
            except Exception:
                pass
        out.append({
            "dataset_id": ds_id,
            "filename": ds.get("filename"),
            "parsed": "df" in ds,
            "rows": ds["df"].height if "df" in ds else None,
            "columns": ds["df"].columns if "df" in ds else [],
        })
    return out


def _tool_detect_duplicates(ds_id: str) -> dict[str, Any]:
    """Report duplicate rows without modifying anything."""
    ds = ensure_parsed(ds_id)
    df = ds["df"]
    dup_mask = df.is_duplicated()
    dup_count = int(dup_mask.sum())
    sample = df.filter(dup_mask).head(10).to_dicts() if dup_count else []
    return {
        "duplicate_rows": dup_count,
        "total_rows": df.height,
        "sample": sample,
    }


def _tool_validate(ds_id: str, required_columns: list[str] | None) -> dict[str, Any]:
    """Data-quality report: missing required columns, per-column null counts,
    duplicate rows, empty rows. Read-only."""
    ds = ensure_parsed(ds_id)
    df = ds["df"]
    required = required_columns or []
    missing = [c for c in required if c not in df.columns]
    nulls = {c: int(df[c].null_count()) for c in df.columns}
    issues: list[str] = []
    if missing:
        issues.append(f"missing required columns: {', '.join(missing)}")
    dup = int(df.is_duplicated().sum())
    if dup:
        issues.append(f"{dup} duplicate row(s)")
    high_null = [c for c, n in nulls.items() if df.height and n > df.height * 0.5]
    if high_null:
        issues.append(f"columns >50% empty: {', '.join(high_null)}")
    return {
        "rows": df.height,
        "columns": df.columns,
        "missing_required_columns": missing,
        "null_counts": nulls,
        "duplicate_rows": dup,
        "issues": issues,
        "ok": not issues,
    }


def _tool_clean(ds_id: str, steps: list[dict[str, Any]], dry_run: bool) -> dict[str, Any]:
    """Apply cleaning steps and write the result to a cleaned copy of the dataset.

    Supported steps (each a dict with a "type"):
      - {"type": "dedupe"}                              remove exact duplicate rows
      - {"type": "drop_nulls", "column": "<col>"}       drop rows null in <col>
      - {"type": "drop_empty_rows"}                      drop fully-empty rows

    The original dataframe (ds["df"]) is never mutated -- immutable source
    (PRD section 8). A dry run reports the effect without persisting; a real run
    stores the result in ds["cleaned"]. Returns a change summary.
    """
    ds = ensure_parsed(ds_id)
    df = ds["df"]
    before = df.height
    working = df
    applied: list[dict[str, Any]] = []

    for step in steps:
        st = (step or {}).get("type")
        rows_before = working.height
        if st == "dedupe":
            working = working.unique(keep="first")
        elif st == "drop_nulls" and step.get("column"):
            col = step["column"]
            if col in working.columns:
                working = working.drop_nulls(subset=[col])
        elif st == "drop_empty_rows":
            # a row is "empty" when every value is null
            working = working.filter(
                ~pl_all_horizontal_null(working)
            )
        elif st == "categorize":
            working, note = _apply_categorize(working, step)
            applied.append({"type": st, **note})
            continue
        else:
            applied.append({"type": st, "skipped": "unknown or missing params"})
            continue
        applied.append({"type": st, "removed": rows_before - working.height})

    after = working.height
    if not dry_run:
        ds["cleaned"] = working

    return {
        "dry_run": dry_run,
        "rows_before": before,
        "rows_after": after,
        "rows_removed": before - after,
        "applied_steps": applied,
        "persisted": (not dry_run),
        "note": "Cleaned copy stored; original preserved." if not dry_run
        else "Preview only; nothing persisted.",
    }


def _tool_export(ds_id: str, fmt: str, which: str) -> dict[str, Any]:
    """Serialize a dataset to CSV/XLSX, upload it to Supabase Storage, and
    return a time-limited signed download URL the dashboard can hand to the user.

    which='cleaned' (default) exports ds['cleaned'] if a clean was persisted,
    else falls back to the parsed original. which='original' always exports the
    parsed source. The immutable raw upload is never touched.
    """
    ds = ensure_parsed(ds_id)
    if which == "original":
        df = ds["df"]
        label = "original"
    else:
        df = ds.get("cleaned", ds["df"])
        label = "cleaned" if "cleaned" in ds else "original"

    fmt = (fmt or "xlsx").lower()
    if fmt not in ("xlsx", "csv"):
        return {"error": f"unsupported format {fmt}; use 'xlsx' or 'csv'"}

    if not supabase_configured():
        return {"error": "file export needs Supabase Storage configured on the parser "
                         "(SUPABASE_URL + service key); not available in this environment"}

    if fmt == "csv":
        data = df.write_csv().encode()
        content_type = "text/csv"
    else:
        data = df_to_xlsx_bytes(df)
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    safe_id = "".join(c if (c.isalnum() or c in "-_./") else "_" for c in ds_id)
    path = f"{safe_id}/{label}.{fmt}"
    ensure_bucket(CLEANED_BUCKET)
    upload_to_supabase(CLEANED_BUCKET, path, data, content_type)
    signed = sign_supabase_url(CLEANED_BUCKET, path, expires_in=3600)
    return {
        "exported": label,
        "format": fmt,
        "rows": df.height,
        "bucket": CLEANED_BUCKET,
        "path": path,
        "download_url": signed,
        "expires_in_seconds": 3600,
        "note": f"{label} dataset exported as {fmt}. Give the user the download_url "
                "(valid ~1 hour). Original upload preserved.",
    }


def _apply_categorize(df, step: dict[str, Any]):
    """Add a derived label column from ordered keyword rules.

    This is what lets the copilot deliver a *categorised* workbook: it computes
    a new column (e.g. "Category") on the cleaned copy so export_dataset writes
    it into the downloaded file. The original upload is never touched.

    step shape:
      {
        "type": "categorize",
        "source_column": "Description",       # column to read (required)
        "target_column": "Category",           # new column name (default "Category")
        "rules": [                              # ordered; first match wins
          {"category": "Payroll", "keywords": ["salary", "wages", "payroll"]},
          {"category": "Utilities", "keywords": ["edf", "british gas", "water"]}
        ],
        "default": "Uncategorised"             # label for unmatched rows
      }

    Matching is case-insensitive substring on the source column's string form.
    """
    import re
    import polars as pl

    source = step.get("source_column") or step.get("column")
    target = step.get("target_column") or "Category"
    rules = step.get("rules") or []
    default = step.get("default", "Uncategorised")

    if not source or source not in df.columns:
        return df, {"skipped": f"source_column '{source}' not found"}
    if not isinstance(rules, list) or not rules:
        return df, {"skipped": "no rules provided"}

    src = pl.col(source).cast(pl.Utf8).fill_null("")
    expr = pl.lit(default)
    valid_rules = 0
    # Build the when/then chain in reverse so that earlier (higher-priority)
    # rules end up outermost and win over later ones.
    for rule in reversed(rules):
        cat = (rule or {}).get("category")
        keywords = (rule or {}).get("keywords") or []
        terms = [str(k) for k in keywords if str(k).strip()]
        if not cat or not terms:
            continue
        pattern = "(?i)" + "|".join(re.escape(t) for t in terms)
        expr = pl.when(src.str.contains(pattern)).then(pl.lit(cat)).otherwise(expr)
        valid_rules += 1

    if valid_rules == 0:
        return df, {"skipped": "no valid rules (each needs category + keywords)"}

    out = df.with_columns(expr.alias(target))
    counts = {
        row[target]: row["n"]
        for row in out.group_by(target).agg(pl.len().alias("n")).to_dicts()
    }
    return out, {
        "added_column": target,
        "from_column": source,
        "rules_applied": valid_rules,
        "category_counts": counts,
    }


def pl_all_horizontal_null(df):
    """Boolean mask: True where every column in the row is null."""
    import polars as _pl
    if not df.columns:
        return _pl.Series([False] * df.height)
    expr = _pl.col(df.columns[0]).is_null()
    for c in df.columns[1:]:
        expr = expr & _pl.col(c).is_null()
    return df.select(expr.alias("_allnull"))["_allnull"]


TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "query_dataset",
            "description": "Run SQL (DuckDB/SQLite dialect) against table `ds` for a dataset. Prefer aggregate queries; results capped at 200 rows.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dataset_id": {"type": "string"},
                    "sql": {"type": "string"},
                },
                "required": ["dataset_id", "sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "profile_dataset",
            "description": "Column-level statistics (types, nulls, sums, duplicate row count) for a dataset.",
            "parameters": {
                "type": "object",
                "properties": {"dataset_id": {"type": "string"}},
                "required": ["dataset_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_datasets",
            "description": "List datasets available in the current workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_duplicates",
            "description": "Report how many exact-duplicate rows a dataset has (with a small sample). Read-only; changes nothing.",
            "parameters": {
                "type": "object",
                "properties": {"dataset_id": {"type": "string"}},
                "required": ["dataset_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_dataset",
            "description": "Data-quality report: missing required columns, per-column null counts, duplicate rows, mostly-empty columns. Read-only. Use before analysis to decide what needs cleaning.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dataset_id": {"type": "string"},
                    "required_columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional columns that must be present.",
                    },
                },
                "required": ["dataset_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clean_dataset",
            "description": (
                "Apply cleaning/derivation steps to a dataset, writing to a cleaned copy "
                "(the original is preserved). Steps: {type:'dedupe'}, "
                "{type:'drop_nulls', column:'<col>'}, {type:'drop_empty_rows'}, and "
                "{type:'categorize', source_column:'<col>', target_column:'Category', "
                "rules:[{category:'Payroll', keywords:['salary','wages']}, ...], "
                "default:'Uncategorised'} which ADDS a new label column derived from "
                "keyword matches (case-insensitive substring, first matching rule wins). "
                "Use 'categorize' when the user asks to categorise/tag/label rows and get "
                "the categorised file back — it writes the new column into the cleaned copy "
                "so export_dataset includes it. "
                "For row-removing steps (dedupe, drop_nulls, drop_empty_rows) ALWAYS call "
                "once with dry_run=true first to preview the impact, tell the user, then call "
                "with dry_run=false to persist. The categorize step is additive (removes no "
                "rows) — skip the dry-run and call it directly with dry_run=false."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dataset_id": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["dedupe", "drop_nulls", "drop_empty_rows", "categorize"],
                                },
                                "column": {"type": "string"},
                                "source_column": {
                                    "type": "string",
                                    "description": "categorize: column to read text from.",
                                },
                                "target_column": {
                                    "type": "string",
                                    "description": "categorize: name of the new column (default 'Category').",
                                },
                                "default": {
                                    "type": "string",
                                    "description": "categorize: label for rows matching no rule (default 'Uncategorised').",
                                },
                                "rules": {
                                    "type": "array",
                                    "description": "categorize: ordered rules; first match wins.",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "category": {"type": "string"},
                                            "keywords": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                        },
                                        "required": ["category", "keywords"],
                                    },
                                },
                            },
                            "required": ["type"],
                        },
                    },
                    "dry_run": {"type": "boolean"},
                },
                "required": ["dataset_id", "steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_dataset",
            "description": (
                "Export a dataset to a downloadable file and return a signed "
                "download_url (valid ~1 hour) to give the user. Use this after a "
                "clean is persisted, or whenever the user asks for the cleaned/edited "
                "file. which='cleaned' returns the cleaned copy (falls back to the "
                "original if nothing was cleaned); which='original' returns the "
                "parsed source. Never fabricate a link — only report the download_url "
                "this tool returns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dataset_id": {"type": "string"},
                    "format": {"type": "string", "enum": ["xlsx", "csv"]},
                    "which": {"type": "string", "enum": ["cleaned", "original"]},
                },
                "required": ["dataset_id"],
            },
        },
    },
]


def _execute_tool(name: str, args: dict[str, Any], scope_workspace_id: str | None) -> dict[str, Any]:
    if name == "query_dataset":
        return _tool_query(args["dataset_id"], args["sql"])
    if name == "profile_dataset":
        return _tool_profile(args["dataset_id"])
    if name == "list_datasets":
        return {"datasets": _tool_list_datasets(scope_workspace_id)}
    if name == "detect_duplicates":
        return _tool_detect_duplicates(args["dataset_id"])
    if name == "validate_dataset":
        return _tool_validate(args["dataset_id"], args.get("required_columns"))
    if name == "clean_dataset":
        return _tool_clean(
            args["dataset_id"],
            args.get("steps") or [],
            bool(args.get("dry_run", True)),
        )
    if name == "export_dataset":
        return _tool_export(
            args["dataset_id"],
            args.get("format", "xlsx"),
            args.get("which", "cleaned"),
        )
    return {"error": f"unknown tool {name}"}


# ---------------------------------------------------------------------------
# endpoint
# ---------------------------------------------------------------------------

@app.post("/api/v1/chat")
async def chat(request: FastAPIRequest,
               authorization: str | None = Header(default=None)) -> dict[str, Any]:
    # Prove the caller is our Next.js dashboard backend
    valid_secrets = {s for s in (TOOL_SECRET, os.environ.get("HERMES_API_SECRET", "")) if s}
    if valid_secrets:
        allowed_headers = {f"Bearer {s}" for s in valid_secrets}
        if authorization not in allowed_headers:
            raise HTTPException(401, "Unauthorized")


    body = await request.json()
    message: str = (body.get("message") or "").strip()
    history: list[dict[str, str]] = body.get("history") or []
    scope_token: str | None = body.get("scope_token")
    workspace_id: str | None = body.get("workspace_id")

    if not message:
        raise HTTPException(400, "message is empty")
    if not OPENROUTER_API_KEY:
        return {
            "status": "error",
            "result": {},
            "warnings": ["OPENROUTER_API_KEY is not configured on the agent service"],
            "execution_metadata": {"dry_run": False},
        }

    # Scope token is minted by the dashboard after requireWorkspaceAccess ran;
    # its presence marks an authenticated turn. We do not parse it here (the
    # dashboard owns verification); we record it in evidence.
    evidence_scope = "scope_token present" if scope_token else "no scope token"

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += [
        {"role": h["role"], "content": h["content"]}
        for h in history[-12:]
        if h.get("role") in ("user", "assistant")
    ]
    messages.append({"role": "user", "content": message})

    started = time.time()
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    tool_trace: list[dict[str, Any]] = []
    downloads: list[dict[str, Any]] = []
    reply_text = ""

    async with httpx.AsyncClient(timeout=60) as client:
        url = f"{OPENROUTER_BASE_URL}/chat/completions"
        used_model = MODEL_PRIMARY
        models_to_try = [m for m in (MODEL_PRIMARY, MODEL_FALLBACK) if m]

        for _round in range(MAX_TOOL_ROUNDS + 1):
            resp = None
            for attempt_model in models_to_try:
                resp = await client.post(url, headers=headers, json={
                    "model": attempt_model,
                    "messages": messages,
                    "tools": TOOLS_SPEC,
                    "max_tokens": 1200,
                    "temperature": 0.2,
                })
                if resp.status_code == 429 and attempt_model != models_to_try[-1]:
                    used_model = models_to_try[models_to_try.index(attempt_model) + 1]
                    continue
                used_model = attempt_model
                break
            assert resp is not None
            if resp.status_code == 401:
                raise HTTPException(502, "OpenRouter rejected the API key")
            if resp.status_code != 200:
                raise HTTPException(502, f"OpenRouter error {resp.status_code}: {resp.text[:200]}")

            choice = resp.json()["choices"][0]["message"]
            calls = choice.get("tool_calls") or []

            if not calls:
                reply_text = (choice.get("content") or "").strip() or (choice.get("reasoning") or "").strip()
                break

            messages.append(choice)
            for call in calls:
                fn = call["function"]["name"]
                args: dict[str, Any] = {}
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                    result = _execute_tool(fn, args, workspace_id)
                except Exception as exc:  # tool errors go back to the model, not the user
                    result = {"error": str(exc)[:300]}
                tool_trace.append({"tool": fn, "args": args})
                if fn == "export_dataset" and isinstance(result, dict) and result.get("download_url"):
                    downloads.append({
                        "filename": f"{result.get('exported', 'dataset')}.{result.get('format', 'xlsx')}",
                        "url": result["download_url"],
                        "format": result.get("format", "xlsx"),
                        "rows": result.get("rows"),
                        "expires_in_seconds": result.get("expires_in_seconds"),
                    })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, default=str)[:6000],
                })
        else:
            try:
                final_resp = await client.post(url, headers=headers, json={
                    "model": used_model or MODEL_PRIMARY,
                    "messages": messages + [{
                        "role": "user",
                        "content": "Please present your full analysis and breakdown now based on the data and query results gathered above."
                    }],
                    "max_tokens": 1500,
                    "temperature": 0.2,
                })
                if final_resp.status_code == 200:
                    reply_text = final_resp.json()["choices"][0]["message"].get("content") or ""
            except Exception:
                pass
            if not reply_text:
                reply_text = "I have gathered the data and queried the numbers, but could not format the final response in time."

    return {
        "status": "ok" if reply_text else "error",
        "result": {"reply": reply_text, "downloads": downloads},
        "evidence": {
            "tools_used": tool_trace,
            "scope": evidence_scope,
        },
        "warnings": [],
        "execution_metadata": {
            "duration_ms": int((time.time() - started) * 1000),
            "model": used_model,
            "dry_run": False,
        },
    }


@app.get("/health")
async def health_extended():
    base = {
        "status": "healthy",
        "agent": "AnalyzeIt Parser Agent",
        "datasets": len(DATASETS),
        "time": datetime.utcnow().isoformat(),
    }
    base |= {
        "chat_enabled": bool(OPENROUTER_API_KEY),
        "queue_depth": 0,
        "active_workers": 1,
    }
    return base
