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


# ---------------------------------------------------------------------------
# Reliable "categorise this and give me the file" short-circuit.
#
# The general agent (a weak free model driving an 8-round tool loop) is
# unreliable at completing the categorise -> export chain: it tends to gather
# the data and then narrate a rule plan in prose instead of calling the tools,
# so no file comes back and the turn can blow past the serverless time cap.
#
# When the user's turn clearly means "categorise my statement and hand me the
# workbook", we bypass the tool loop entirely and run the job in code:
#   1. resolve the target dataset + its description column (deterministic),
#   2. generate keyword rules in ONE bounded LLM call, with a built-in UK
#      keyword library as a guaranteed fallback (so it works even if the model
#      is down or returns junk),
#   3. execute the already-verified _apply_categorize + _tool_export engine.
# This always produces a downloadable, categorised file, fast, and spends at
# most one model call instead of a multi-round loop.
# ---------------------------------------------------------------------------

# Column-name hints for the free-text description column on a statement.
_DESC_COL_HINTS = [
    "transaction", "description", "narrative", "details", "memo",
    "reference", "payee", "particulars", "vendor", "merchant", "name",
]

# Deterministic fallback: sensible UK bank-statement categories. Used when the
# model call is unavailable or unusable, so a file is ALWAYS produced.
_DEFAULT_UK_RULES = [
    {"category": "Payroll", "keywords": ["salary", "wages", "payroll"]},
    {"category": "Government/HMRC", "keywords": ["hmrc", "dwp", "child benefit",
        "universal credit", "tax credit", "gov.uk", "council tax", "dvla"]},
    {"category": "Housing/Rent", "keywords": ["rent", "housing", "mortgage",
        "landlord", "sovereign", "council"]},
    {"category": "Utilities", "keywords": ["edf", "british gas", "octopus",
        "thames water", "water", "vodafone", "virgin", "o2", "sse", "eon",
        "npower", "gas", "electric", "bt "]},
    {"category": "Groceries", "keywords": ["tesco", "asda", "sainsbury", "aldi",
        "lidl", "morrison", "waitrose", "co-op", "coop", "iceland", "m&s"]},
    {"category": "Transport/Fuel", "keywords": ["shell", "bp ", "esso", "tfl",
        "uber", "trainline", "petrol", "fuel"]},
    {"category": "Insurance", "keywords": ["insurance", "aviva", "axa",
        "direct line", "admiral"]},
    {"category": "Subscriptions", "keywords": ["netflix", "spotify", "amazon",
        "prime", "apple", "google", "microsoft"]},
    {"category": "Transfers", "keywords": ["transfer", "faster payment",
        "bank giro", "standing order", "direct debit"]},
    {"category": "Cash/ATM", "keywords": ["atm", "cash", "withdrawal"]},
    {"category": "Fees", "keywords": ["fee", "charge", "interest", "overdraft"]},
]


def _wants_categorized_file(message: str) -> bool:
    """True when the turn means 'apply categories to my data / give me the
    categorised file', rather than merely asking a question about categories."""
    m = (message or "").lower()
    if not any(t in m for t in ("categor", "classif")):
        return False
    wants_apply = any(t in m for t in (
        "file", "download", "xlsx", "csv", "spreadsheet", "column", "add ",
        "tag", "label", "export", "give me", "provide", "sort ", "group ",
    ))
    starts = m.lstrip().startswith(("categor", "classif"))
    return wants_apply or starts


def _pick_categorize_dataset(parsed: list[dict[str, Any]], message: str) -> dict[str, Any] | None:
    """Choose which loaded dataset to categorise. Prefer one the user named by
    filename, else one that has a description-like column, tie-broken by size."""
    msg = (message or "").lower()
    for d in parsed:
        stem = str(d.get("filename") or "").rsplit(".", 1)[0].strip().lower()
        if stem and len(stem) >= 4 and stem in msg:
            return d

    def has_desc(d: dict[str, Any]) -> bool:
        cols = " ".join(str(c).lower() for c in (d.get("columns") or []))
        return any(h in cols for h in _DESC_COL_HINTS)

    scored = sorted(parsed, key=lambda d: (has_desc(d), d.get("rows") or 0), reverse=True)
    return scored[0] if scored else None


def _pick_source_column(df, message: str) -> str | None:
    """Pick the free-text column to read. A name matching a known hint wins;
    otherwise the column with the widest average text is the description."""
    import polars as pl
    if not df.columns:
        return None
    for hint in _DESC_COL_HINTS:
        for c in df.columns:
            if hint in str(c).lower():
                return c
    best, best_len = None, -1.0
    for c in df.columns:
        try:
            avg = df[c].cast(pl.Utf8).str.len_chars().mean() or 0.0
        except Exception:
            avg = 0.0
        if avg > best_len:
            best, best_len = c, float(avg)
    return best


