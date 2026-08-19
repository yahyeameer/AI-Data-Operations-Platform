"""
The worker loop.

This is the process that runs on the VPS forever. Everything about it is shaped
by that: it must survive a network blip, a bad file, a killed process and a
reboot, and it must be diagnosable by someone reading `journalctl` at midnight
with no debugger.

The loop is deliberately boring.

    heartbeat -> claim -> run -> report -> repeat

There is no in-process scheduling, no thread pool and no async. One job at a
time, in one process, with the database as the only shared state. To run more
work, run more copies -- `claim_agent_job` uses `skip locked`, so two workers on
two hosts cooperate with no coordination between them. Concurrency that lives
in the database is concurrency you can reason about from a SQL prompt.

Failure handling has one rule: the job must always reach a terminal state or
lose its lease. A worker that dies holding a claim is fine, because the lease
expires. A worker that swallows an exception and returns to the top of the loop
without reporting is not, so the try/except around the handler is total.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import Any

from .config import Config, ConfigError, load_config
from .jobs import HANDLERS, JobContext, JobError
from .llm.router import LLMRouter
from .supabase import SupabaseClient, SupabaseError

log = logging.getLogger("hermes.worker")


class Worker:
    def __init__(self, config: Config):
        self.config = config
        self.supabase = SupabaseClient(config.supabase_url, config.service_key)
        self.llm = LLMRouter(config.llm)
        self._stopping = threading.Event()
        self._jobs_done = 0
        self._jobs_failed = 0

    # -- lifecycle -----------------------------------------------------------

    def request_stop(self, signum: int | None = None, _frame: Any = None) -> None:
        """
        Finish the current job, then exit.

        Deliberately not an immediate abort. A parse that is 90% done has
        already spent the expensive part; killing it means redoing that work
        after the restart, and a deploy should not cost the customer their
        month-end run. systemd's default 90-second stop timeout is enough for
        anything this service does, and the lease covers the case where it is
        not.
        """
        if signum is not None:
            log.info("signal %s received; finishing the current job then stopping", signum)
        self._stopping.set()

    def close(self) -> None:
        self.llm.close()
        self.supabase.close()

    # -- database chores -----------------------------------------------------

    def announce(self) -> None:
        self.supabase.rpc(
            "agent_worker_heartbeat",
            {
                "p_worker_id": self.config.worker_id,
                "p_hostname": self.config.hostname,
                "p_version": self.config.version,
                "p_capabilities": list(self.config.capabilities),
                "p_metadata": {
                    "llm_enabled": self.llm.enabled,
                    "reasoning_provider": self.config.llm.provider_for("reasoning"),
                    "jobs_done": self._jobs_done,
                    "jobs_failed": self._jobs_failed,
                },
            },
        )

    def claim(self) -> dict[str, Any] | None:
        claimed = self.supabase.rpc(
            "claim_agent_job",
            {
                "p_worker_id": self.config.worker_id,
                "p_kinds": list(self.config.capabilities),
                "p_lease_seconds": self.config.lease_seconds,
            },
        )
        # PostgREST returns a single-row set for a function returning a record;
        # null or an empty list both mean "nothing to do".
        if isinstance(claimed, list):
            claimed = claimed[0] if claimed else None
        if not claimed or not claimed.get("id"):
            return None
        return claimed

    def _heartbeat_for(self, job_id: str):
        def heartbeat(progress: dict[str, Any]) -> None:
            try:
                self.supabase.rpc(
                    "heartbeat_agent_job",
                    {
                        "p_job_id": job_id,
                        "p_worker_id": self.config.worker_id,
                        "p_progress": progress,
                        "p_lease_seconds": self.config.lease_seconds,
                    },
                )
            except SupabaseError as error:
                # A failed heartbeat is not fatal. The lease may still be valid,
                # and if it is not, the completion call will be rejected and
                # another worker will have taken the job -- which is correct.
                log.warning("heartbeat for %s failed: %s", job_id, error)

        return heartbeat

    def finish(
        self,
        job_id: str,
        success: bool,
        result: Any = None,
        error: str | None = None,
        retryable: bool = True,
    ) -> None:
        self.supabase.rpc(
            "finish_agent_job",
            {
                "p_job_id": job_id,
                "p_worker_id": self.config.worker_id,
                "p_success": success,
                "p_result": result,
                "p_error": error,
                "p_retryable": retryable,
            },
        )

    # -- the loop ------------------------------------------------------------

    def run_job(self, job: dict[str, Any]) -> None:
        kind = job["kind"]
        job_id = job["id"]
        handler = HANDLERS.get(kind)

        log.info(
            "job %s: %s for workspace %s (attempt %s)",
            job_id, kind, job["workspace_id"], job.get("attempts"),
        )

        if handler is None:
            # Reachable if the database knows a job kind this build does not --
            # i.e. the web app was deployed and the agent was not.
            self.finish(job_id, False, None, f"this agent build cannot run {kind!r}")
            self._jobs_failed += 1
            return

        context = JobContext(
            config=self.config,
            supabase=self.supabase,
            llm=self.llm,
            job=job,
            heartbeat=self._heartbeat_for(job_id),
        )

        started = time.perf_counter()
        try:
            result = handler(context)
            elapsed = int((time.perf_counter() - started) * 1000)
            if isinstance(result, dict):
                result.setdefault("duration_ms", elapsed)
            self.finish(job_id, True, result)
            self._jobs_done += 1
            log.info("job %s: done in %sms", job_id, elapsed)

        except JobError as error:
            # A message written for the accountant. Shown verbatim, and normally
            # not retried -- see JobError.
            log.warning("job %s: %s", job_id, error)
            self.finish(job_id, False, None, str(error), retryable=error.retryable)
            self._jobs_failed += 1

        except SupabaseError as error:
            log.error("job %s: supabase error %s %s", job_id, error.status, error.body)
            self.finish(
                job_id, False, None,
                f"Lost contact with Supabase while running this job ({error.status}). "
                f"It will be retried.",
            )
            self._jobs_failed += 1

        except Exception as error:  # noqa: BLE001 - the loop must never die on a job
            log.exception("job %s: unexpected failure", job_id)
            self.finish(
                job_id, False, None,
                f"The agent hit an unexpected error ({type(error).__name__}). "
                f"The details are in the agent log.",
            )
            self._jobs_failed += 1

    def run_forever(self) -> int:
        log.info(
            "hermes %s starting as %s on %s (models: %s)",
            self.config.version,
            self.config.worker_id,
            self.config.hostname,
            self.config.llm.provider_for("reasoning") or "none (rule engine only)",
        )

        try:
            self.announce()
        except SupabaseError as error:
            log.error("could not register with Supabase: %s %s", error.status, error.body)
            return 1

        last_announce = time.monotonic()
        backoff = self.config.poll_seconds

        while not self._stopping.is_set():
            try:
                now = time.monotonic()
                if now - last_announce >= self.config.heartbeat_seconds:
                    self.announce()
                    last_announce = now

                job = self.claim()

                if job is None:
                    self._stopping.wait(self.config.poll_seconds)
                    backoff = self.config.poll_seconds
                    continue

                self.run_job(job)
                backoff = self.config.poll_seconds

            except SupabaseError as error:
                # The database is unreachable. Back off rather than hammering
                # it, but keep trying forever -- a VPS that gives up after five
                # attempts is a VPS somebody has to notice and restart.
                log.error("supabase unreachable (%s); retrying in %ss", error.status, backoff)
                self._stopping.wait(backoff)
                backoff = min(backoff * 2, 120)

            except Exception:  # noqa: BLE001
                log.exception("unexpected error in the worker loop; continuing")
                self._stopping.wait(backoff)
                backoff = min(backoff * 2, 120)

        log.info(
            "stopped after %s job(s) done, %s failed", self._jobs_done, self._jobs_failed
        )
        return 0


def configure_logging() -> None:
    import os

    level = os.environ.get("HERMES_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        # No timestamp: journald and Docker both add their own, and two of them
        # on every line makes the log harder to read, not easier.
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_local_env() -> None:
    """
    Environment loading, with a development convenience.

    In production the agent's own `.env` (or the systemd unit's EnvironmentFile)
    is the only source. On a developer's machine the same Supabase credentials
    already exist in `apps/web/.env.local`, and requiring them to be copied into
    a second file is how the two drift apart after a `supabase start` reissues
    the keys.

    So: the agent's own environment always wins, and the web app's file is read
    only to fill gaps. NEXT_PUBLIC_SUPABASE_URL becomes SUPABASE_URL because the
    agent is not a browser and the prefix would be misleading here.
    """
    try:
        from dotenv import dotenv_values, load_dotenv
    except ImportError:
        return

    import os
    from pathlib import Path

    load_dotenv()

    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SECRET_KEY"):
        return

    web_env = Path(__file__).resolve().parents[3] / "apps" / "web" / ".env.local"
    if not web_env.is_file():
        return

    values = dotenv_values(web_env)
    mapping = {
        "SUPABASE_URL": values.get("NEXT_PUBLIC_SUPABASE_URL"),
        "SUPABASE_SECRET_KEY": values.get("SUPABASE_SECRET_KEY"),
    }
    for name, value in mapping.items():
        if value and not os.environ.get(name):
            os.environ[name] = value

    log.info("filled missing Supabase settings from %s (development only)", web_env)


def main() -> int:
    configure_logging()
    load_local_env()

    try:
        config = load_config()
    except ConfigError as error:
        log.error("%s", error)
        return 2

    config.work_dir.mkdir(parents=True, exist_ok=True)

    worker = Worker(config)
    signal.signal(signal.SIGINT, worker.request_stop)
    signal.signal(signal.SIGTERM, worker.request_stop)

    try:
        return worker.run_forever()
    finally:
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
