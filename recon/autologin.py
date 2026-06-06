"""
Авто-логин в Phygital по PHYGITAL_EMAIL / PHYGITAL_PASSWORD из .env.

Используется как фолбэк для recon/refresh_capture: если профиль user_data/
полностью разлогинен (cookies истекли, SPA не подхватывает refresh) —
этот скрипт открывает страницу /sign-in, заполняет email + password,
жмёт Continue, ждёт redirect и снимает storage-*.json в обычном формате
recon-дампа.

Логи никогда не пишут password; email маскируется.

Запуск (только если креды лежат в .env):
    python -m recon.autologin            # headless
    python -m recon.autologin --show     # с видимым окном (для отладки)

Return codes:
    0 — успех, дамп снят.
    2 — креды не заданы / форма не нашлась / submit не подхватил cookies.
    3 — навигация на /sign-in упала.
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
APP_URL = "https://app.phygital.plus/"
SIGNIN_URL = "https://app.phygital.plus/sign-in"


def _mask_email(email: str) -> str:
    if "@" not in email:
        return email[:2] + "***"
    local, domain = email.split("@", 1)
    head = local[:2] if len(local) >= 2 else local
    return f"{head}***@{domain}"


async def _fill_login_form(page, email: str, password: str) -> bool:
    """Заполняет email+password, жмёт Continue. Возвращает True, если submit нажат."""
    # Email — SuperTokens рендерит как обычный input[type=email] или name=email.
    try:
        email_in = page.locator(
            'input[name="email"], input[type="email"]'
        ).first
        await email_in.wait_for(state="visible", timeout=15000)
        await email_in.fill(email)
    except Exception as e:
        logger.warning(f"auto-login: не нашёл/не заполнил поле email: {e}")
        return False
    # Password.
    try:
        pass_in = page.locator(
            'input[name="password"], input[type="password"]'
        ).first
        await pass_in.wait_for(state="visible", timeout=5000)
        await pass_in.fill(password)
    except Exception as e:
        logger.warning(f"auto-login: не нашёл/не заполнил поле password: {e}")
        return False
    # Continue. SuperTokens обычно даёт button[type=submit] с текстом Continue / Sign In.
    # Дадим маленькую паузу — клиентская валидация (8 chars / 1 letter / 1 number)
    # должна успеть снять disabled с кнопки.
    await asyncio.sleep(0.3)
    submit = None
    candidates = [
        'button[type="submit"]',
        'button:has-text("Continue")',
        'button:has-text("Sign in")',
        'button:has-text("Войти")',
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                submit = loc
                logger.info(f"auto-login: submit найден селектором {sel!r}")
                break
        except Exception:
            continue
    if submit is None:
        logger.warning("auto-login: submit-кнопка не найдена ни по одному селектору")
        return False
    try:
        await submit.click(timeout=5000)
    except Exception as e:
        logger.warning(f"auto-login: клик по submit упал: {e}")
        return False
    return True


async def main(headless: bool = True, settle_seconds: float = 10.0) -> int:
    # Импортируем settings лениво — модуль может вызываться отдельно от бота.
    try:
        from client.config import settings
    except Exception as e:
        logger.error(f"auto-login: не смог загрузить settings: {e}")
        return 2

    email = (settings.phygital_email or "").strip()
    password = (settings.phygital_password or "").strip()
    if not email or not password:
        logger.warning(
            "auto-login: PHYGITAL_EMAIL / PHYGITAL_PASSWORD пусты в .env — "
            "автологин пропущен. Заполни их и попробуй ещё раз."
        )
        return 2

    CAPTURES.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    storage_path = CAPTURES / f"storage-{ts}.json"

    logger.info(f"auto-login: запускаю как {_mask_email(email)} (headless={headless})")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            headless=headless,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # 1) Заходим на /sign-in напрямую — даже если профиль ещё авторизован,
        #    SPA редиректнет нас на главную и cookies подтянутся.
        try:
            await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.error(f"auto-login: navigation to /sign-in failed: {e}")
            await context.close()
            return 3

        # SPA может редиректнуть прямо на главную, если профиль ещё жив.
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        await asyncio.sleep(1.0)

        cookies = await context.cookies()
        access = next((c for c in cookies if c["name"] == "st-access-token"), None)
        if access:
            logger.info("auto-login: профиль уже авторизован, форму не заполняю")
        else:
            # 2) Если до сих пор на /sign-in — пытаемся заполнить.
            if "/sign-in" not in page.url:
                # Редирект не на форму — что-то странное; пробуем перейти ещё раз.
                try:
                    await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass

            ok = await _fill_login_form(page, email, password)
            if not ok:
                await context.close()
                return 2

            # 3) Ждём, пока submit разрулится: либо SPA редиректнет на главную,
            #    либо cookies проставятся прямо на /sign-in.
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            await asyncio.sleep(settle_seconds)

            cookies = await context.cookies()
            access = next((c for c in cookies if c["name"] == "st-access-token"), None)
            if not access:
                logger.error(
                    "auto-login: submit нажат, но st-access-token не появился. "
                    f"Текущий URL: {page.url}. Возможно, неверные креды или Phygital "
                    "вернул ошибку — открой /sign-in вручную и проверь."
                )
                await context.close()
                return 2

        # 4) Дальше — стандартный recon-дамп, как в refresh_capture.
        refresh = next((c for c in cookies if c["name"] == "st-refresh-token"), None)
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
        storage_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.success(f"auto-login: storage dumped → {storage_path}")
        logger.info(
            f"auto-login: access_token len={len(access['value'])} "
            f"refresh_token len={len(refresh['value']) if refresh else 0}"
        )
        await context.close()
        return 0


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    headless = "--show" not in sys.argv
    sys.exit(asyncio.run(main(headless=headless)))
