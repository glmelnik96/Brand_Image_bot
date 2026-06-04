"""
Phygital+ использует SuperTokens с auth-mode "header":
  - access_token   лежит в cookie `st-access-token` (НЕ HttpOnly) и подставляется
    фронтом в заголовок `Authorization: Bearer <jwt>`
  - refresh_token  лежит в cookie `st-refresh-token`
  - sticky cookie  `STICKY_SESSION_*` (HttpOnly) — для LB-роутинга
  - служебные:     `sFrontToken`, `st-last-access-token-update`

Refresh flow:
  POST https://app-server-azure.phygital.plus/auth/session/refresh
  headers: Authorization: Bearer <refresh_token>, rid: session, st-auth-mode: header
  В ответ — новые st-access-token / st-refresh-token / front-token (response headers).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

# Имена SuperTokens-cookies, которые нам нужны
ACCESS_COOKIE = "st-access-token"
REFRESH_COOKIE = "st-refresh-token"
FRONT_TOKEN_COOKIE = "sFrontToken"
LAST_UPDATE_COOKIE = "st-last-access-token-update"


@dataclass
class Session:
    """Снимок состояния авторизации."""
    cookies: list[dict[str, Any]] = field(default_factory=list)
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ── derived ────────────────────────────────────────────────────────────
    def cookie_value(self, name: str) -> str | None:
        for c in self.cookies:
            if c.get("name") == name:
                return c.get("value")
        return None

    @property
    def access_token(self) -> str | None:
        return self.cookie_value(ACCESS_COOKIE)

    @property
    def refresh_token(self) -> str | None:
        return self.cookie_value(REFRESH_COOKIE)

    def jwt_ttl_seconds(self) -> int | None:
        """Сколько секунд осталось у access-JWT (None — токена нет / битый)."""
        tok = self.access_token
        if not tok or tok.count(".") < 2:
            return None
        try:
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            exp = int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
        except Exception:
            return None
        if not exp:
            return None
        return exp - int(time.time())

    @property
    def cookie_jar(self) -> dict[str, str]:
        """Только те cookies, что реально нужны бэку (минимизируем поверхность)."""
        keep = {
            "STICKY_SESSION_",  # любой sticky-LB cookie начинается с этого префикса
            ACCESS_COOKIE,
            REFRESH_COOKIE,
            FRONT_TOKEN_COOKIE,
            LAST_UPDATE_COOKIE,
        }
        out: dict[str, str] = {}
        for c in self.cookies:
            n = c.get("name", "")
            if n in keep or any(n.startswith(p) for p in keep if p.endswith("_")):
                out[n] = c.get("value", "")
        return out


class SessionManager:
    """Загружает/сохраняет/импортирует/обновляет сессию."""

    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self._refresh_lock = asyncio.Lock()

    def load(self) -> Session | None:
        if not self.storage_path.exists():
            logger.warning(f"Session file not found: {self.storage_path}")
            return None
        data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        session = Session(cookies=data.get("cookies", []))
        # Восстанавливаем captured_at — иначе дефолт-фабрика поставит datetime.now(),
        # и _find_fresher_recon_dump никогда не найдёт «свежий» дамп (mtime ≤ now).
        cap_raw = data.get("captured_at")
        if isinstance(cap_raw, str):
            try:
                session.captured_at = datetime.fromisoformat(cap_raw)
            except ValueError:
                logger.warning(f"captured_at unparseable ({cap_raw!r}); fallback to file mtime")
                session.captured_at = datetime.fromtimestamp(
                    self.storage_path.stat().st_mtime, tz=timezone.utc,
                )
        else:
            session.captured_at = datetime.fromtimestamp(
                self.storage_path.stat().st_mtime, tz=timezone.utc,
            )
        if not session.access_token:
            logger.warning("Session loaded, but st-access-token missing")
        return session

    def save(self, session: Session) -> None:
        # captured_at теперь = "когда мы в последний раз персистили эту сессию".
        # После refresh — обновляется, чтобы по полю всегда было видно свежесть.
        session.captured_at = datetime.now(timezone.utc)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookies": session.cookies,
            "captured_at": session.captured_at.isoformat(),
        }
        self.storage_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Session saved → {self.storage_path}")

    # ── import from recon dump ────────────────────────────────────────────
    @staticmethod
    def from_recon_dump(dump_path: Path) -> Session:
        data = json.loads(dump_path.read_text(encoding="utf-8"))
        session = Session(cookies=data.get("cookies", []))
        if session.access_token:
            logger.info(f"Imported session. access_token len={len(session.access_token)}")
        else:
            logger.error(f"No {ACCESS_COOKIE} in dump {dump_path}")
        return session

    # ── refresh + persist ─────────────────────────────────────────────────
    async def refresh(self, session: Session) -> Session:
        """Обновляет access/refresh токены через SuperTokens и сохраняет на диск.

        Защищено локом — параллельные вызовы делают один refresh.

        Failover: если сервер вернул RefreshError (refresh-token инвалидирован,
        обычно из-за параллельной ротации в браузере) — ищем более свежий
        recon-дамп в `recon/captures/storage-*.json`, у которого access-JWT
        ещё живой, и переключаемся на него вместо мёртвой сессии.
        """
        from client.auth import RefreshError, refresh_session  # late import

        async with self._refresh_lock:
            try:
                await refresh_session(session)
            except RefreshError as e:
                logger.warning(f"refresh rejected: {e} — пробую fallback к recon-дампу")
                fresh = self._find_fresher_recon_dump(session)
                if fresh is None:
                    logger.error("Свежих recon-дампов с живым JWT нет — поднимаю ошибку")
                    raise
                logger.info(f"Fallback: подгружаю сессию из {fresh.name}")
                self._swap_session_inplace(session, fresh)
                # JWT в дампе валиден (мы это проверили в _find_fresher_recon_dump),
                # поэтому refresh здесь не вызываем — текущий запрос пройдёт.
            self.save(session)
            return session

    @staticmethod
    def _find_fresher_recon_dump(session: Session) -> Path | None:
        """Возвращает самый свежий `storage-*.json` с непросроченным access-JWT,
        более новый, чем текущая сессия. Иначе None."""
        captures = Path(__file__).resolve().parent.parent / "recon" / "captures"
        if not captures.exists():
            return None
        cur_ts = session.captured_at.timestamp() if session.captured_at else 0.0
        best: Path | None = None
        best_mtime = 0.0
        for f in captures.glob("storage-*.json"):
            try:
                mtime = f.stat().st_mtime
                if mtime <= cur_ts:
                    continue
                d = json.loads(f.read_text(encoding="utf-8"))
                cookies = d.get("cookies", [])
                tmp = Session(cookies=cookies)
                ttl = tmp.jwt_ttl_seconds()
                # Запас 30 сек, чтобы не подхватывать дамп ровно на грани.
                if ttl is None or ttl < 30:
                    continue
                if mtime > best_mtime:
                    best = f
                    best_mtime = mtime
            except Exception as e:
                logger.debug(f"skip {f.name}: {e}")
                continue
        return best

    @staticmethod
    def _swap_session_inplace(session: Session, dump_path: Path) -> None:
        """Перезаписывает cookies в существующем session-объекте из recon-дампа."""
        d = json.loads(dump_path.read_text(encoding="utf-8"))
        session.cookies = d.get("cookies", [])
