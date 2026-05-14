"""
SuperTokens session refresh (header-mode).

Phygital+ использует SuperTokens с auth-mode "header":
  - Access token хранится в cookie `st-access-token` (НЕ HttpOnly), фронт
    подставляет его в Authorization-заголовок защищённых API-вызовов.
  - Refresh token хранится в cookie `st-refresh-token`.
  - При истечении access-token фронт вызывает:
        POST https://app.phygital.plus/auth/session/refresh
        Authorization: Bearer <refresh_token>
        rid: session
        st-auth-mode: header
        anti-csrf: <если выдан фронту в front-token>
  - В ответ приходят новые токены — в response headers (st-access-token,
    st-refresh-token, front-token) и одновременно как Set-Cookie.

Док: https://supertokens.com/docs/contribute/sdk/frontend-driver-interface
"""

from __future__ import annotations

import base64
import json
import ssl
from typing import Any

import httpx
import truststore
from loguru import logger

from client.session import (
    ACCESS_COOKIE,
    FRONT_TOKEN_COOKIE,
    LAST_UPDATE_COOKIE,
    REFRESH_COOKIE,
    Session,
)

AUTH_REFRESH_URL = "https://app-server-azure.phygital.plus/auth/session/refresh"

_SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class RefreshError(Exception):
    """Refresh не удался — обычно refresh-token истёк или отозван."""


def _decode_front_token(token: str | None) -> dict[str, Any] | None:
    """sFrontToken — base64(JSON) с полями ate (access token expiry ms), uid, up.
    Используется фронтом, чтобы заранее знать, когда дёргать refresh."""
    if not token:
        return None
    try:
        # SuperTokens передаёт уже без padding — добавим
        padding = "=" * (-len(token) % 4)
        return json.loads(base64.b64decode(token + padding))
    except Exception:
        return None


def _set_cookie(session: Session, name: str, value: str, *, domain: str = "app.phygital.plus") -> None:
    for c in session.cookies:
        if c.get("name") == name:
            c["value"] = value
            return
    session.cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})


async def refresh_session(session: Session) -> Session:
    """Делает один refresh-запрос и обновляет cookies в session (in-place + return)."""
    refresh = session.refresh_token
    if not refresh:
        raise RefreshError(f"No {REFRESH_COOKIE} in session — cannot refresh")

    # Anti-CSRF: достаётся из front-token (поле 'antiCsrf'), но в header-mode
    # обычно не требуется. Попробуем добавить если есть.
    front = _decode_front_token(session.cookie_value(FRONT_TOKEN_COOKIE))
    anti_csrf = (front or {}).get("antiCsrfToken")

    headers = {
        "Authorization": f"Bearer {refresh}",
        "rid": "session",
        "st-auth-mode": "header",
        "Origin": "https://app.phygital.plus",
        "Referer": "https://app.phygital.plus/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }
    if anti_csrf:
        headers["anti-csrf"] = anti_csrf

    # Шлём cookies, чтобы LB-sticky тоже сработал
    cookies = session.cookie_jar

    async with httpx.AsyncClient(timeout=30, verify=_SSL_CTX, follow_redirects=False) as c:
        resp = await c.post(AUTH_REFRESH_URL, headers=headers, cookies=cookies, json={})

    logger.debug(f"refresh status={resp.status_code}")
    if resp.status_code in (401, 403, 418):
        # 418 у SuperTokens — try refresh ещё раз (token theft) или unauthorised
        raise RefreshError(f"Refresh rejected ({resp.status_code}): {resp.text[:200]}")
    if resp.status_code >= 400:
        raise RefreshError(f"Refresh failed ({resp.status_code}): {resp.text[:200]}")

    # Новые токены приходят в response headers (приоритет) ИЛИ в Set-Cookie
    new_access = resp.headers.get("st-access-token") or resp.cookies.get(ACCESS_COOKIE)
    new_refresh = resp.headers.get("st-refresh-token") or resp.cookies.get(REFRESH_COOKIE)
    new_front = resp.headers.get("front-token") or resp.cookies.get(FRONT_TOKEN_COOKIE)

    if not new_access:
        raise RefreshError("Refresh OK but no new access token in response")

    _set_cookie(session, ACCESS_COOKIE, new_access)
    if new_refresh:
        _set_cookie(session, REFRESH_COOKIE, new_refresh)
    if new_front:
        _set_cookie(session, FRONT_TOKEN_COOKIE, new_front)

    # Обновим st-last-access-token-update (фронт это делает) — миллисекунды unix
    import time
    _set_cookie(session, LAST_UPDATE_COOKIE, str(int(time.time() * 1000)))

    front_decoded = _decode_front_token(new_front)
    expiry = front_decoded.get("ate") if front_decoded else None
    logger.info(f"Refresh OK: new access_token len={len(new_access)} expires_at_ms={expiry}")
    return session
