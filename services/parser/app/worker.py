"""
The worker loop.

This is the process that replaces the request handler. Nothing calls it; it
calls out. It wakes up, asks the database whether there is work, does the work,
writes the answer back, and goes round again.

    heartbeat -> claim -> run -> report -> repeat

That inversion is the entire fix for the timeout. When the dashboard called the
parser over HTTP, three clocks ran in series on one request -- the hosting
platform's function cap, this service's cold start, and the analysis itself --
and the shortest of them decided whether the user saw a result. Here there is no
request. A worker that is asleep, restarting or being redeployed delays a job;
it cannot fail one. The analysis may take as long as it takes.

Deliberately boring: one job at a time, one process, no threads, no async. To do
more work, run more copies -- `claim_agent_job` uses `for update skip locked`,
so two workers cooperate with no coordination between them. Concurrency that
lives in the database is concurrency you can reason about from a SQL prompt.

Failure handling has one rule: every job must reach a terminal state or lose its
lease. A worker that dies holding a claim is fine, because the lease expires. A
worker that swallows an exception and loops without reporting is not, so the
try/except around the handler is total.

Run it:  python -m app.worker
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from typing import Any

import httpx

log = logging.getLogger("worker")

POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "5"))
LEASE_SECONDS = int(os.environ.get("WORKER_LEASE_SECONDS", "300"))
HEARTBEAT_SECONDS = float(os.environ.get("WORKER_HEARTBEAT_SECONDS", "30"))
MAX_DOWNLOAD_BYTES = int(os.environ.get("WORKER_MAX_DOWNLOAD_BYTES", str(50 * 1024 * 1024)))
VERSION = os.environ.get("WORKER_VERSION", "phase-1")
RAW_BUCKET = os.environ.get("RAW_BUCKET", "raw")


class JobError(RuntimeError):
    """
    A failure whose message belongs on the accountant's screen.

    Distinct from an unexpected exception: "this file has no readable table"
    is worth showing verbatim, a KeyError is not. `retryable` defaults to False
    because a JobError describes a conclusion rather than an accident -- running
    it twice more produces the same sentence three times while the user waits to
    read it once.
    """

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Supabase:
    """Minimal PostgREST + Storage client, scoped to the service role."""

    def __init__(self, url: str, key: str):
        self._url = url.rstrip("/")
        self._key = key
        self._client = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            # A flaky link should reconnect rather than lose a job that took
            # four minutes of parsing to reach its final write.
            transport=httpx.HTTPTransport(retries=3),
        )

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        # Built per request rather than stored on the instance, so a stray
        # repr() of this object cannot surface the key.
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def rpc(self, function: str, params: dict[str, Any] | None = None) -> Any:
        response = self._client.post(
            f"{self._url}/rest/v1/rpc/{function}",
            headers=self._headers(),
            json=params or {},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"rpc {function} failed [{response.status_code}]: {response.text[:300]}")
        return response.json() if response.content else None

    def select(self, table: str, columns: str, filters: dict[str, str], limit: int = 1) -> list[dict]:
        params = {"select": columns, "limit": str(limit), **filters}
        response = self._client.get(
            f"{self._url}/rest/v1/{table}", headers=self._headers(), params=params
        )
        if response.status_code >= 400:
            raise RuntimeError(f"select {table} failed [{response.status_code}]: {response.text[:300]}")
        return response.json()

    def download(self, bucket: str, path: str, max_bytes: int) -> bytes:
        """
        Fetch an object, refusing anything past the ceiling.

        Streams rather than trusting Content-Length: the point is to bound this
        process's memory, and a wrong or absent header should not defeat that.
        """
        url = f"{self._url}/storage/v1/object/{bucket}/{path}"
        chunks: list[bytes] = []
        total = 0
        with self._client.stream("GET", url, headers=self._headers()) as response:
            if response.status_code >= 400:
                response.read()
                raise JobError(
                    "The uploaded file could not be read back from storage. "
                    "Try uploading it again.",
                    retryable=response.status_code >= 500,
                )
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise JobError(
                        f"This file is larger than the {max_bytes // (1024 * 1024)} MB limit."
                    )
                chunks.append(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        self._client.close()


# -----------------------------------------------------------------------------
# The one job kind
# -----------------------------------------------------------------------------


def handle_analyze_workbook(supabase: Supabase, job: dict[str, Any], heartbeat) -> dict[str, Any]:
    from .analysis import analyze
    from .main import parse_sheet

    upload_id = job.get("raw_upload_id")
    if not upload_id:
        raise JobError("this job has no upload attached")

    rows = supabase.select(
        "raw_uploads",
        "id,workspace_id,storage_path,original_filename,status",
        {"id": f"eq.{upload_id}"},
    )
    if not rows:
        raise JobError("the upload no longer exists")
    upload = rows[0]

    if upload["status"] != "stored":
        raise JobError(f"the upload is {upload['status']}, not stored; there is nothing to read")

    heartbeat({"stage": "downloading", "file": upload["original_filename"]})
    data = supabase.download(RAW_BUCKET, upload["storage_path"], MAX_DOWNLOAD_BYTES)

    heartbeat({"stage": "reading", "bytes": len(data)})
    try:
        parsed = parse_sheet(data)
    except Exception as error:
        raise JobError(
            "This file could not be read as a workbook. Check that it contains a header "
            "row with data beneath it."
        ) from error

    if parsed["dataframe"].height == 0:
        raise JobError(
            "No data rows were found beneath the header. Check that the sheet is not empty."
        )

    heartbeat({"stage": "analysing", "rows": parsed["dataframe"].height})
    result = analyze(parsed)
    result["filename"] = upload["original_filename"]

    # Carried through so the answer records what was asked, not just what was
    # found. It does not change what the analysis is allowed to touch.
    instructions = (job.get("payload") or {}).get("instructions")
    if instructions:
        result["instructions"] = instructions

    return result


def handle_chat_turn(supabase: Supabase, job: dict[str, Any], heartbeat) -> dict[str, Any]:
    """
    One conversational turn, run here rather than inside a request.

    This is the job kind that most needed moving. A turn is a multi-round loop
    against a free-tier model, and its duration is not something anyone can
    promise in advance -- which is exactly what an HTTP handler is forced to do.
    Queued, a slow model costs the user a longer wait rather than an error.

    The turn's own progress extends the lease as it goes: four rounds of thinking
    must not let another worker decide this job was abandoned.
    """
    import asyncio

    from .chat import ChatError, run_chat_turn

    payload = job.get("payload") or {}
    message = payload.get("message") or ""
    history = payload.get("history") or []

    if not message.strip():
        raise JobError("There was no question to answer.")

    heartbeat({"stage": "thinking"})

    try:
        outcome = asyncio.run(
            run_chat_turn(
                message=message,
                history=history,
                # From the claimed job row, never from the payload: the database
                # decided which tenant this job belongs to when the dashboard
                # enqueued it, and the worker does not get a second opinion.
                workspace_id=job["workspace_id"],
                on_progress=heartbeat,
            )
        )
    except ChatError as error:
        raise JobError(str(error), retryable=error.retryable) from error

    result = outcome.get("result") or {}
    return {
        "reply": result.get("reply") or "",
        "downloads": result.get("downloads") or [],
        "tools_used": (outcome.get("evidence") or {}).get("tools_used") or [],
        "model": (outcome.get("execution_metadata") or {}).get("model"),
    }


HANDLERS = {
    "analyze_workbook": handle_analyze_workbook,
    "chat_turn": handle_chat_turn,
}


# -----------------------------------------------------------------------------
# The loop
# -----------------------------------------------------------------------------


class Worker:
    def __init__(self, supabase: Supabase, worker_id: str):
        self.supabase = supabase
        self.worker_id = worker_id
        self._stopping = threading.Event()
        self._done = 0
        self._failed = 0

    def request_stop(self, signum: int | None = None, _frame: Any = None) -> None:
        """
        Finish the current job, then exit.

        Not an immediate abort: an analysis that is nearly done has already
        spent the expensive part, and a redeploy should not cost the user their
        run. The lease covers the case where the platform kills us anyway.
        """
        if signum is not None:
            log.info("signal %s received; finishing the current job then stopping", signum)
        self._stopping.set()

    def announce(self) -> None:
        self.supabase.rpc(
            "agent_worker_heartbeat", {"p_worker_id": self.worker_id, "p_version": VERSION}
        )

    def claim(self) -> dict[str, Any] | None:
        claimed = self.supabase.rpc(
            "claim_agent_job", {"p_worker_id": self.worker_id, "p_lease_seconds": LEASE_SECONDS}
        )
        # A set-returning function reaches PostgREST as a list; empty means
        # nothing to do.
        if isinstance(claimed, list):
            claimed = claimed[0] if claimed else None
        return claimed if claimed and claimed.get("id") else None

    def _heartbeat_for(self, job_id: str):
        def heartbeat(progress: dict[str, Any]) -> None:
            try:
                self.supabase.rpc(
                    "heartbeat_agent_job",
                    {
                        "p_job_id": job_id,
                        "p_worker_id": self.worker_id,
                        "p_progress": progress,
                        "p_lease_seconds": LEASE_SECONDS,
                    },
                )
            except Exception as error:  # noqa: BLE001
                # Not fatal. The lease may still be valid; if it is not, the
                # completion call is rejected and another worker has the job --
                # which is correct.
                log.warning("heartbeat for %s failed: %s", job_id, error)

        return heartbeat

    def finish(self, job_id: str, success: bool, result=None, error=None, retryable=True) -> None:
        self.supabase.rpc(
            "finish_agent_job",
            {
                "p_job_id": job_id,
                "p_worker_id": self.worker_id,
                "p_success": success,
                "p_result": result,
                "p_error": error,
                "p_retryable": retryable,
            },
        )

    def run_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        kind = job["kind"]
        handler = HANDLERS.get(kind)

        log.info("job %s: %s (attempt %s)", job_id, kind, job.get("attempts"))

        if handler is None:
            # Reachable when the database knows a job kind this build does not,
            # i.e. the dashboard was deployed and the worker was not.
            self.finish(job_id, False, None, f"this engine build cannot run {kind!r}", False)
            self._failed += 1
            return

        started = time.perf_counter()
        try:
            result = handler(self.supabase, job, self._heartbeat_for(job_id))
            elapsed = int((time.perf_counter() - started) * 1000)
            if isinstance(result, dict):
                result.setdefault("duration_ms", elapsed)
            self.finish(job_id, True, result)
            self._done += 1
            log.info("job %s: done in %sms", job_id, elapsed)

        except JobError as error:
            log.warning("job %s: %s", job_id, error)
            self.finish(job_id, False, None, str(error), error.retryable)
            self._failed += 1

        except Exception as error:  # noqa: BLE001 - the loop must never die on a job
            log.exception("job %s: unexpected failure", job_id)
            self.finish(
                job_id,
                False,
                None,
                f"The engine hit an unexpected error ({type(error).__name__}). "
                f"The details are in the engine log.",
            )
            self._failed += 1

    def run_forever(self) -> int:
        log.info("worker %s starting (version %s)", self.worker_id, VERSION)

        try:
            self.announce()
        except Exception as error:  # noqa: BLE001
            log.error("could not register with Supabase: %s", error)
            return 1

        last_announce = time.monotonic()
        backoff = POLL_SECONDS

        while not self._stopping.is_set():
            try:
                now = time.monotonic()
                if now - last_announce >= HEARTBEAT_SECONDS:
                    self.announce()
                    last_announce = now

                job = self.claim()
                if job is None:
                    self._stopping.wait(POLL_SECONDS)
                    backoff = POLL_SECONDS
                    continue

                self.run_job(job)
                backoff = POLL_SECONDS

            except Exception:  # noqa: BLE001
                # The database is unreachable, or something else went wrong at
                # the loop level. Back off rather than hammering it, but keep
                # trying forever -- a worker that gives up after five attempts
                # is a worker somebody has to notice and restart.
                log.exception("worker loop error; retrying in %ss", backoff)
                self._stopping.wait(backoff)
                backoff = min(backoff * 2, 120)

        log.info("stopped after %s done, %s failed", self._done, self._failed)
        return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        # No timestamp: journald, Docker and Render all add their own, and two
        # per line makes the log harder to read rather than easier.
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    url = os.environ.get("SUPABASE_URL", "").rstrip("/") or os.environ.get(
        "NEXT_PUBLIC_SUPABASE_URL", ""
    ).rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_SECRET_KEY", "")

    if not url or not key:
        log.error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
        return 2

    # Stable across restarts on a fixed host, unique when several run side by
    # side, so the worker table shows hosts rather than a growing list of ghosts.
    worker_id = os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

    supabase = Supabase(url, key)
    worker = Worker(supabase, worker_id)
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)

    try:
        return worker.run_forever()
    finally:
        supabase.close()


if __name__ == "__main__":
    raise SystemExit(main())