def _parse_rules_json(content: str):
    """Defensively parse the model's rule JSON (tolerates code fences / prose).
    Returns (rules, default) or None."""
    txt = (content or "").strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        nl = txt.find("\n")
        if nl != -1 and txt[:nl].strip().lower() in ("json", ""):
            txt = txt[nl + 1:]
    i, j = txt.find("{"), txt.rfind("}")
    if i == -1 or j == -1 or j <= i:
        return None
    try:
        obj = json.loads(txt[i:j + 1])
    except Exception:
        return None
    rules = obj.get("rules")
    if not isinstance(rules, list) or not rules:
        return None
    clean: list[dict[str, Any]] = []
    for r in rules:
        cat = (r or {}).get("category")
        terms = [str(k).lower() for k in ((r or {}).get("keywords") or []) if str(k).strip()]
        if cat and terms:
            clean.append({"category": str(cat), "keywords": terms})
    if not clean:
        return None
    return clean, str(obj.get("default") or "?")


async def _generate_rules(descriptions: list[str], message: str):
    """One bounded model call to produce keyword rules fitted to the actual
    descriptions. Falls back to the built-in UK library on any problem, so a
    result is guaranteed. Returns (rules, default, source)."""
    fallback = (_DEFAULT_UK_RULES, "?", "default-library")
    if not OPENROUTER_API_KEY or not descriptions:
        return fallback
    sample = "\n".join("- " + d[:80] for d in descriptions[:60])
    sys = ("You build keyword rules to categorise UK bank-statement transactions. "
           "Output ONLY a JSON object, no prose and no markdown fences.")
    usr = (
        f"Distinct transaction descriptions:\n{sample}\n\n"
        f"User request: {message}\n\n"
        'Return JSON exactly like {"rules":[{"category":"Name","keywords":["lower","substrings"]}],'
        '"default":"?"}. Keywords must be lowercase substrings that actually appear in the '
        "descriptions above. Cover the data with 5-9 sensible categories (e.g. Payroll, "
        "Utilities, Groceries, Housing/Rent, Government/HMRC, Transfers, Fees, Cash/ATM). "
        "If the user named specific categories, use those. Prefer precise, distinctive keywords."
    )
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": MODEL_PRIMARY,
                    "messages": [{"role": "system", "content": sys},
                                 {"role": "user", "content": usr}],
                    "max_tokens": 900,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
            )
        if resp.status_code != 200:
            return fallback
        content = resp.json()["choices"][0]["message"].get("content") or ""
        parsed = _parse_rules_json(content)
        if parsed:
            return parsed[0], parsed[1], "ai-generated"
        return fallback
    except Exception:
        return fallback


# Adjustment-language a user uses to refine an existing categorisation, e.g.
# "put HMRC NDDS under Government too", "the ATM ones should be Cash",
# "move Amazon into Shopping", "reclassify the ? rows as Other".
_REFINE_HINTS = [
    "recategor", "re-categor", "reclassif", "re-classif", "should be",
    "should go", "move ", "put ", " under ", "instead", "rename",
    "relabel", "re-label", "merge ", "combine", "change ", "add a categor",
    "add category", "also label", "also tag", "belongs", "classify the",
    "the ? ", "'?'", "\"?\"", "unmatched", "uncategor", "wrong categor",
    "fix the categor", "not ", "these are", "that's ", "those are",
]


def _looks_like_refine(message: str) -> bool:
    """Heuristic: does this turn look like a follow-up adjustment to a prior
    categorisation (rather than a fresh request or an unrelated question)?"""
    m = (message or "").lower().strip()
    if not m:
        return False
    return any(h in m for h in _REFINE_HINTS)


def _find_recent_categorized(workspace_id: str | None) -> tuple[str, dict[str, Any]] | None:
    """Most-recently categorised dataset in this workspace that still holds its
    rule state, so a refine turn knows what it is adjusting."""
    best: tuple[float, str, dict[str, Any]] | None = None
    for ds_id, ds in DATASETS.items():
        state = ds.get("cat_state")
        if not state:
            continue
        if workspace_id and ds.get("workspace_id") not in (None, workspace_id):
            continue
        when = float(state.get("at") or 0.0)
        if best is None or when > best[0]:
            best = (when, ds_id, ds)
    if best is None:
        return None
    return best[1], best[2]


