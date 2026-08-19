"""
Model routing (PRD section 8).

OpenAI and Kimi are interchangeable because both speak the OpenAI chat
completions shape, so one code path serves both and switching is a config
change rather than a rewrite. That is the whole point of the "interchangeable"
claim -- and the eval harness is what lets anyone act on it, because a router
you cannot measure is a router you will never dare switch in production.

Three rules hold everywhere in this file.

**The model never produces a number that reaches a report.** It writes prose
and it emits structured query *specifications*, which `analyze.compile_query`
validates against the real column list before any SQL exists. If it invents a
column, compilation fails loudly. If it invents a total, nothing reads it.

**Every call degrades to a deterministic fallback.** No key, no network, a
timeout, a malformed response, a refusal -- all of them land in the same place:
the rule-based text the caller already had. A pilot customer's month-end must
not depend on an API being up.

**Nothing goes out except a redacted context.** See `redact.build_context`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import LLMConfig

log = logging.getLogger("hermes.llm")

MAX_RATIONALE_CHARS = 600


@dataclass
class LLMResult:
    content: str | None
    provider: str | None
    model: str | None
    ok: bool
    error: str | None = None


class LLMRouter:
    def __init__(self, config: LLMConfig):
        self._config = config
        self._client = httpx.Client(timeout=httpx.Timeout(config.timeout_seconds, connect=15.0))

    def close(self) -> None:
        self._client.close()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    # -- transport -----------------------------------------------------------

    def _endpoint(self, provider: str) -> tuple[str, str, str]:
        if provider == "kimi":
            return (
                self._config.kimi_base_url.rstrip("/") + "/chat/completions",
                self._config.kimi_api_key or "",
                self._config.kimi_model,
            )
        return (
            self._config.openai_base_url.rstrip("/") + "/chat/completions",
            self._config.openai_api_key or "",
            self._config.openai_model,
        )

    def _complete(
        self,
        task: str,
        system: str,
        user: str,
        json_mode: bool = False,
    ) -> LLMResult:
        provider = self._config.provider_for(task)
        if provider is None:
            return LLMResult(None, None, None, False, "no model configured")

        url, key, model = self._endpoint(provider)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self._config.max_output_tokens,
            # Low but not zero. These are explanation tasks where a completely
            # greedy decode tends to produce the same stock phrasing for every
            # dataset, which reads as boilerplate and gets ignored.
            "temperature": 0.2,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = self._client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            if response.status_code >= 400:
                return LLMResult(
                    None, provider, model, False,
                    f"{provider} returned {response.status_code}: {response.text[:200]}",
                )
            body = response.json()
            content = (body.get("choices") or [{}])[0].get("message", {}).get("content")
            if not content:
                return LLMResult(None, provider, model, False, "empty response")
            return LLMResult(content.strip(), provider, model, True)
        except Exception as error:  # noqa: BLE001 - every failure is a fallback, not a crash
            log.warning("%s call failed: %s", provider, error)
            return LLMResult(None, provider, model, False, str(error))

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any] | None:
        """
        Models sometimes wrap JSON in a fence despite being asked not to.
        Recovering from that is cheaper than a retry and than failing the job.
        """
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text[: text.rfind("```")]
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                try:
                    parsed = json.loads(text[start : end + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
            return None

    # -- tasks ---------------------------------------------------------------

    def explain_proposals(
        self, context: dict[str, Any], proposals: list[dict[str, Any]]
    ) -> tuple[dict[str, str], str | None]:
        """
        Rewrite each proposal's rationale in the language an accountant uses.

        The proposals themselves -- which rows, which operation, how much money
        -- are already decided. This only changes the sentence. A model that
        returns nothing, or a key nobody asked about, costs the product its
        prose and nothing else.
        """
        if not self.enabled or not proposals:
            return {}, None

        system = (
            "You are assisting a UK accountant reviewing proposed data-cleaning changes to a "
            "client's monthly export. For each change you are given the finding and the "
            "system's own draft explanation.\n\n"
            "Rewrite each explanation so a qualified accountant can decide quickly. Rules:\n"
            "- State what was found, then why it matters to the accounts.\n"
            "- Two sentences at most. Plain English. No preamble, no headings.\n"
            "- Use only the figures given to you. Never estimate, recompute or introduce a "
            "number that is not in the input.\n"
            "- If a change could be wrong, say what would make it wrong.\n"
            "- Do not tell the user to approve or reject. They decide.\n\n"
            'Return JSON: {"rationales": {"<group_key>": "<text>"}}'
        )

        user = json.dumps(
            {
                "dataset": context,
                "proposals": [
                    {
                        "group_key": proposal["group_key"],
                        "title": proposal["title"],
                        "operation": proposal["operation"].get("op"),
                        "column": proposal.get("column_name"),
                        "affected_rows": proposal["affected_rows"],
                        "materiality_gbp": proposal.get("materiality_gbp"),
                        "tier": proposal["confidence"],
                        "draft": proposal["rationale"],
                    }
                    for proposal in proposals
                ],
            },
            default=str,
        )

        result = self._complete("reasoning", system, user, json_mode=True)
        if not result.ok or not result.content:
            return {}, None

        parsed = self._parse_json(result.content)
        if not parsed or not isinstance(parsed.get("rationales"), dict):
            return {}, result.model

        valid_keys = {proposal["group_key"] for proposal in proposals}
        rationales = {
            key: str(value).strip()[:MAX_RATIONALE_CHARS]
            for key, value in parsed["rationales"].items()
            if key in valid_keys and isinstance(value, str) and value.strip()
        }
        return rationales, result.model

    def plan_query(
        self, question: str, context: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """
        Natural language to a structured query spec.

        Returns the spec, the model that produced it and an error if it failed.
        The spec is *not* trusted: the caller passes it to `compile_query`,
        which validates every identifier against the dataset's real columns and
        raises on anything else. The model chooses what to ask; it does not
        choose what runs.
        """
        if not self.enabled:
            return None, None, "no model configured"

        columns = [
            {"name": column["name"], "type": column["type"]} for column in context.get("columns", [])
        ]

        system = (
            "Translate an accountant's question into a JSON query specification over one table.\n\n"
            "Schema (use these column names exactly; inventing one is an error):\n"
            f"{json.dumps(columns)}\n\n"
            "Specification format:\n"
            "{\n"
            '  "select": [{"column": "<name>", "agg": "sum|avg|min|max|count|count_distinct|median", '
            '"alias": "<name>"}],\n'
            '  "group_by": ["<name>"],\n'
            '  "filters": [{"column": "<name>", "op": "eq|neq|gt|gte|lt|lte|like|in|between|is_null|not_null", '
            '"value": <value>}],\n'
            '  "order_by": [{"column": "<name or alias>", "direction": "asc|desc"}],\n'
            '  "limit": <int>\n'
            "}\n\n"
            "Rules:\n"
            "- Dates are ISO strings; use 'between' with two dates for a period.\n"
            "- Every non-aggregated selected column must also appear in group_by.\n"
            "- Prefer sum() for money questions and count for 'how many'.\n"
            '- If the question cannot be answered from this schema, return {"error": "<why>"}.\n'
            "- Return only the JSON object."
        )

        result = self._complete("reasoning", system, question, json_mode=True)
        if not result.ok or not result.content:
            return None, result.model, result.error

        parsed = self._parse_json(result.content)
        if not parsed:
            return None, result.model, "the model did not return usable JSON"
        if parsed.get("error"):
            return None, result.model, str(parsed["error"])[:300]
        if not parsed.get("select"):
            return None, result.model, "the model returned a query that selects nothing"

        return parsed, result.model, None

    def narrate(self, context: dict[str, Any], figures: dict[str, Any]) -> tuple[str | None, str | None]:
        """
        The month's commentary.

        Every figure is supplied. The model arranges and explains them; it does
        not calculate. Section 8's table puts "drafting the monthly narrative"
        in the AI column and "every number inside it" in the deterministic one,
        and that line is exactly here.
        """
        if not self.enabled:
            return None, None

        system = (
            "Write a short month-end commentary for a UK accountant on a client's data.\n\n"
            "Rules:\n"
            "- Use only the figures provided. Never calculate, estimate or infer a new number. "
            "If a figure is not given, do not mention it.\n"
            "- Three or four sentences. Lead with what changed most.\n"
            "- Mention data-quality issues only where they affect a figure's reliability.\n"
            "- Plain English, no bullet points, no headings, no sign-off."
        )

        user = json.dumps({"dataset": context, "figures": figures}, default=str)
        result = self._complete("drafting", system, user)
        if not result.ok or not result.content:
            return None, None
        return result.content[:2000], result.model


__all__ = ["LLMResult", "LLMRouter"]
