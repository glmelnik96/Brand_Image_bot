"""
Recon: live-capture одного сценария генерации в Phygital+.

Запуск:
    python -m recon.capture

Что делает:
1. Стартует Chromium с persistent context (./user_data/) — логин сохраняется между запусками.
2. Открывает app.phygital.plus.
3. Пишет HAR-файл (recon/captures/phygital-<ts>.har) со всеми XHR/Fetch + WS-фреймами.
4. Дополнительно логирует WebSocket в JSONL (recon/captures/ws-<ts>.jsonl) — HAR в Playwright
   не всегда сохраняет тела WS-сообщений, поэтому пишем параллельно.
5. По нажатию Enter в терминале — снимает дамп storage (cookies + localStorage + sessionStorage)
   в recon/captures/storage-<ts>.json и закрывает браузер.

Флоу для пользователя:
    1) Запусти скрипт.
    2) В открывшемся окне залогинься в Phygital+ (если ещё не залогинен).
    3) Сделай ОДНУ генерацию (image) от начала до конца — дождись результата.
    4) Вернись в терминал и нажми Enter.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright, WebSocket

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "recon" / "captures"
USER_DATA = ROOT / "user_data"
TARGET_URL = "https://app.phygital.plus/"


async def dump_storage(context, page, path: Path) -> None:
    """Снимаем cookies + localStorage + sessionStorage активной страницы."""
    cookies = await context.cookies()
    local_storage = await page.evaluate(
        "() => Object.fromEntries(Object.entries(localStorage))"
    )
    session_storage = await page.evaluate(
        "() => Object.fromEntries(Object.entries(sessionStorage))"
    )
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "url": page.url,
        "cookies": cookies,
        "localStorage": local_storage,
        "sessionStorage": session_storage,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Storage dumped → {path}")


def attach_ws_logger(ws: WebSocket, ws_log_path: Path) -> None:
    """Логируем все WebSocket-фреймы в JSONL (HAR не всегда их сохраняет полностью)."""
    logger.info(f"WS opened: {ws.url}")

    def write(direction: str, payload):
        try:
            data = payload if isinstance(payload, str) else f"<binary:{len(payload)}>"
        except Exception:
            data = "<unreadable>"
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "url": ws.url,
            "dir": direction,
            "payload": data,
        }
        with ws_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    ws.on("framesent", lambda p: write("send", p))
    ws.on("framereceived", lambda p: write("recv", p))
    ws.on("close", lambda: logger.info(f"WS closed: {ws.url}"))


async def stdin_wait() -> None:
    """Ждём Enter в stdin без блокировки event loop."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, sys.stdin.readline)


async def main() -> None:
    CAPTURES.mkdir(parents=True, exist_ok=True)
    USER_DATA.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    har_path = CAPTURES / f"phygital-{ts}.har"
    ws_log_path = CAPTURES / f"ws-{ts}.jsonl"
    storage_path = CAPTURES / f"storage-{ts}.json"

    logger.info(f"HAR  → {har_path}")
    logger.info(f"WS   → {ws_log_path}")
    logger.info(f"STOR → {storage_path}")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            headless=False,
            viewport={"width": 1440, "height": 900},
            record_har_path=str(har_path),
            record_har_content="embed",
            record_har_mode="full",
            args=["--disable-blink-features=AutomationControlled"],
        )

        # Перехватчик WS навешиваем на все страницы контекста.
        context.on("page", lambda p: p.on("websocket", lambda ws: attach_ws_logger(ws, ws_log_path)))

        page = context.pages[0] if context.pages else await context.new_page()
        page.on("websocket", lambda ws: attach_ws_logger(ws, ws_log_path))

        await page.goto(TARGET_URL, wait_until="domcontentloaded")

        print("\n" + "=" * 70)
        print(" Залогинься (если нужно) и сделай ОДНУ полную генерацию.")
        print(" Когда результат получен — вернись сюда и нажми Enter.")
        print("=" * 70 + "\n")

        await stdin_wait()

        # Берём актуальную активную страницу (юзер мог переключить вкладку).
        active = context.pages[-1] if context.pages else page
        try:
            await dump_storage(context, active, storage_path)
        except Exception as e:
            logger.error(f"Storage dump failed: {e}")

        await context.close()
        logger.success("Done. Captures saved.")


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    asyncio.run(main())
