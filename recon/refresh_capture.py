"""
Не-интерактивный refresh сессии через персистентный браузерный профиль.

Если ./user_data/ всё ещё авторизован, SPA сама дёрнет refresh при загрузке —
мы дожидаемся networkidle и снимаем storage в новый recon-дамп.

Запуск:
    .venv/bin/python -m recon.refresh_capture
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "recon" / "captures"
USER_DATA = ROOT / "user_data"
TARGET_URL = "https://app.phygital.plus/"


async def main(headless: bool = True, settle_seconds: float = 8.0) -> int:
    CAPTURES.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    storage_path = CAPTURES / f"storage-{ts}.json"

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            headless=headless,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        logger.info(f"Opening {TARGET_URL} (headless={headless})…")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.warning(f"navigation: {e}")

        # Дать SPA время дёрнуть /auth/session/refresh и подтянуть workspace
        try:
            await page.wait_for_load_state("networkidle", timeout=int(settle_seconds * 1000))
        except Exception:
            pass
        await asyncio.sleep(settle_seconds)

        cookies = await context.cookies()
        access = next((c for c in cookies if c["name"] == "st-access-token"), None)
        refresh = next((c for c in cookies if c["name"] == "st-refresh-token"), None)
        if not access:
            logger.error("No st-access-token in cookies — профиль разлогинен, нужен ручной capture.")
            await context.close()
            return 2

        try:
            ls = await page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
            ss = await page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage))")
        except Exception:
            ls = ss = {}

        payload = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "url": page.url,
            "cookies": cookies,
            "localStorage": ls,
            "sessionStorage": ss,
        }
        storage_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.success(f"Storage dumped → {storage_path}")
        logger.info(f"access_token len={len(access['value'])}  refresh_token len={len(refresh['value']) if refresh else 0}")
        await context.close()
        return 0


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    headless = "--show" not in sys.argv
    sys.exit(asyncio.run(main(headless=headless)))
