"""
Configuration, read once at startup.

Everything the agent needs comes from the environment, because the agent runs
on a host the web app knows nothing about (a Hostinger VPS, in the deployment
this repo documents) and the two are only ever configured separately.

The validation here is deliberately loud. A worker that starts with a missing
secret and then fails every job at 03:00 is worse than one that refuses to
start at all, because the first looks like it is working.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# The job kinds this build knows how to execute. Sent to the database on every
# heartbeat, so the dashboard can distinguish "no worker" from "a worker that
# is too old to do what you just asked".
CAPABILITIES = (
    "parse_workbook",
    "profile_dataset",
    "propose_cleaning",
    "apply_cleaning",
    "query_dataset",
    "reconcile_sources",
    "generate_report",
)

VERSION = "0.2.0"


class ConfigError(RuntimeError):
    """Raised at startup when the environment cannot support a working agent."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. The agent cannot reach Supabase without it; "
            f"see services/hermes/.env.example."
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LLMConfig:
    """
    Model routing (PRD section 8: "OpenAI and Kimi are interchangeable reasoning
    models routed by task, cost and quality").

    Both are optional. With neither configured the agent still runs every
    deterministic tool and still produces cleaning proposals from its rule
    engine -- it simply cannot write prose explanations. That is the right
    default for a pilot: the numbers never depended on the model anyway, so
    losing the model must not lose the product.
    """

    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"

    kimi_api_key: str | None = None
    kimi_base_url: str = "https://api.moonshot.ai/v1"
    kimi_model: str = "kimi-k2-0905-preview"

    # Which provider handles which class of work. "reasoning" is proposal
    # generation and anomaly explanation; "drafting" is narrative prose, where
    # the cheaper model is usually indistinguishable.
    reasoning_provider: str = "openai"
    drafting_provider: str = "openai"

    timeout_seconds: int = 90
    max_output_tokens: int = 2000

    @property
    def enabled(self) -> bool:
        return bool(self.openai_api_key or self.kimi_api_key)

    def provider_for(self, task: str) -> str | None:
        """
        The provider to use for a task class, falling back to whichever is
        actually configured. Routing preferences must not become an outage: if
        the operator set the router to Kimi but only supplied an OpenAI key,
        run on OpenAI rather than refusing.
        """
        preferred = self.reasoning_provider if task == "reasoning" else self.drafting_provider
        if preferred == "openai" and self.openai_api_key:
            return "openai"
        if preferred == "kimi" and self.kimi_api_key:
            return "kimi"
        if self.openai_api_key:
            return "openai"
        if self.kimi_api_key:
            return "kimi"
        return None


@dataclass(frozen=True)
class Config:
    supabase_url: str
    service_key: str

    worker_id: str
    hostname: str
    version: str = VERSION
    capabilities: tuple[str, ...] = CAPABILITIES

    # How long a claimed job is ours before another worker may take it. Long
    # enough to survive a slow parse, short enough that a crashed worker's jobs
    # come back within a coffee break.
    lease_seconds: int = 300
    heartbeat_seconds: int = 30
    # Sleep between claim attempts when the queue is empty. Postgres LISTEN
    # would be tighter, but a 3-second poll on an idle queue is a negligible
    # cost against a job that takes minutes, and it survives connection drops
    # without any reconnection logic.
    poll_seconds: int = 3

    max_download_bytes: int = 50 * 1024 * 1024
    work_dir: Path = field(default_factory=lambda: Path("/tmp/hermes"))

    llm: LLMConfig = field(default_factory=LLMConfig)

    # Section 8's context discipline, enforced as a setting so it is auditable
    # rather than merely intended. Raising it is a deliberate, visible act.
    max_sample_values: int = 5
    redact_samples: bool = True


def load_config() -> Config:
    """Build the config from the environment, or raise ConfigError."""
    url = _require("SUPABASE_URL").rstrip("/")
    key = _require("SUPABASE_SECRET_KEY")

    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"SUPABASE_URL must be a full URL, got {url!r}")

    # A stable id keeps one row per host in agent_workers across restarts, so
    # the dashboard shows "the agent restarted" rather than accumulating a new
    # ghost worker every deploy.
    hostname = socket.gethostname()
    worker_id = os.environ.get("HERMES_WORKER_ID", "").strip() or f"hermes-{hostname}"
    if len(worker_id) > 200:
        worker_id = worker_id[:200]

    llm = LLMConfig(
        openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip() or None,
        openai_base_url=os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1",
        openai_model=os.environ.get("OPENAI_MODEL", "").strip() or "gpt-4.1-mini",
        kimi_api_key=os.environ.get("KIMI_API_KEY", "").strip() or None,
        kimi_base_url=os.environ.get("KIMI_BASE_URL", "").strip() or "https://api.moonshot.ai/v1",
        kimi_model=os.environ.get("KIMI_MODEL", "").strip() or "kimi-k2-0905-preview",
        reasoning_provider=os.environ.get("HERMES_REASONING_PROVIDER", "").strip() or "openai",
        drafting_provider=os.environ.get("HERMES_DRAFTING_PROVIDER", "").strip() or "openai",
        timeout_seconds=_int("HERMES_LLM_TIMEOUT_SECONDS", 90),
        max_output_tokens=_int("HERMES_LLM_MAX_OUTPUT_TOKENS", 2000),
    )

    work_dir = Path(os.environ.get("HERMES_WORK_DIR", "").strip() or "/tmp/hermes")

    return Config(
        supabase_url=url,
        service_key=key,
        worker_id=worker_id,
        hostname=hostname,
        lease_seconds=_int("HERMES_LEASE_SECONDS", 300),
        heartbeat_seconds=_int("HERMES_HEARTBEAT_SECONDS", 30),
        poll_seconds=_int("HERMES_POLL_SECONDS", 3),
        max_download_bytes=_int("HERMES_MAX_DOWNLOAD_BYTES", 50 * 1024 * 1024),
        work_dir=work_dir,
        llm=llm,
        max_sample_values=_int("HERMES_MAX_SAMPLE_VALUES", 5),
        redact_samples=_bool("HERMES_REDACT_SAMPLES", True),
    )


def new_run_id() -> str:
    return str(uuid.uuid4())