async def _refine_rules(existing_rules: list[dict[str, Any]], default: str,
                        descriptions: list[str], message: str):
    """One bounded model call to EDIT existing keyword rules per the user's
    adjustment instruction. Falls back to the unchanged rules on any problem
    (safe: never loses the prior categorisation). Returns (rules, default, source)."""
    fallback = (existing_rules, default, "unchanged")
    if not OPENROUTER_API_KEY or not existing_rules:
        return fallback
    sample = "\n".join("- " + d[:80] for d in descriptions[:60])
    current = json.dumps({"rules": existing_rules, "default": default})
    sys = ("You edit an existing set of keyword rules that categorise UK "
           "bank-statement transactions, applying the user's adjustment. "
           "Output ONLY a JSON object, no prose and no markdown fences.")
    usr = (
        f"Current rules JSON:\n{current}\n\n"
        f"Distinct transaction descriptions in the data:\n{sample}\n\n"
        f"User adjustment: {message}\n\n"
        "Apply the adjustment by editing the rules: add/rename/merge categories or "
        "move keywords as asked, and KEEP everything the user did not mention. "
        'Return the FULL updated JSON exactly like {"rules":[{"category":"Name",'
        '"keywords":["lower","substrings"]}],"default":"?"}. Keywords must be lowercase '
        "substrings that actually appear in the descriptions above."
    )
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": MODEL_PRIMARY,
                    "messages": [{"role": "system", "content": sys},
                                 {"role": "user", "content": usr}],
                    "max_tokens": 1000,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
            )
        if resp.status_code != 200:
            return fallback
        content = resp.json()["choices"][0]["message"].get("content") or ""
        parsed = _parse_rules_json(content)
        if parsed:
            return parsed[0], parsed[1], "ai-refined"
        return fallback
    except Exception:
        return fallback


async def _categorize_shortcircuit(message: str, workspace_id: str | None,
                                   refine: bool = False) -> dict[str, Any] | None:
    """Run the whole categorise->export job in code. Returns a chat envelope,
    or None to fall back to the general agent (e.g. nothing to categorise).

    refine=True adjusts a PRIOR categorisation: it targets the dataset that
    still holds its rule state and edits those rules per the user's instruction,
    instead of generating a fresh rule set from scratch.
    """
    started = time.time()

    prior_state: dict[str, Any] | None = None
    if refine:
        found = _find_recent_categorized(workspace_id)
        if not found:
            return None  # nothing to refine -> let the agent handle it
        ds_id, ds = found
        try:
            ds = ensure_parsed(ds_id)
        except Exception:
            return None
        prior_state = ds.get("cat_state") or {}
        target = {"dataset_id": ds_id, "filename": ds.get("filename")}
    else:
        try:
            datasets = _tool_list_datasets(workspace_id)
        except Exception:
            return None
        parsed = [d for d in datasets if d.get("parsed") and d.get("columns")]
        if not parsed:
            return None
        target = _pick_categorize_dataset(parsed, message)
        if not target:
            return None
        ds_id = target["dataset_id"]
        try:
            ds = ensure_parsed(ds_id)
        except Exception:
            return None

    df = ds["df"]
    if refine and prior_state:
        source_col = prior_state.get("source_col") or _pick_source_column(df, message)
    else:
        source_col = _pick_source_column(df, message)
    if not source_col or source_col not in df.columns:
        return None

    descs = [str(x) for x in df[source_col].drop_nulls().unique().to_list() if str(x).strip()]

    if refine and prior_state:
        rules, default, rule_source = await _refine_rules(
            prior_state.get("rules") or [], prior_state.get("default", "?"),
            descs[:60], message)
    else:
        rules, default, rule_source = await _generate_rules(descs[:60], message)

    target_col = (prior_state or {}).get("target") or "Category"
    step = {"type": "categorize", "source_column": source_col,
            "target_column": target_col, "rules": rules, "default": default}
    working, note = _apply_categorize(df, step)
    if "skipped" in note:
        return None
    ds["cleaned"] = working
    # Persist the rule state so a later turn can refine it.
    ds["cat_state"] = {"source_col": source_col, "target": target_col,
                       "rules": rules, "default": default, "at": time.time()}

    export = _tool_export(ds_id, "xlsx", "cleaned")
    downloads: list[dict[str, Any]] = []
    if isinstance(export, dict) and export.get("download_url"):
        downloads.append({
            "filename": f"{export.get('exported', 'cleaned')}.{export.get('format', 'xlsx')}",
            "url": export["download_url"],
            "format": export.get("format", "xlsx"),
            "rows": export.get("rows"),
            "expires_in_seconds": export.get("expires_in_seconds"),
        })

    counts = note.get("category_counts", {}) or {}
    if not isinstance(counts, dict):
        counts = {}
    total = sum(counts.values()) or working.height or 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    breakdown = "\n".join(f"- {cat}: {n}" for cat, n in ordered)
    fname = target.get("filename") or ds_id
    warnings: list[str] = []

    if downloads:
        if refine:
            if rule_source == "unchanged":
                lead = (f"I couldn't automatically apply that adjustment to **{fname}**, so "
                        f"the categories are unchanged. Try naming the exact category and a word "
                        f"from the transactions, e.g. \u201clabel anything with \u2018HMRC\u2019 as Government\u201d.")
                warnings.append("refinement could not be applied automatically")
            else:
                lead = (f"Updated the categories on **{fname}** ({working.height} rows) per your "
                        f"change.")
        else:
            lead = (f"Added a **{target_col}** column to **{fname}** ({working.height} rows), "
                    f"read from the `{source_col}` column.")
        reply = (f"{lead}\n\n{breakdown}\n\n"
                 "Your categorised file is ready to download below. "
                 "Want to adjust any category? Just tell me (e.g. \u201cmove ATM to Cash\u201d).")
        status = "ok"
        unmatched = counts.get(default, 0)
        if unmatched and unmatched / total > 0.4:
            warnings.append(
                f"{unmatched} of {total} rows ({unmatched * 100 // total}%) didn't match any rule "
                f"and are labelled '{default}'. Tell me the categories/keywords you want for those "
                "and I'll refine the file."
            )
    else:
        err = export.get("error") if isinstance(export, dict) else "unknown error"
        reply = (f"I categorised {working.height} rows of **{fname}** by `{source_col}`, but the "
                 f"file export failed ({err}).")
        status = "error"
        warnings.append("file export failed")

    return {
        "status": status,
        "result": {"reply": reply, "downloads": downloads},
        "evidence": {
            "tools_used": [{"tool": "categorize_shortcircuit", "dataset_id": ds_id,
                            "source_column": source_col, "rule_source": rule_source,
                            "refine": refine}],
            "scope": "categorize-shortcircuit",
        },
        "warnings": warnings,
        "execution_metadata": {
            "duration_ms": int((time.time() - started) * 1000),
            "model": MODEL_PRIMARY if rule_source in ("ai-generated", "ai-refined") else "none",
            "dry_run": False,
            "mode": "categorize-refine" if refine else "categorize-shortcircuit",
        },
    }


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

