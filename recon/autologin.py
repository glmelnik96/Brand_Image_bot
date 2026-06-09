"""
Авто-логин в Phygital по PHYGITAL_EMAIL / PHYGITAL_PASSWORD из .env.

Используется как фолбэк для recon/refresh_capture: если профиль user_data/
полностью разлогинен (cookies истекли, SPA не подхватывает refresh) —
этот скрипт открывает страницу /sign-in, заполняет email + password,
жмёт Continue, ждёт redirect и снимает storage-*.json в обычном формате
recon-дампа.

Логи никогда не пишут password; email маскируется.

Self-heal при сбое формы (на случай редизайна SuperTokens / anti-bot):
    - При любом провале заполнения снимается дамп состояния в
      recon/captures/autologin-fail-<ts>-<tag>/ (screenshot.png, dom.html,
      meta.json со списком видимых input/button) — чтобы причина была видна.
    - Перед поиском полей снимаются интерстициалы (cookie-overlay,
      экран выбора провайдера «Continue with email»).
    - Поля ищутся по лестнице селекторов, а не по одному.
    - Если форму не нашли в headless — один авто-ретрай в видимом окне.

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


# Лестницы селекторов: от точного к широкому. SuperTokens может отрендерить
# поля по-разному в зависимости от версии фронта, поэтому не полагаемся на один
# селектор, а перебираем по очереди с коротким таймаутом на каждый.
_EMAIL_SELECTORS = [
    'input[name="email"]',
    'input[type="email"]',
    'input[autocomplete="username"]',
    'input[placeholder*="mail" i]',
    'input[aria-label*="mail" i]',
    'input[type="text"]',  # широкий fallback — последним, чтобы не перехватить чужой инпут
]
_PASSWORD_SELECTORS = [
    'input[name="password"]',
    'input[type="password"]',
    'input[autocomplete="current-password"]',
    'input[placeholder*="pass" i]',
    'input[aria-label*="pass" i]',
]
_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("Sign in")',
    'button:has-text("Войти")',
    'button:has-text("Продолжить")',
]


async def _dump_failure(page, tag: str) -> Path:
    """Ярус 1: при сбое снимаем состояние страницы на диск — скриншот, DOM и
    список видимых input/button. Превращает «Timeout 15000ms» в полную картину
    того, что реально было на экране в момент провала."""
    d = CAPTURES / f"autologin-fail-{datetime.now():%Y%m%d-%H%M%S}-{tag}"
    try:
        d.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(d / "screenshot.png"), full_page=True)
        (d / "dom.html").write_text(await page.content(), encoding="utf-8")
        inputs = await page.eval_on_selector_all(
            "input",
            "els => els.map(e => ({name:e.name, type:e.type, "
            "placeholder:e.placeholder, aria:e.getAttribute('aria-label'), "
            "visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}))",
        )
        buttons = await page.eval_on_selector_all(
            "button",
            "els => els.map(e => ({text:(e.innerText||'').trim().slice(0,40), "
            "type:e.type, disabled:e.disabled, "
            "visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}))",
        )
        (d / "meta.json").write_text(
            json.dumps(
                {"url": page.url, "tag": tag, "inputs": inputs, "buttons": buttons},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        logger.warning(f"auto-login: состояние сбоя сохранено → {d}")
    except Exception as e:
        logger.warning(f"auto-login: не смог снять дамп сбоя ({tag}): {e}")
    return d


async def _clear_interstitials(page) -> None:
    """Ярус 2: снимаем то, что предшествует/перекрывает форму — cookie-overlay и
    экран выбора провайдера (SuperTokens мог добавить «Continue with email», и
    тогда поле email появляется только после клика по нему)."""
    # cookie-consent overlay перекрывает поле → state="visible" не выполнится
    for sel in (
        'button:has-text("Accept")',
        'button:has-text("Принять")',
        'button:has-text("Allow all")',
        '[aria-label*="cookie" i] button',
    ):
        try:
            b = page.locator(sel).first
            if await b.count() and await b.is_visible():
                await b.click(timeout=2000)
                logger.info(f"auto-login: закрыл cookie-overlay ({sel!r})")
                break
        except Exception:
            continue
    # экран выбора провайдера: email-форма за кнопкой «Continue with email»
    for sel in (
        'button:has-text("Continue with email")',
        'button:has-text("Sign in with email")',
        'button:has-text("Войти по почте")',
        'a:has-text("email")',
    ):
        try:
            b = page.locator(sel).first
            if await b.count() and await b.is_visible():
                logger.info(f"auto-login: provider-chooser, жму {sel!r}")
                await b.click(timeout=3000)
                await asyncio.sleep(0.5)
                break
        except Exception:
            continue


async def _find_and_fill(page, selectors, value, field: str) -> bool:
    """Ярус 3: перебираем лестницу селекторов с коротким таймаутом на каждый,
    вместо одного длинного ожидания узкого селектора."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=3000)
            await loc.fill(value)
            logger.info(f"auto-login: поле {field} найдено селектором {sel!r}")
            return True
        except Exception:
            continue
    logger.warning(
        f"auto-login: поле {field} не найдено ни по одному из "
        f"{len(selectors)} селекторов"
    )
    return False


