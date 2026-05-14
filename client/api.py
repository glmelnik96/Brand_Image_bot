"""
PhygitalClient: async HTTP-обёртка над приватным API Phygital+.

Бэк живёт на app-server-azure.phygital.plus. Фронт — на app.phygital.plus.
Auth: SuperTokens, Bearer JWT из cookie st-access-token подставляется в Authorization.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import httpx
import truststore
from loguru import logger

from client.session import Session, SessionManager

# Используем системный keychain (macOS Keychain / Windows Cert Store / Linux CA bundle).
# Нужно, если в твоей среде стоит корпоративный/локальный MITM CA, которому
# Python certifi-бандл не доверяет, а браузер — доверяет.
_SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

API_BASE = "https://app-server-azure.phygital.plus"
AUTH_BASE = "https://app.phygital.plus"
ORIGIN = "https://app.phygital.plus"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# Заголовки, которые backend требует/ожидает (из HAR)
PHYGITAL_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Origin": ORIGIN,
    "Referer": f"{ORIGIN}/",
    "rid": "anti-csrf",          # SuperTokens
    "st-auth-mode": "header",    # SuperTokens
    "sec-ch-ua": '"Chromium";v="129", "Not=A?Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "priority": "u=1, i",
}


class PhygitalAuthError(Exception):
    """Сессия невалидна / истёк access_token."""


class PhygitalClient:
    def __init__(
        self,
        session: Session,
        *,
        session_manager: SessionManager | None = None,
        timeout: float = 60.0,
    ) -> None:
        """
        :param session: текущая сессия (cookies)
        :param session_manager: если передан — клиент сам обновит токен при 401 и повторит запрос
        :param timeout: HTTP timeout, сек
        """
        self.session = session
        self.session_manager = session_manager
        self._client: httpx.AsyncClient | None = None
        self._timeout = timeout

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def __aenter__(self) -> "PhygitalClient":
        if not self.session.access_token:
            raise PhygitalAuthError("Session has no access_token")
        self._client = self._build_client()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    def _build_client(self) -> httpx.AsyncClient:
        headers = dict(PHYGITAL_HEADERS)
        headers["Authorization"] = f"Bearer {self.session.access_token}"
        return httpx.AsyncClient(
            headers=headers,
            cookies=self.session.cookie_jar,
            timeout=self._timeout,
            http2=True,
            follow_redirects=False,
            verify=_SSL_CTX,
        )

    async def _rebuild_after_refresh(self) -> None:
        """Пересоздаём httpx-клиент с новым Bearer и cookies из обновлённой сессии."""
        if self._client:
            await self._client.aclose()
        self._client = self._build_client()

    # ── core ──────────────────────────────────────────────────────────────
    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client, "Use 'async with PhygitalClient(...)' context"
        logger.debug(f"{method} {url}")
        resp = await self._client.request(method, url, **kwargs)

        # SuperTokens сигнализирует "try refresh" статусами 401 и 418
        if resp.status_code in (401, 418) and self.session_manager is not None:
            logger.info(f"{resp.status_code} from {url} — refreshing session…")
            try:
                await self.session_manager.refresh(self.session)
            except Exception as e:
                raise PhygitalAuthError(f"Refresh failed: {e}") from e
            await self._rebuild_after_refresh()
            logger.debug(f"Retry {method} {url} with new token")
            resp = await self._client.request(method, url, **kwargs)

        if resp.status_code in (401, 418):
            raise PhygitalAuthError(
                f"{resp.status_code} from {url} — auth failed (no refresh available or refresh did not help)"
            )
        resp.raise_for_status()
        return resp

    async def api_get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", f"{API_BASE}{path}", **kwargs)

    async def api_post(self, path: str, *, json: Any = None, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", f"{API_BASE}{path}", json=json, **kwargs)

    # ── high-level endpoints (по recon) ───────────────────────────────────
    async def submit_task(self, payload: dict[str, Any]) -> int:
        """POST /api/v2/tasks/  → task_id"""
        resp = await self.api_post("/api/v2/tasks/", json=payload)
        return resp.json()["task_id"]

    async def post_config_history(self, task_id: int, config: dict[str, Any]) -> None:
        """POST /api/v2/tasks/config_history — судя по поведению, обязательный шаг:
        без него таск остаётся в status='pending' навсегда."""
        await self.api_post("/api/v2/tasks/config_history", json={"taskId": task_id, "config": config})

    async def task_status(self, task_id: int) -> dict[str, Any]:
        """GET /api/v2/tasks/queue-position/<id>
        Возвращает {status: new|running|done|..., outputs, position, progress, ...}

        Phygital периодически кидает 500 на этот эндпоинт — это транзиентный сбой
        (видимо, бэк-сервис не успевает за БД). Ретраим 5xx + connection errors
        с экспоненциальным бэкоффом: 1s → 3s → 9s.
        """
        delays = (1.0, 3.0, 9.0)
        for attempt in range(len(delays) + 1):
            try:
                resp = await self.api_get(f"/api/v2/tasks/queue-position/{task_id}")
                return resp.json()
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if 500 <= code < 600 and attempt < len(delays):
                    logger.warning(
                        f"task_status({task_id}) → {code}, retry "
                        f"{attempt + 1}/{len(delays)} in {delays[attempt]}s"
                    )
                    await asyncio.sleep(delays[attempt])
                    continue
                raise
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.PoolTimeout,
                httpx.ReadTimeout,
            ) as e:
                if attempt < len(delays):
                    logger.warning(
                        f"task_status({task_id}) {type(e).__name__}: {e}; retry "
                        f"{attempt + 1}/{len(delays)} in {delays[attempt]}s"
                    )
                    await asyncio.sleep(delays[attempt])
                    continue
                raise
        # unreachable
        raise RuntimeError("task_status retry loop fell through")

    async def get_download_links(self, link_ids: list[int]) -> list[dict[str, Any]]:
        """POST /api/v2/storage-object/storage-object/download-links → [{file_name, download_link, ...}]"""
        resp = await self.api_post(
            "/api/v2/storage-object/storage-object/download-links",
            json={"link_ids": link_ids},
        )
        return resp.json()["links"]

    async def get_credits_price(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v2/nodes/get_credits_price → {price, details}"""
        resp = await self.api_post("/api/v2/nodes/get_credits_price", json=payload)
        return resp.json()

    async def upload_file(self, path: str | "Path", *, field_name: str = "fileobject") -> int:
        """POST /api/v2/storage-object/storage-object (multipart) → file_obj_id.

        Сервер ожидает multipart-поле с именем `fileobject` (422 без него).
        Большие файлы (>10МБ) на HTTP/2 иногда обрываются на стороне сервера
        (ReadError/BrokenResource) — поэтому upload идёт отдельным httpx-клиентом
        на HTTP/1.1 с расширенным таймаутом."""
        from pathlib import Path as _Path
        import mimetypes

        p = _Path(path)
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        with p.open("rb") as fh:
            data = fh.read()
        files = {field_name: (p.name, data, mime)}

        headers = dict(PHYGITAL_HEADERS)
        headers["Authorization"] = f"Bearer {self.session.access_token}"

        async def _do() -> httpx.Response:
            async with httpx.AsyncClient(
                headers=headers,
                cookies=self.session.cookie_jar,
                timeout=httpx.Timeout(300.0, connect=30.0),
                http2=False,
                follow_redirects=False,
                verify=_SSL_CTX,
            ) as up:
                return await up.post(
                    f"{API_BASE}/api/v2/storage-object/storage-object", files=files
                )

        resp = await _do()
        if resp.status_code in (401, 418) and self.session_manager is not None:
            logger.info(f"{resp.status_code} on upload — refreshing session and retrying…")
            await self.session_manager.refresh(self.session)
            headers["Authorization"] = f"Bearer {self.session.access_token}"
            resp = await _do()
        resp.raise_for_status()
        body = resp.json()
        if "file_obj_id" not in body:
            raise RuntimeError(f"Unexpected upload response: {body}")
        return int(body["file_obj_id"])