class ChatError(RuntimeError):
    """
    A chat turn that cannot proceed, with a message fit for the user.

    Exists so this module raises something the *worker* can catch. The turn used
    to run inside a FastAPI request and raised HTTPException, which is only
    meaningful to a web framework -- and the turn no longer runs inside a
    request. `retryable` distinguishes "the model is rate-limited, try again"
    from "there is no API key", which will fail identically for ever.
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


async def run_chat_turn(
    message: str,
    history: list[dict[str, str]] | None = None,
    workspace_id: str | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """
    One conversational turn: message in, reply out, tools called in between.

    Extracted from the HTTP route it used to be, because a multi-round tool loop
    against a free-tier model has no bounded duration and therefore has no
    business inside a request. The worker calls this from a queued job, so a slow
    model delays an answer instead of tripping a platform timeout.

    `on_progress` lets the caller extend the job's lease and tell the user which
    round it is on. It is optional so this stays callable from a test with no
    queue behind it.
    """
    history = history or []
    message = (message or "").strip()

    def progress(stage: str, **detail: Any) -> None:
        if on_progress:
            on_progress({"stage": stage, **detail})

    if not message:
        raise ChatError("The message is empty.")
    if not OPENROUTER_API_KEY:
        raise ChatError(
            "No reasoning model is configured for this deployment, so questions in plain "
            "English cannot be answered. Set OPENROUTER_API_KEY on the engine host."
        )

    evidence_scope = f"workspace {workspace_id}" if workspace_id else "no workspace scope"

    # Fast, reliable path for "categorise this and give me the file". The weak
    # free model is unreliable at driving the categorise->export tool chain to
    # completion, so when the turn clearly means that, run the whole job in code
    # (deterministic engine + at most one bounded rule-generation call) and
    # return a downloadable file directly. Falls through to the general agent if
    # there is nothing to categorise.
    if _wants_categorized_file(message):
        try:
            shortcut = await _categorize_shortcircuit(message, workspace_id)
        except Exception:
            shortcut = None
        if shortcut is not None:
            shortcut["evidence"]["scope"] = evidence_scope
            return shortcut

    # Conversational refine loop: a short follow-up like "move ATM to Cash" or
    # "the HMRC ones should be Government" adjusts the PRIOR categorisation and
    # returns an updated file. Only engages when this workspace actually has a
    # categorised dataset with saved rule state; otherwise falls through.
    if _looks_like_refine(message) and _find_recent_categorized(workspace_id) is not None:
        try:
            refined = await _categorize_shortcircuit(message, workspace_id, refine=True)
        except Exception:
            refined = None
        if refined is not None:
            refined["evidence"]["scope"] = evidence_scope
            return refined

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

    # Whether the user's turn asks for a produced artifact / mutation (a file,
    # a categorised/cleaned/exported workbook). Weak free models often gather
    # the data and then narrate a *plan* in prose instead of calling the tool
    # that does the work. When that happens we nudge once or twice to force the
    # action rather than returning the plan as if it were the result.
    _m = message.lower()
    action_requested = any(
        k in _m for k in (
            "categori", "clean", "dedup", "download", "export",
            "give me the file", "the file", "xlsx", "csv", "spreadsheet",
        )
    )
    forced_nudges = 0
    MAX_FORCED_NUDGES = 2

    async with httpx.AsyncClient(timeout=60) as client:
        url = f"{OPENROUTER_BASE_URL}/chat/completions"
        used_model = MODEL_PRIMARY
        models_to_try = [m for m in (MODEL_PRIMARY, MODEL_FALLBACK) if m]

        for _round in range(MAX_TOOL_ROUNDS + 1):
            # Each round is another model call plus whatever tools it asks for,
            # so this is where the lease has to be extended -- a turn that
            # thinks for four rounds must not have its job stolen mid-thought.
            progress("thinking", round=_round + 1, of=MAX_TOOL_ROUNDS + 1)
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
                # A bad key fails the same way for ever; retrying spends the
                # user's wait for nothing.
                raise ChatError("The reasoning provider rejected this deployment's API key.")
            if resp.status_code == 429:
                raise ChatError(
                    "The reasoning model is rate-limited right now. This will be retried.",
                    retryable=True,
                )
            if resp.status_code >= 500:
                raise ChatError(
                    f"The reasoning provider is unavailable ({resp.status_code}). "
                    f"This will be retried.",
                    retryable=True,
                )
            if resp.status_code != 200:
                raise ChatError(f"The reasoning provider returned an error ({resp.status_code}).")

            choice = resp.json()["choices"][0]["message"]
            calls = choice.get("tool_calls") or []

            if not calls:
                candidate = (choice.get("content") or "").strip() or (choice.get("reasoning") or "").strip()
                # The model stopped without calling a tool. If the user asked us
                # to produce a file/mutation and we have not yet produced a
                # download, the model has almost certainly narrated a plan
                # instead of executing it. Force the action rather than handing
                # the plan back as the answer.
                if action_requested and not downloads and forced_nudges < MAX_FORCED_NUDGES:
                    forced_nudges += 1
                    messages.append(choice)
                    messages.append({
                        "role": "user",
                        "content": (
                            "You have NOT completed the task — you only described a plan. "
                            "Do not reply with prose. Execute now by calling the tools: to "
                            "categorise, call clean_dataset with a categorize step "
                            "(source_column, target_column, your keyword rules) and "
                            "dry_run=false; then call export_dataset (format 'xlsx'). Only "
                            "after the file is exported, give a 2-3 line reply with the "
                            "category breakdown. Act now."
                        ),
                    })
                    continue
                reply_text = candidate
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


@app.post("/api/v1/chat")
async def chat(request: FastAPIRequest,
               authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """
    The HTTP entry point, kept only while the dashboard is migrated onto the
    queue.

    Calling a chat turn over HTTP is what produced "the analysis is taking longer
    than this plan allows": the reply had to arrive inside the caller's request,
    and a multi-round tool loop cannot promise that. `enqueue_agent_job` with
    kind 'chat_turn' is the supported path; this wrapper exists so the old
    dashboard keeps working during the changeover and is deleted with the rest of
    the synchronous surface.
    """
    valid_secrets = {s for s in (TOOL_SECRET, os.environ.get("HERMES_API_SECRET", "")) if s}
    if valid_secrets:
        allowed_headers = {f"Bearer {s}" for s in valid_secrets}
        if authorization not in allowed_headers:
            raise HTTPException(401, "Unauthorized")

    body = await request.json()

    try:
        return await run_chat_turn(
            message=body.get("message") or "",
            history=body.get("history") or [],
            workspace_id=body.get("workspace_id"),
        )
    except ChatError as error:
        return {
            "status": "error",
            "result": {},
            "warnings": [str(error)],
            "execution_metadata": {"dry_run": False},
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