async def _fill_login_form(page, email: str, password: str) -> bool:
    """Заполняет email+password, жмёт Continue. Возвращает True, если submit нажат.
    При любом сбое снимает диагностический дамп (_dump_failure)."""
    await _clear_interstitials(page)

    if not await _find_and_fill(page, _EMAIL_SELECTORS, email, "email"):
        await _dump_failure(page, "no-email")
        return False
    if not await _find_and_fill(page, _PASSWORD_SELECTORS, password, "password"):
        await _dump_failure(page, "no-password")
        return False

    # Continue. SuperTokens обычно даёт button[type=submit] с текстом Continue / Sign In.
    # Дадим маленькую паузу — клиентская валидация (8 chars / 1 letter / 1 number)
    # должна успеть снять disabled с кнопки.
    await asyncio.sleep(0.3)
    submit = None
    for sel in _SUBMIT_SELECTORS:
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
        await _dump_failure(page, "no-submit")
        return False
    try:
        await submit.click(timeout=5000)
    except Exception as e:
        logger.warning(f"auto-login: клик по submit упал: {e}")
        await _dump_failure(page, "submit-click")
        return False
    return True


async def _attempt(
    pw, headless: bool, email: str, password: str,
    settle_seconds: float, storage_path: Path,
) -> int:
    """Одна попытка логина в своём persistent-контексте. Контекст всегда
    закрывается в finally, чтобы можно было безопасно перезапустить попытку с
    другим headless (ярус 4) на том же user_data-каталоге."""
    logger.info(
        f"auto-login: запускаю как {_mask_email(email)} (headless={headless})"
    )
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA),
        headless=headless,
        viewport={"width": 1280, "height": 800},
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        page = context.pages[0] if context.pages else await context.new_page()

        # 1) Заходим на /sign-in напрямую — даже если профиль ещё авторизован,
        #    SPA редиректнет нас на главную и cookies подтянутся.
        try:
            await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            logger.error(f"auto-login: navigation to /sign-in failed: {e}")
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
                await _dump_failure(page, "no-token-after-submit")
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
        return 0
    finally:
        await context.close()


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

    async with async_playwright() as pw:
        rc = await _attempt(pw, headless, email, password, settle_seconds, storage_path)
        # Ярус 4: форму не нашли в headless (rc=2) — один ретрай в видимом окне.
        # Часть anti-automation/bot-challenge срабатывает только в headless,
        # в headed-окне форма рендерится нормально. На rc=3 (навигация) и rc=0
        # не эскалируем.
        if rc == 2 and headless:
            logger.warning(
                "auto-login: headless-попытка не удалась — повторяю с видимым окном"
            )
            rc = await _attempt(pw, False, email, password, settle_seconds, storage_path)
        return rc


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    headless = "--show" not in sys.argv
    sys.exit(asyncio.run(main(headless=headless)))
