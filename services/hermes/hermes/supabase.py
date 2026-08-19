"""
Supabase transport.

A small hand-written client over httpx rather than the supabase-py SDK. The
agent uses perhaps a dozen endpoints, all of them PostgREST or Storage, and a
thin wrapper we can read end to end is worth more here than a dependency whose
retry and error semantics we would have to reverse-engineer the first time a
job fails at 3am on a VPS.

Every call carries the service key, so this module is the most privileged code
in the deployment. Two consequences are load-bearing:

  * It never accepts a workspace id from anywhere but a claimed job row. The
    database decided which tenant that job belongs to; the worker does not get
    a second opinion.
  * Errors carry the status and body but are logged without the key, which is
    why the headers are built per-request rather than stored on the instance
    where a stray repr() could surface them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("hermes.supabase")


class SupabaseError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class StoredObject:
    bucket: str
    path: str
    size: int


class SupabaseClient:
    """Synchronous PostgREST + Storage client scoped to the service role."""

    def __init__(self, url: str, service_key: str, timeout: float = 60.0):
        self._url = url.rstrip("/")
        self._key = service_key
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=15.0),
            # A VPS on a flaky link should reconnect rather than fail a job that
            # took four minutes of parsing to reach its final write.
            transport=httpx.HTTPTransport(retries=3),
        )

    # -- plumbing ------------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, f"{self._url}{path}", **kwargs)
        if response.status_code >= 400:
            body = response.text[:2000]
            raise SupabaseError(
                f"{method} {path} failed with {response.status_code}",
                status=response.status_code,
                body=body,
            )
        return response

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SupabaseClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- RPC -----------------------------------------------------------------

    def rpc(self, function: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request(
            "POST",
            f"/rest/v1/rpc/{function}",
            headers=self._headers(),
            content=json.dumps(params or {}, default=str),
        )
        if not response.content:
            return None
        return response.json()

    # -- tables --------------------------------------------------------------

    def select(
        self,
        table: str,
        columns: str = "*",
        filters: dict[str, str] | None = None,
        order: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"select": columns}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)

        response = self._request(
            "GET", f"/rest/v1/{table}", headers=self._headers(), params=params
        )
        return response.json()

    def insert(self, table: str, rows: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
        payload = rows if isinstance(rows, list) else [rows]
        response = self._request(
            "POST",
            f"/rest/v1/{table}",
            headers=self._headers({"Prefer": "return=representation"}),
            content=json.dumps(payload, default=str),
        )
        return response.json() if response.content else []

    def update(self, table: str, filters: dict[str, str], values: dict[str, Any]) -> list[dict[str, Any]]:
        response = self._request(
            "PATCH",
            f"/rest/v1/{table}",
            headers=self._headers({"Prefer": "return=representation"}),
            params=filters,
            content=json.dumps(values, default=str),
        )
        return response.json() if response.content else []

    # -- storage -------------------------------------------------------------

    def download(self, bucket: str, path: str, max_bytes: int) -> bytes:
        """
        Fetch an object, refusing anything larger than the configured ceiling.

        The size check streams rather than trusting Content-Length: the point is
        to bound this process's memory on a small VPS, and a wrong or absent
        header should not be able to defeat that.
        """
        url = f"{self._url}/storage/v1/object/{bucket}/{path}"
        chunks: list[bytes] = []
        total = 0

        with self._client.stream("GET", url, headers=self._headers()) as response:
            if response.status_code >= 400:
                response.read()
                raise SupabaseError(
                    f"download of {bucket}/{path} failed with {response.status_code}",
                    status=response.status_code,
                    body=response.text[:500],
                )
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise SupabaseError(
                        f"{bucket}/{path} exceeds the {max_bytes} byte download limit"
                    )
                chunks.append(chunk)

        return b"".join(chunks)

    def upload(
        self,
        bucket: str,
        path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        upsert: bool = False,
    ) -> StoredObject:
        url = f"{self._url}/storage/v1/object/{bucket}/{path}"
        headers = self._headers({"Content-Type": content_type})
        if upsert:
            headers["x-upsert"] = "true"

        response = self._client.post(url, headers=headers, content=data)
        if response.status_code >= 400:
            raise SupabaseError(
                f"upload to {bucket}/{path} failed with {response.status_code}",
                status=response.status_code,
                body=response.text[:500],
            )
        return StoredObject(bucket=bucket, path=path, size=len(data))
