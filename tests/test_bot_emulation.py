"""Эмуляционный ран бота без реальных сетевых вызовов.

Цель: проверить новые фичи end-to-end на уровне хендлеров и `BotState`:
  1) menu-callback `menu:generate` поднимает /generate-сценарий и переводит в GEN_PROMPT;
  2) /generate-флоу (Nano Banana v3.1) проходит prompt → ratio → resolution
     (node-picker и model-picker сняты — модель зашита);
  3) worker реально запускает `_execute_task`, скачивает «картинку», сохраняет recipe;
  4) 🔄 Повторить (regen_cb) — повторно ставит задачу с теми же параметрами;
  5) ✏️ Уточнить (edit_prompt_cb) — ставит pending_edit; следующий текст идёт через
     `pending_edit_listener` и стартует новую задачу с новым промптом;
  6) 🖼 Как img2img (as_i2i_cb) — заходит в img2img-конверсейшн с готовой картинкой как init;
  7) inline `cancel:picker` callback — завершает конверсейшн;
  8) entry-point capacity check — на 5+ задаче возвращает END и шлёт отбивку;
  9) expired recipe — regen/edit/asi2i отвечают «параметры устарели».

Мокаем:
  - `cli.load_session`              → MagicMock (BotState.__init__);
  - `client.api.PhygitalClient`     → async-CM возвращающий MagicMock-клиент;
  - workflow.run / .run_with_files  → AsyncMock возвращающий GenerationJob;
  - `httpx.AsyncClient`             → CM с .get() возвращающий байты PNG-«магического числа».
  - `settings.allowed_user_ids`     → пусто, `_is_allowed` пропустит любого uid.

Запуск (после активации venv): `python -m tests.test_bot_emulation`
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# Под Windows stdout/stderr по умолчанию cp1251 — выводим Unicode-баннеры/эмодзи
# в utf-8 явно, иначе UnicodeEncodeError на первом же ━.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

# Один минимальный PNG (8-байтная сигнатура + IHDR), на отправку этого Telegram-mock плевать.
FAKE_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"  # signature
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cb\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ── Update / Context shims ─────────────────────────────────────────────────
def make_user(uid: int = 42, username: str = "tester"):
    u = MagicMock(spec=[])
    u.id = uid
    u.username = username
    u.full_name = "Test User"
    return u


def make_chat(chat_id: int = 42):
    c = MagicMock(spec=[])
    c.id = chat_id
    # `send_message` возвращает «статус-сообщение» с awaitable .edit_text
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    c.send_message = AsyncMock(return_value=status_msg)
    c.send_photo = AsyncMock()
    c.send_document = AsyncMock()
    return c


def make_message_update(text: str | None, user, chat, photo=None, document=None):
    """Update с update.message — для CommandHandler / MessageHandler."""
    msg = MagicMock(spec=[])
    msg.text = text
    msg.photo = photo or []
    msg.document = document
    msg.chat = chat
    msg.reply_text = AsyncMock()
    upd = MagicMock(spec=[])
    upd.message = msg
    upd.callback_query = None
    upd.effective_user = user
    upd.effective_chat = chat
    upd.effective_message = msg
    return upd


def make_callback_update(callback_data: str, user, chat):
    """Update с callback_query — для CallbackQueryHandler."""
    msg = MagicMock(spec=[])
    msg.text = None
    msg.photo = []
    msg.document = None
    msg.chat = chat
    msg.reply_text = AsyncMock()
    q = MagicMock(spec=[])
    q.data = callback_data
    q.message = msg
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    upd = MagicMock(spec=[])
    upd.message = None
    upd.callback_query = q
    upd.effective_user = user
    upd.effective_chat = chat
    upd.effective_message = msg
    return upd


def make_context(args: list[str] | None = None):
    ctx = MagicMock(spec=[])
    ctx.args = args or []
    ctx.user_data = {}
    ctx.bot = MagicMock()
    return ctx


# ── общий setUp: мокаем PhygitalClient + workflow и httpx ──────────────────
class FakeJob:
    """Не используем настоящий GenerationJob — он pydantic, есть жёсткая валидация полей.
    А scenarios достаёт только status / result_urls / job_id / error."""
    def __init__(self, status="completed", urls=("http://fake.example/result.png",), job_id="42"):
        self.status = status
        self.result_urls = list(urls)
        self.job_id = job_id
        self.error = None


def make_fake_client_cm():
    """Возвращает async context manager, имитирующий `async with PhygitalClient(...) as c`."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock())
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def make_workflow_constructor(captured: dict):
    """Возвращает функцию, которая на каждом вызове конструктора workflow:
      - сохраняет переданные kwargs в `captured["calls"]` для проверки;
      - возвращает объект с .run / .run_with_files, возвращающими FakeJob.
    """

    def ctor(*args, **kwargs):
        wf = MagicMock()
        wf.run = AsyncMock(return_value=FakeJob())
        wf.run_with_files = AsyncMock(return_value=FakeJob())
        captured.setdefault("calls", []).append({"args": args, "kwargs": kwargs})
        return wf

    return ctor


class FakeAsyncHttpx:
    """Async-CM имитирующий `httpx.AsyncClient(...)` с .get(url) → fake PNG-bytes."""
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        r = MagicMock()
        r.content = FAKE_PNG_BYTES
        r.raise_for_status = MagicMock()
        return r


# ── вспомогательное: подождать пока worker отработает задачу ───────────────
async def drain_user_queue(state, uid: int, timeout: float = 5.0):
    """Дожидается, пока user_inflight[uid] упадёт до нуля И в очереди пусто.
    После этого можно проверять recipe / send_photo calls."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        inflight, queued = state.user_load(uid)
        if inflight == 0 and queued == 0:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"queue not drained in {timeout}s: inflight={state.user_inflight.get(uid, 0)} "
        f"queued={state.user_queues.get(uid).qsize() if state.user_queues.get(uid) else 0}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# СЦЕНАРИИ
# ═══════════════════════════════════════════════════════════════════════════

async def scenario_full_generate_with_post_actions():
    """Полный t2i-сценарий + проверка всех трёх пост-задачных кнопок + cancel + capacity check."""
    from bot import scenarios as scn
    from bot.state import RECIPE_TTL_SEC, get_state, reset_state_for_tests

    reset_state_for_tests()
    state = get_state()

    user = make_user(42)
    chat = make_chat(42)
    ctx = make_context()

    # === 1. Меню → /generate (через menu-callback) ===
    upd = make_callback_update("menu:generate", user, chat)
    state_id = await scn.gen_start(upd, ctx)
    assert state_id == scn.GEN_PROMPT, f"menu:generate должен войти в GEN_PROMPT, got {state_id}"
    upd.callback_query.answer.assert_awaited()  # callback подтверждён
    print("  ✓ menu:generate → GEN_PROMPT")

    # === 2. prompt → сразу GEN_RATIO (нода Nano Banana и модель v3.1 зашиты) ===
    upd = make_message_update("a happy banana on a beach", user, chat)
    state_id = await scn.gen_prompt(upd, ctx)
    assert state_id == scn.GEN_RATIO
    assert ctx.user_data["prompt"] == "a happy banana on a beach"
    print("  ✓ prompt → GEN_RATIO (node/model pickers сняты)")

    # === 5. ratio = r_1_1 ===
    upd = make_callback_update("ratio:r_1_1", user, chat)
    state_id = await scn.gen_ratio(upd, ctx)
    assert state_id == scn.GEN_RES
    print("  ✓ ratio:r_1_1 → GEN_RES")

    # === 6. res = k2 → enqueue ===
    captured: dict = {}
    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.ImageGenWorkflow", side_effect=make_workflow_constructor(captured)), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        upd = make_callback_update("res:k2", user, chat)
        state_id = await scn.gen_res(upd, ctx)
        assert state_id == -1  # ConversationHandler.END
        print("  ✓ res:k2 → END (task enqueued)")

        # Подождать пока worker отработает.
        await drain_user_queue(state, 42)

    # Verify: workflow constructor вызван с правильными параметрами
    assert len(captured["calls"]) == 1
    wf_kwargs = captured["calls"][0]["kwargs"]
    assert wf_kwargs.get("model_name") == "v3_1"
    assert wf_kwargs.get("ratio") == "r_1_1"
    assert wf_kwargs.get("resolution") == "k2"
    print("  ✓ ImageGenWorkflow получил model=v3_1 ratio=r_1_1 resolution=k2")

    # Verify: send_photo вызван (картинка отправлена) — был upload-fallback (нужен kb)
    assert chat.send_photo.await_count >= 1, "result image must be sent"
    last_call = chat.send_photo.await_args
    assert "reply_markup" in last_call.kwargs, "action keyboard must be attached"
    kb = last_call.kwargs["reply_markup"]
    btns = [b for row in kb.inline_keyboard for b in row]
    assert any("Повторить" in b.text for b in btns)
    assert any("Изменить текст" in b.text for b in btns)
    assert any("Изменить изображение" == b.text for b in btns)
    assert any("Добавить Brand patterns" == b.text for b in btns)
    print("  ✓ result отправлен с inline-клавиатурой [Повторить/Изменить текст/Изменить изображение/Добавить Brand patterns]")

    # Verify: recipe сохранён
    recipes = list(state.recipes.values())
    assert len(recipes) == 1, f"expected 1 recipe, got {len(recipes)}"
    recipe = recipes[0]
    assert recipe.workflow == "nb_t2i"
    assert recipe.prompt == "a happy banana on a beach"
    assert recipe.params == {"model": "v3_1", "ratio": "r_1_1", "resolution": "k2"}
    assert recipe.result_path is not None and recipe.result_path.exists()
    print(f"  ✓ recipe сохранён: task_uid={recipe.task_uid[:8]}, result={recipe.result_path.name}")

    # === 7. 🔄 Повторить ===
    captured.clear()
    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.ImageGenWorkflow", side_effect=make_workflow_constructor(captured)), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        upd = make_callback_update(f"regen:{recipe.task_uid}", user, chat)
        await scn.regen_cb(upd, ctx)
        await drain_user_queue(state, 42)
    # Workflow вызван ещё раз с теми же params
    assert captured["calls"], "regen must call workflow constructor"
    re_kwargs = captured["calls"][0]["kwargs"]
    assert re_kwargs.get("model_name") == "v3_1"
    assert re_kwargs.get("ratio") == "r_1_1"
    assert re_kwargs.get("resolution") == "k2"
    print("  ✓ regen: те же параметры, workflow перезапущен")

    # Теперь должно быть 2 recipe (оригинал + regen)
    assert len(state.recipes) == 2
    regen_recipe = max(state.recipes.values(), key=lambda r: r.created_at)
    assert " (regen)" in regen_recipe.label, f"regen label expected, got: {regen_recipe.label}"
    print("  ✓ recipe из regen сохранён с меткой (regen)")

    # === 8. ✏️ Уточнить ===
    upd = make_callback_update(f"editp:{recipe.task_uid}", user, chat)
    await scn.edit_prompt_cb(upd, ctx)
    assert state.has_pending_edit(42), "pending_edit должно быть установлено"
    # reply_text был с HTML код-блоком исходного промпта
    sent_msg = upd.callback_query.message.reply_text.await_args.args[0]
    assert "<pre>" in sent_msg
    assert "a happy banana on a beach" in sent_msg
    print("  ✓ edit-prompt: pending_edit установлен, исходный промпт показан в <pre>")

    # Юзер шлёт новый промпт обычным сообщением → pending_edit_listener подхватывает
    captured.clear()
    new_prompt = "a sad cucumber under rain"
    upd = make_message_update(new_prompt, user, chat)
    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.ImageGenWorkflow", side_effect=make_workflow_constructor(captured)), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        await scn.pending_edit_listener(upd, ctx)
        await drain_user_queue(state, 42)
    assert not state.has_pending_edit(42), "pending_edit должен быть очищен после использования"
    edit_recipe = max(state.recipes.values(), key=lambda r: r.created_at)
    assert edit_recipe.prompt == new_prompt, f"edit recipe prompt expected new, got: {edit_recipe.prompt}"
    assert " (edit)" in edit_recipe.label
    print(f"  ✓ edit-prompt: новый промпт {new_prompt!r} ушёл в workflow с меткой (edit)")

    # === 9. 🖼 Как img2img ===
    # asi2i на t2i-результат: должен зайти в img2img-конверсейшн с result_path как init
    upd = make_callback_update(f"asi2i:{recipe.task_uid}", user, chat)
    state_id = await scn.as_i2i_cb(upd, ctx)
    assert state_id == scn.I2I_COLLECT, f"asi2i must return I2I_COLLECT, got {state_id}"
    assert "init_paths" in ctx.user_data and len(ctx.user_data["init_paths"]) == 1
    init = ctx.user_data["init_paths"][0]
    assert init.exists()
    print(f"  ✓ asi2i: вошли в I2I_COLLECT, init предзагружен: {init.name}")

    # === 9b. 🖼 Как brand img2img ===
    # asbi2i на t2i-результат: должен зайти в brand_img2img-конверсейшн (BI2I_COLLECT)
    ctx.user_data.clear()
    upd = make_callback_update(f"asbi2i:{recipe.task_uid}", user, chat)
    state_id = await scn.as_brand_i2i_cb(upd, ctx)
    assert state_id == scn.BI2I_COLLECT, f"asbi2i must return BI2I_COLLECT, got {state_id}"
    assert "init_paths" in ctx.user_data and len(ctx.user_data["init_paths"]) == 1
    init_b = ctx.user_data["init_paths"][0]
    assert init_b.exists()
    assert init_b.name.startswith("asbi2i_"), f"expected asbi2i_ prefix, got {init_b.name}"
    print(f"  ✓ asbi2i: вошли в BI2I_COLLECT, init предзагружен: {init_b.name}")

    # === 10. cancel:picker (inline-кнопка отмены) ===
    upd = make_callback_update("cancel:picker", user, chat)
    state_id = await scn.cmd_cancel(upd, ctx)
    assert state_id == -1, "cancel:picker должен возвращать END"
    assert ctx.user_data == {}, "user_data должен быть очищен после cancel"
    print("  ✓ cancel:picker → END, user_data очищен")

    # === 11. expired recipe ===
    # Сдвинем created_at recipe в прошлое, чтобы он считался просроченным.
    recipe.created_at = time.time() - RECIPE_TTL_SEC - 10
    upd = make_callback_update(f"regen:{recipe.task_uid}", user, chat)
    await scn.regen_cb(upd, ctx)
    answer_calls = upd.callback_query.answer.await_args_list
    assert any("устарели" in str(c).lower() or "устарел" in str(c).lower() for c in answer_calls), \
        f"expected 'устарели' answer on expired recipe, got: {answer_calls}"
    print("  ✓ regen на просроченный recipe → отбивка 'параметры устарели'")

    # === 12. capacity check ===
    # Подкрутим user_inflight чтобы имитировать 5 активных задач — entry-point должен отбить.
    state.user_inflight[42] = 5
    upd = make_message_update("/generate", user, chat)
    state_id = await scn.gen_start(upd, ctx)
    assert state_id == -1, "gen_start при заполненной очереди должен вернуть END"
    # Reply с отбивкой
    assert upd.message.reply_text.await_count >= 1
    reply_text = upd.message.reply_text.await_args.args[0]
    assert "очередь заполнена" in reply_text.lower()
    state.user_inflight.pop(42, None)
    print("  ✓ capacity check: 5 задач → отбивка, новая не пускается")


async def scenario_menu_help_callback():
    """menu:help shouldn't trigger conversation, should just echo HELP_TEXT."""
    from bot import scenarios as scn

    user = make_user(43)
    chat = make_chat(43)
    ctx = make_context()
    upd = make_callback_update("menu:help", user, chat)
    await scn.menu_router(upd, ctx)
    upd.callback_query.answer.assert_awaited()
    assert upd.callback_query.message.reply_text.await_count >= 1
    sent = upd.callback_query.message.reply_text.await_args.args[0]
    assert "Cloud.ru Image Bot" in sent and "/menu" in sent
    print("  ✓ menu:help → HELP_TEXT отправлен")


def make_async_func_mock(captured: dict, *, name: str):
    """Возвращает AsyncMock с side_effect, сохраняющим kwargs в captured["calls"]."""
    fake_job = FakeJob()

    async def fn(*args, **kwargs):
        captured.setdefault("calls", []).append({"name": name, "args": args, "kwargs": kwargs})
        return fake_job

    return fn


async def scenario_brand_t2i_flow():
    """Brand t2i (variant=photo): prompt → ratio → res → enqueue. Recipe.workflow='brand_t2i',
    params['variant']='photo', kb [Повторить/Изменить изображение]."""
    from bot import scenarios as scn
    from bot.state import get_state, reset_state_for_tests

    reset_state_for_tests()
    state = get_state()
    user = make_user(77)
    chat = make_chat(77)
    ctx = make_context()

    # Меню → brand Photo
    upd = make_callback_update("menu:brand_photo", user, chat)
    state_id = await scn.bt2i_start(upd, ctx)
    assert state_id == scn.BT2I_PROMPT
    assert ctx.user_data.get("variant") == "photo"
    print("  ✓ menu:brand_photo → BT2I_PROMPT (variant=photo)")

    # prompt
    upd = make_message_update("Cloud.ru hero shot, IT specialist on stage", user, chat)
    state_id = await scn.bt2i_prompt(upd, ctx)
    assert state_id == scn.BT2I_RATIO
    print("  ✓ prompt → BT2I_RATIO")

    # ratio
    upd = make_callback_update("ratio:r_16_9", user, chat)
    state_id = await scn.bt2i_ratio(upd, ctx)
    assert state_id == scn.BT2I_RES
    print("  ✓ ratio:r_16_9 → BT2I_RES")

    # res → enqueue
    captured: dict = {}
    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.run_brand_text2img", side_effect=make_async_func_mock(captured, name="brand_t2i")), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        upd = make_callback_update("res:k2", user, chat)
        state_id = await scn.bt2i_res(upd, ctx)
        assert state_id == -1
        await drain_user_queue(state, 77)

    # run_brand_text2img вызван с правильными kwargs (включая variant)
    assert len(captured["calls"]) == 1
    kw = captured["calls"][0]["kwargs"]
    assert kw["prompt"] == "Cloud.ru hero shot, IT specialist on stage"
    assert kw["variant"] == "photo"
    assert kw["model_name"] == "v3_1"
    assert kw["ratio"] == "r_16_9"
    assert kw["resolution"] == "k2"
    print("  ✓ run_brand_text2img(prompt=..., variant=photo, model_name=v3_1, ratio=r_16_9, resolution=k2)")

    # Recipe
    recipes = list(state.recipes.values())
    assert len(recipes) == 1
    rec = recipes[0]
    assert rec.workflow == "brand_t2i"
    assert rec.params.get("variant") == "photo"
    assert rec.prompt == "Cloud.ru hero shot, IT specialist on stage"
    print(f"  ✓ recipe.workflow=brand_t2i, variant=photo, prompt сохранён")

    # KB brand_t2i: [Повторить, Изменить изображение]. Без «Изменить текст» и без «Добавить Brand patterns».
    last_call = chat.send_photo.await_args
    kb = last_call.kwargs["reply_markup"]
    btns = [b for row in kb.inline_keyboard for b in row]
    assert any("Повторить" in b.text for b in btns)
    assert any("Изменить изображение" == b.text for b in btns)
    assert not any("Изменить текст" in b.text for b in btns), \
        "brand_t2i kb не должен содержать «Изменить текст»"
    assert not any("Добавить Brand patterns" == b.text for b in btns), \
        "brand_t2i kb не должен содержать «Добавить Brand patterns»"
    print("  ✓ kb brand_t2i: [Повторить/Изменить изображение]")

    # Regen — должен ещё раз вызвать run_brand_text2img с тем же prompt и variant
    captured.clear()
    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.run_brand_text2img", side_effect=make_async_func_mock(captured, name="brand_t2i")), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        upd = make_callback_update(f"regen:{rec.task_uid}", user, chat)
        await scn.regen_cb(upd, ctx)
        await drain_user_queue(state, 77)
    assert captured["calls"], "regen must re-invoke brand composer"
    re_kw = captured["calls"][0]["kwargs"]
    assert re_kw["prompt"] == rec.prompt
    assert re_kw["variant"] == "photo"
    assert re_kw["ratio"] == "r_16_9"
    print("  ✓ regen → run_brand_text2img со старым промптом и variant=photo")


async def scenario_brand_i2i_flow():
    """Brand i2i: collect → /done → ratio → res → enqueue. Recipe.workflow='brand_i2i',
    recipe.prompt='' (нет пользовательского текста), kb БЕЗ кнопки 'Изменить текст'."""
    from bot import scenarios as scn
    from bot.state import get_state, reset_state_for_tests

    reset_state_for_tests()
    state = get_state()
    user = make_user(88)
    chat = make_chat(88)
    ctx = make_context()
    ctx.bot.get_file = AsyncMock()

    # Подготовим фейковый init-файл в user_tmp (имитируем уже скачанный)
    user_tmp = state.user_tmp(88)
    init1 = user_tmp / "init_brand.jpg"
    init1.write_bytes(b"fake init bytes")
    ctx.user_data = {"init_paths": [init1], "ratio": "r_3_4", "resolution": "k1"}

    # Пройдём через bi2i_res с готовым стейтом (имитируем после collect/ratio).
    captured: dict = {}
    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.run_brand_img2img", side_effect=make_async_func_mock(captured, name="brand_i2i")), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        upd = make_callback_update("res:k1", user, chat)
        state_id = await scn.bi2i_res(upd, ctx)
        assert state_id == -1
        await drain_user_queue(state, 88)

    # run_brand_img2img вызван
    assert len(captured["calls"]) == 1
    kw = captured["calls"][0]["kwargs"]
    assert kw["init_paths"] == [init1]
    assert kw["model_name"] == "v3_1"
    assert kw["ratio"] == "r_3_4"
    assert kw["resolution"] == "k1"
    print("  ✓ run_brand_img2img(init_paths=..., model_name=v3_1, ratio=r_3_4, resolution=k1)")

    # Recipe
    recipes = list(state.recipes.values())
    assert len(recipes) == 1
    rec = recipes[0]
    assert rec.workflow == "brand_i2i"
    assert rec.prompt == "", f"brand_i2i prompt должен быть пустым, got {rec.prompt!r}"
    assert len(rec.init_paths) == 1 and rec.init_paths[0].exists()
    # init копия в regen_cache, оригинал удалён
    assert not init1.exists(), "оригинальный init должен быть удалён через cleanup_paths"
    print(f"  ✓ recipe.workflow=brand_i2i, prompt='', init в regen_cache")

    # KB brand_i2i: только «Повторить». Никаких текст/jump-кнопок.
    last_call = chat.send_photo.await_args
    kb = last_call.kwargs["reply_markup"]
    btns = [b for row in kb.inline_keyboard for b in row]
    assert any("Повторить" in b.text for b in btns)
    assert not any("Изменить текст" in b.text for b in btns), \
        "brand_i2i kb не должен содержать «Изменить текст»"
    assert not any("Изменить изображение" == b.text for b in btns), \
        "brand_i2i kb не должен содержать «Изменить изображение»"
    assert not any("Добавить Brand patterns" == b.text for b in btns), \
        "brand_i2i kb не должен содержать «Добавить Brand patterns»"
    # «Повторить» + 👍/👎 (feedback rating row добавлен ко всем сценариям).
    assert len(btns) == 3, f"brand_i2i kb expected 3 buttons (Повторить + 👍 + 👎), got {len(btns)}: {[b.text for b in btns]}"
    assert any(b.text == "👍" for b in btns) and any(b.text == "👎" for b in btns)
    print("  ✓ kb brand_i2i: [Повторить] + 👍/👎 only")

    # Regen — должен ещё раз вызвать run_brand_img2img со скопированными init
    captured.clear()
    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.run_brand_img2img", side_effect=make_async_func_mock(captured, name="brand_i2i")), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        upd = make_callback_update(f"regen:{rec.task_uid}", user, chat)
        await scn.regen_cb(upd, ctx)
        await drain_user_queue(state, 88)
    assert captured["calls"], "regen must re-invoke brand_i2i composer"
    re_kw = captured["calls"][0]["kwargs"]
    assert len(re_kw["init_paths"]) == 1
    # _stage_inits_from_recipe должен скопировать init из regen_cache в user_tmp (не использовать
    # исходник напрямую, чтобы cleanup_paths не зачистил regen_cache).
    staged = re_kw["init_paths"][0]
    assert "tmp" in str(staged), f"regen init должен идти из user_tmp, got {staged}"
    assert "regen_cache" not in str(staged), \
        f"regen НЕ должен передавать путь напрямую из regen_cache (cleanup его сотрёт): {staged}"
    print("  ✓ regen brand_i2i → run_brand_img2img со staged-init в user_tmp")


async def scenario_i2i_recipe_persists_inits():
    """img2img: init-файлы должны попадать в regen_cache + recipe.init_paths не пустой."""
    from bot import scenarios as scn
    from bot.state import get_state, reset_state_for_tests

    reset_state_for_tests()
    state = get_state()
    user = make_user(99)
    chat = make_chat(99)
    ctx = make_context()
    ctx.bot.get_file = AsyncMock()

    # Подготовим фейковый init-файл в user_tmp
    user_tmp = state.user_tmp(99)
    init1 = user_tmp / "init_test.jpg"
    init1.write_bytes(b"fake init bytes")

    ctx.user_data = {"init_paths": [init1], "prompt": "make it cyber"}

    captured: dict = {}
    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.ImageToImageWorkflow", side_effect=make_workflow_constructor(captured)), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        # Пройдём напрямую через i2i_res (модель зашита, нода зашита — пикеры сняты)
        ctx.user_data.update({"ratio": "r_3_4", "resolution": "k1"})
        upd = make_callback_update("res:k1", user, chat)
        state_id = await scn.i2i_res(upd, ctx)
        assert state_id == -1
        await drain_user_queue(state, 99)

    # Verify recipe
    recipes = list(state.recipes.values())
    assert len(recipes) == 1
    rec = recipes[0]
    assert rec.workflow == "nb_i2i"
    assert rec.prompt == "make it cyber"
    assert len(rec.init_paths) == 1, f"init_paths должен содержать 1 файл, got {len(rec.init_paths)}"
    assert rec.init_paths[0].exists(), "init копия в regen_cache должна существовать"
    # init_test.jpg исходный должен быть удалён (cleanup_paths)
    assert not init1.exists(), "оригинальный init должен быть удалён через cleanup_paths"
    print(f"  ✓ i2i recipe: init {rec.init_paths[0].name} в regen_cache, исходный удалён")


async def scenario_brand_t2i_render_variant():
    """Brand t2i variant=render: тот же flow что в photo, но system-prompt-doc другой.
    Проверяем, что в bot/scenarios.py callback `menu:brand_render` → variant='render'."""
    from bot import scenarios as scn
    from bot.state import get_state, reset_state_for_tests

    reset_state_for_tests()
    state = get_state()
    user = make_user(70)
    chat = make_chat(70)
    ctx = make_context()

    # menu:brand_render → BT2I_PROMPT variant='render'
    upd = make_callback_update("menu:brand_render", user, chat)
    state_id = await scn.bt2i_start(upd, ctx)
    assert state_id == scn.BT2I_PROMPT
    assert ctx.user_data.get("variant") == "render", f"variant render, got {ctx.user_data.get('variant')}"
    print("  ✓ menu:brand_render → BT2I_PROMPT (variant=render)")

    upd = make_message_update("3D rendered isometric cloud server", user, chat)
    await scn.bt2i_prompt(upd, ctx)
    upd = make_callback_update("ratio:r_1_1", user, chat)
    await scn.bt2i_ratio(upd, ctx)

    captured: dict = {}
    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.run_brand_text2img", side_effect=make_async_func_mock(captured, name="brand_t2i")), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        upd = make_callback_update("res:k2", user, chat)
        await scn.bt2i_res(upd, ctx)
        await drain_user_queue(state, 70)

    assert captured["calls"][0]["kwargs"]["variant"] == "render"
    rec = list(state.recipes.values())[0]
    assert rec.params["variant"] == "render"
    print("  ✓ workflow вызван с variant=render, recipe.params['variant']='render'")


async def scenario_brand_t2i_isometric_variant():
    """Brand t2i variant=isometric: проверяем callback menu:brand_isometric."""
    from bot import scenarios as scn
    from bot.state import get_state, reset_state_for_tests

    reset_state_for_tests()
    state = get_state()
    user = make_user(71)
    chat = make_chat(71)
    ctx = make_context()

    upd = make_callback_update("menu:brand_isometric", user, chat)
    state_id = await scn.bt2i_start(upd, ctx)
    assert state_id == scn.BT2I_PROMPT
    assert ctx.user_data.get("variant") == "isometric", f"variant isometric, got {ctx.user_data.get('variant')}"
    print("  ✓ menu:brand_isometric → BT2I_PROMPT (variant=isometric)")

    upd = make_message_update("a database server isometric line art", user, chat)
    await scn.bt2i_prompt(upd, ctx)
    upd = make_callback_update("ratio:r_1_1", user, chat)
    await scn.bt2i_ratio(upd, ctx)

    captured: dict = {}
    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.run_brand_text2img", side_effect=make_async_func_mock(captured, name="brand_t2i")), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        upd = make_callback_update("res:k2", user, chat)
        await scn.bt2i_res(upd, ctx)
        await drain_user_queue(state, 71)

    assert captured["calls"][0]["kwargs"]["variant"] == "isometric"
    rec = list(state.recipes.values())[0]
    assert rec.params["variant"] == "isometric"
    print("  ✓ workflow вызван с variant=isometric, recipe.params['variant']='isometric'")


async def scenario_prep_speaker_flow():
    """/prep_speaker: photo → gender → ratio → res → enqueue с build_speaker_prep_workflow.
    Recipe.workflow='speaker', kb только [Повторить]."""
    from bot import scenarios as scn
    from bot.state import get_state, reset_state_for_tests

    reset_state_for_tests()
    state = get_state()
    user = make_user(55)
    chat = make_chat(55)
    ctx = make_context()
    ctx.bot.get_file = AsyncMock()

    # Подготовим фейковый photo-файл в user_tmp (имитируем уже скачанный sp_photo шаг)
    user_tmp = state.user_tmp(55)
    photo = user_tmp / "speaker.jpg"
    photo.write_bytes(b"fake speaker bytes")
    ctx.user_data = {"speaker_photo": photo, "gender": "man", "ratio": "r_3_4"}

    # Проходим через sp_res с готовым стейтом
    captured: dict = {}
    fake_wf = MagicMock()
    fake_wf.ratio = ""
    fake_wf.resolution = ""
    fake_wf.run_with_files = AsyncMock(return_value=FakeJob())

    def fake_build(client):
        captured.setdefault("builds", []).append(client)
        return fake_wf

    with patch("bot.scenarios.PhygitalClient", side_effect=lambda *a, **kw: make_fake_client_cm()), \
         patch("bot.scenarios.build_speaker_prep_workflow", side_effect=fake_build), \
         patch("httpx.AsyncClient", FakeAsyncHttpx):
        upd = make_callback_update("res:k2", user, chat)
        state_id = await scn.sp_res(upd, ctx)
        assert state_id == -1
        await drain_user_queue(state, 55)

    # build_speaker_prep_workflow был вызван
    assert "builds" in captured and len(captured["builds"]) == 1
    # run_with_files был вызван с правильным prompt + init_paths
    rwf_call = fake_wf.run_with_files.await_args
    assert "init_paths" in rwf_call.kwargs
    init_paths = rwf_call.kwargs["init_paths"]
    assert len(init_paths) == 2, f"speaker init_paths должно быть 2 (reference + speaker_photo), got {len(init_paths)}"
    assert init_paths[1] == photo
    assert fake_wf.ratio == "r_3_4"
    assert fake_wf.resolution == "k2"
    print("  ✓ sp_res → build_speaker_prep_workflow → run_with_files(reference+speaker_photo)")

    # Recipe
    recipes = list(state.recipes.values())
    assert len(recipes) == 1
    rec = recipes[0]
    assert rec.workflow == "speaker"
    assert rec.params == {"gender": "man", "ratio": "r_3_4", "resolution": "k2"}
    print(f"  ✓ recipe.workflow=speaker, params={rec.params}")

    # KB speaker: [Повторить] + 5 цветных кнопок «Сменить фон» (spkrbg:<uid>:<HEX>)
    last_call = chat.send_photo.await_args
    kb = last_call.kwargs["reply_markup"]
    btns = [b for row in kb.inline_keyboard for b in row]
    assert any("Повторить" in b.text for b in btns)
    assert not any("Изменить текст" in b.text for b in btns), \
        "speaker kb не должен содержать «Изменить текст»"
    bg_btns = [b for b in btns if (b.callback_data or "").startswith("spkrbg:")]
    assert len(bg_btns) == 5, \
        f"speaker kb должен иметь 5 цветовых кнопок, got {len(bg_btns)}: {[b.text for b in bg_btns]}"
    # каждая колбэк-строка spkrbg:<task_uid>:<HEX> — HEX это 6 hex-символов
    for b in bg_btns:
        _, _, hx = b.callback_data.split(":")
        assert len(hx) == 6 and all(c in "0123456789ABCDEF" for c in hx), \
            f"некорректный HEX в кнопке: {b.callback_data!r}"
    # Повторить + 5 цветов + 👍/👎
    assert len(btns) == 1 + 5 + 2, \
        f"speaker kb expected 1 + 5 + 2 buttons, got {len(btns)}: {[b.text for b in btns]}"
    assert any(b.text == "👍" for b in btns) and any(b.text == "👎" for b in btns)
    print("  ✓ kb speaker: [Повторить] + 5 цветов «Сменить фон» + 👍/👎")

    # cleanup: исходный photo удалён
    assert not photo.exists(), "speaker_photo должен быть удалён через cleanup_paths"
    print("  ✓ speaker_photo очищен через cleanup_paths")


async def scenario_safety_scrubber_loop():
    """workflows/brand_text2img: при safety reject от Nano Banana —
    прогоняем prompt через Gemini-scrubber и пробуем ещё раз. Проверяем:
      - parse flagged word из job.error
      - scrubber вызван 1 раз
      - Nano Banana вызван 2 раза (fail → retry success)
      - финальный job — completed
    """
    from workflows import brand_text2img as bt2i

    # Счётчики
    gemini_calls: list[dict] = []  # описания вызовов GeminiTextWorkflow.run_text
    nano_calls: list[str] = []      # prompts отправленные в Nano Banana

    # Mocked GeminiTextWorkflow — instance counter + sequenced responses
    class _MockGemini:
        _counter = 0

        def __init__(self, client):
            self.client = client
            self.id = _MockGemini._counter
            _MockGemini._counter += 1

        async def run_text(self, *, prompt, init_img_ids=None, init_img_dims=None, document_ids=None):
            gemini_calls.append({
                "id": self.id, "prompt": prompt[:80], "document_ids": list(document_ids or []),
            })
            from client.models import GenerationJob
            # 1-й вызов — это enhancer (исходный prompt → enhanced English)
            # 2-й — scrubber (enhanced English с flagged word → cleaned)
            if self.id == 0:
                return GenerationJob(
                    job_id="enh-1", status="completed",
                    result_text=(
                        "A black-and-white 2D isometric line illustration of a wide low control-panel slab "
                        "with a tall cylindrical knob and a small @ glyph."
                    ),
                )
            return GenerationJob(
                job_id=f"scr-{self.id}", status="completed",
                result_text=(
                    "A black-and-white 2D isometric line illustration of a wide low control-panel slab "
                    "with a tall cylindrical disc and a small @ glyph."
                ),
            )

    class _MockImageGen:
        _counter = 0

        def __init__(self, client, *, model_name, ratio, resolution):
            self.client = client
            self.id = _MockImageGen._counter
            _MockImageGen._counter += 1

        async def run(self, *, prompt):
            nano_calls.append(prompt)
            from client.models import GenerationJob
            if self.id == 0:
                # Первый прогон — safety reject
                return GenerationJob(
                    job_id="nb-fail", status="failed",
                    error="Please remove potential harmful word knob from the prompt and press Generate again",
                )
            return GenerationJob(
                job_id="nb-ok", status="completed",
                result_urls=["http://fake.example/cleaned.png"],
            )

    async def fake_get_text2img_doc(client, variant):
        return 1001

    async def fake_get_scrubber_doc(client):
        return 1002

    with patch("workflows.brand_text2img.GeminiTextWorkflow", _MockGemini), \
         patch("workflows.brand_text2img.ImageGenWorkflow", _MockImageGen), \
         patch("workflows.brand_text2img.get_text2img_doc", side_effect=fake_get_text2img_doc), \
         patch("workflows.brand_text2img.get_scrubber_doc", side_effect=fake_get_scrubber_doc):
        progress_events: list[str] = []

        async def progress_cb(msg):
            progress_events.append(msg)

        job = await bt2i.run_brand_text2img(
            client=MagicMock(),
            prompt="rocket launch isometric",
            variant="isometric",
            progress_cb=progress_cb,
        )

    # Проверки
    assert job.status == "completed", f"финальный job должен быть completed, got {job.status}"
    print(f"  ✓ финальный job completed после safety retry")

    # 2 Gemini вызова: enhancer + scrubber
    assert len(gemini_calls) == 2, f"ожидаем 2 Gemini вызова (enhancer+scrubber), got {len(gemini_calls)}"
    assert gemini_calls[0]["document_ids"] == [1001], "1-й Gemini — enhancer doc"
    assert gemini_calls[1]["document_ids"] == [1002], "2-й Gemini — scrubber doc"
    assert "FLAGGED WORD: knob" in gemini_calls[1]["prompt"], \
        f"scrubber должен получить flagged word knob, got prompt={gemini_calls[1]['prompt']!r}"
    print("  ✓ Gemini вызван 2 раза: enhancer (doc 1001) + scrubber (doc 1002, FLAGGED=knob)")

    # 2 Nano Banana вызова — fail + retry
    assert len(nano_calls) == 2, f"ожидаем 2 Nano Banana вызова, got {len(nano_calls)}"
    assert "knob" in nano_calls[0], "1-й Nano Banana — оригинал с knob"
    assert "knob" not in nano_calls[1], "2-й Nano Banana — без knob (заменён на disc)"
    assert "disc" in nano_calls[1], "2-й Nano Banana — disc вместо knob"
    print("  ✓ Nano Banana вызван 2 раза: original (с knob) → cleaned (disc)")

    # Progress events: должен быть step transition «Nano Banana» (после Gemini),
    # затем «Чистка safety-words: «knob»…», затем «Nano Banana (повтор)»
    assert any("Nano Banana" == e for e in progress_events), \
        f"первый Nano Banana step не сообщён: {progress_events}"
    assert any("Чистка safety-words" in e for e in progress_events), \
        f"scrub step не сообщён: {progress_events}"
    assert any("повтор" in e for e in progress_events), \
        f"resubmit step не сообщён: {progress_events}"
    print(f"  ✓ progress events: {progress_events}")


async def scenario_cancel_in_picker():
    """cancel:picker в каждом ConversationHandler сбрасывает state в END и чистит user_data."""
    from bot import scenarios as scn

    user = make_user(33)
    chat = make_chat(33)

    # Имитируем «середина GEN_RATIO» — user_data заполнен частично
    ctx = make_context()
    ctx.user_data = {"prompt": "test", "ratio": None}
    upd = make_callback_update("cancel:picker", user, chat)
    state_id = await scn.cmd_cancel(upd, ctx)
    assert state_id == -1, "cancel должен вернуть END"
    assert ctx.user_data == {}, f"user_data должен быть очищен, got {ctx.user_data}"
    print("  ✓ cancel в GEN_RATIO → END, user_data={}")

    # cancel в I2I_PROMPT
    ctx = make_context()
    ctx.user_data = {"init_paths": [], "prompt": "x"}
    upd = make_callback_update("cancel:picker", user, chat)
    state_id = await scn.cmd_cancel(upd, ctx)
    assert state_id == -1
    assert ctx.user_data == {}
    print("  ✓ cancel в I2I_PROMPT → END, user_data={}")

    # cancel в SP_GENDER
    ctx = make_context()
    ctx.user_data = {"speaker_photo": Path("fake.jpg")}
    upd = make_callback_update("cancel:picker", user, chat)
    state_id = await scn.cmd_cancel(upd, ctx)
    assert state_id == -1
    assert ctx.user_data == {}
    print("  ✓ cancel в SP_GENDER → END, user_data={}")


async def scenario_status_reporter_timeline():
    """StatusReporter: проверяем формат сообщений на типичной brand-цепочке
    (queued → waiting_slot → start → step → done) и на error-цепочке."""
    from bot.status_reporter import StatusReporter

    # Happy path: brand_t2i
    msgs: list[str] = []
    status = MagicMock()

    async def fake_edit(text):
        msgs.append(text)

    status.edit_text = fake_edit
    r = StatusReporter(status=status, label="brand_t2i:isometric/v3_1/r_16_9/k2", eta_sec=90)

    await r.queued(queue_pos=2)
    assert "принято" in msgs[-1].lower() and "очередь №2" in msgs[-1]
    print("  ✓ queued: 'принято' + 'очередь №2'")

    await r.waiting_slot()
    assert "в очереди" in msgs[-1].lower() and "жду" in msgs[-1].lower()
    print("  ✓ waiting_slot: 'в очереди' + 'жду свободный слот'")

    await r.start("Gemini Text")
    assert "генерация" in msgs[-1].lower() and "Gemini Text" in msgs[-1] and "~1:30" in msgs[-1]
    print("  ✓ start: 'генерация Gemini Text • 0:00 / ~1:30'")

    await r.step("Nano Banana")
    assert "Nano Banana" in msgs[-1]
    print("  ✓ step: 'Nano Banana'")

    await r.done(job_id="42")
    final = msgs[-1]
    assert "готово" in final.lower() and "job 42" in final
    # breakdown с длительностями этапов
    assert "Gemini Text" in final and "Nano Banana" in final
    print(f"  ✓ done: 'готово ... job 42' с breakdown")

    # Error path
    msgs2: list[str] = []
    status2 = MagicMock()

    async def fake_edit2(text):
        msgs2.append(text)

    status2.edit_text = fake_edit2
    r2 = StatusReporter(status=status2, label="generate/v3_1/default/default", eta_sec=45)
    await r2.start("Nano Banana")
    await r2.error("Please remove potential harmful word knob from the prompt")
    final2 = msgs2[-1]
    assert "ошибка" in final2.lower()
    assert "Nano Banana" in final2
    assert "knob" in final2
    print(f"  ✓ error: 'ошибка ... Nano Banana ... knob'")


async def scenario_whitelist_reject():
    """Юзер не из whitelist получает отбивку и не доходит до сценария."""
    from bot import scenarios as scn

    user = make_user(999)
    chat = make_chat(999)
    ctx = make_context()
    upd = make_message_update("/generate", user, chat)

    # Здесь _is_allowed мокается отдельно — на False — чтобы whitelist_only декоратор сбил вход.
    # whitelist_only возвращает None (не END) при deny — этого достаточно, чтобы ConversationHandler
    # не запустил FSM. Проверяем что вернулось не state_id (== не вошли в FSM) и что юзеру отправили reject.
    with patch("bot.auth._is_allowed", return_value=False):
        state_id = await scn.gen_start(upd, ctx)
    assert state_id is None, f"whitelist_only при deny должен вернуть None, got {state_id!r}"
    # И в чат улетел текст про "Доступ запрещён"
    deny_msgs = [c.args[0] for c in upd.message.reply_text.call_args_list]
    assert any("Доступ запрещён" in t for t in deny_msgs), \
        f"ожидали reject-сообщение, got {deny_msgs!r}"
    print("  ✓ non-whitelist user → None + reject-msg")


async def scenario_feedback_survey_flow():
    """Подсистема обратной связи: меню → опрос (single + multi + skip + submit),
    рейтинг 👍, комментарий, авто-prompt после N-й генерации.

    Дополнительно проверяем: rating_kb появляется в _action_keyboard для всех
    workflow'ов и записи в JSONL формируются корректно.
    """
    import json
    import tempfile
    from pathlib import Path as _Path

    from bot import feedback as fb
    from bot import scenarios as scn

    # Изолируем feedback в tmp-директории чтобы не засорять реальный storage/
    with tempfile.TemporaryDirectory() as tmp:
        events = _Path(tmp) / "feedback.jsonl"
        state_file = _Path(tmp) / "feedback_state.json"
        with patch.object(fb, "FEEDBACK_DIR", _Path(tmp)), \
             patch.object(fb, "EVENTS_FILE", events), \
             patch.object(fb, "STATE_FILE", state_file):
            fb.reset_feedback_store_for_tests()

            user = make_user(777)
            chat = make_chat(777)
            ctx = make_context()

            # === 1. menu:feedback → подменю ===
            upd = make_callback_update("menu:feedback", user, chat)
            await fb.feedback_menu_cb(upd, ctx)
            upd.callback_query.edit_message_text.assert_awaited_once()
            kb = upd.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
            labels = [b.text for row in kb.inline_keyboard for b in row]
            assert "Пройти опрос" in labels, f"submenu missing survey: {labels}"
            assert "Оставить комментарий" in labels
            # owner-only stats — uid 777 не owner → не должно быть.
            assert "Статистика" not in labels
            print("  ✓ menu:feedback показал подменю без 'Статистика' (non-owner)")

            # === 2. start survey ===
            upd = make_callback_update("fb:menu:survey", user, chat)
            await fb.feedback_action_cb(upd, ctx)
            assert "fb_survey" in ctx.user_data
            assert ctx.user_data["fb_survey"]["step"] == 0
            print("  ✓ fb:menu:survey стартовал опрос (step=0)")

            # === 3. A1 = multi: toggle + next ===
            # Q0 = A1 (multi). Тоггл brand_photo + presentations(нет — это A4), потом next.
            upd = make_callback_update("fb:srv:toggle:brand_photo", user, chat)
            await fb.survey_cb(upd, ctx)
            assert "brand_photo" in ctx.user_data["fb_survey"]["pending_multi"]
            upd = make_callback_update("fb:srv:next", user, chat)
            await fb.survey_cb(upd, ctx)
            assert ctx.user_data["fb_survey"]["answers"]["A1"] == ["brand_photo"]
            assert ctx.user_data["fb_survey"]["step"] == 1
            print("  ✓ A1 multi: toggle+next записал ['brand_photo'], step → 1")

            # === 4. A2 = single: pick ===
            upd = make_callback_update("fb:srv:pick:all_needed", user, chat)
            await fb.survey_cb(upd, ctx)
            assert ctx.user_data["fb_survey"]["answers"]["A2"] == "all_needed"
            assert ctx.user_data["fb_survey"]["step"] == 2
            print("  ✓ A2 single: pick all_needed")

            # === 5. A3 = free: skip ===
            upd = make_callback_update("fb:srv:skip", user, chat)
            await fb.survey_cb(upd, ctx)
            assert "A3" in ctx.user_data["fb_survey"]["skipped"]
            assert ctx.user_data["fb_survey"]["step"] == 3
            print("  ✓ A3 free: skip")

            # === 6. Пропустим остальные через skip и проверим submit ===
            for expected_step in range(3, len(fb.SURVEY_QUESTIONS)):
                upd = make_callback_update("fb:srv:skip", user, chat)
                await fb.survey_cb(upd, ctx)
            # После последнего skip — show_summary, step == len
            assert ctx.user_data["fb_survey"]["step"] == len(fb.SURVEY_QUESTIONS)
            print(f"  ✓ остальные {len(fb.SURVEY_QUESTIONS)-3} вопросов skipped, дошли до summary")

            # === 7. submit ===
            upd = make_callback_update("fb:srv:submit", user, chat)
            await fb.survey_cb(upd, ctx)
            assert "fb_survey" not in ctx.user_data
            # JSONL должен содержать одно событие survey
            assert events.exists(), "feedback.jsonl must be created on submit"
            lines = events.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 1
            ev = json.loads(lines[0])
            assert ev["type"] == "survey"
            assert ev["uid"] == 777
            assert ev["answers"]["A1"] == ["brand_photo"]
            assert ev["answers"]["A2"] == "all_needed"
            assert "A3" in ev["skipped"]
            assert ev["survey_version"] == fb.SURVEY_VERSION
            print(f"  ✓ submit: записано в JSONL, version={ev['survey_version']}, {len(ev['answers'])} ответов")

            # === 8. Состояние юзера — опрос помечен как пройденный ===
            st = fb.get_feedback_store().get_uid_state(777)
            assert st["survey_completed_version"] == fb.SURVEY_VERSION
            assert st["surveys_total"] == 1
            print("  ✓ uid state: survey_completed_version выставлен")

            # === 9. Повторный вход в подменю — лейбл "Пройти опрос ещё раз" ===
            upd = make_callback_update("menu:feedback", user, chat)
            await fb.feedback_menu_cb(upd, ctx)
            kb = upd.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
            labels = [b.text for row in kb.inline_keyboard for b in row]
            assert "Пройти опрос ещё раз" in labels
            print("  ✓ при повторном входе кнопка → 'Пройти опрос ещё раз'")

            # === 10. Rating 👍 без причин ===
            upd = make_callback_update("fb:rate:abc123:nb_t2i:up", user, chat)
            await fb.rating_cb(upd, ctx)
            lines = events.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 2
            ev = json.loads(lines[1])
            assert ev["type"] == "rating"
            assert ev["value"] == "up"
            assert ev["task_uid"] == "abc123"
            assert ev["workflow"] == "nb_t2i"
            print("  ✓ rating up записан в JSONL")

            # === 11. Comment flow ===
            upd = make_callback_update("fb:cmt:idea", user, chat)
            await fb.comment_category_cb(upd, ctx)
            assert ctx.user_data["fb_pending"]["kind"] == "comment"
            assert ctx.user_data["fb_pending"]["category"] == "idea"
            # Юзер шлёт текст → feedback_text_listener
            upd = make_message_update("Хочу батч-генерацию по 10 картинок", user, chat)
            await fb.feedback_text_listener(upd, ctx)
            assert "fb_pending" not in ctx.user_data
            lines = events.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 3
            ev = json.loads(lines[2])
            assert ev["type"] == "comment"
            assert ev["category"] == "idea"
            assert "батч" in ev["text"]
            print("  ✓ comment: категория idea + текст записаны")

            # === 12. Auto-prompt: после 3-й успешной генерации триггерится ===
            store = fb.get_feedback_store()
            # uid юзера с уже survey_completed_version → НЕ должен получать prompt
            assert not store.should_auto_prompt(777)
            # Свежий uid → after 3 успехов должен сработать.
            fresh = 999
            assert not store.should_auto_prompt(fresh)  # 0 генераций
            await store.mark_generation_success(fresh)
            await store.mark_generation_success(fresh)
            assert not store.should_auto_prompt(fresh)  # 2 — рано
            await store.mark_generation_success(fresh)
            assert store.should_auto_prompt(fresh), "after 3rd success — должен предложить"
            print("  ✓ should_auto_prompt: триггер на 3-й успешной генерации")

            # === 13. on_generation_success реально шлёт баннер ===
            sent: list = []
            async def send_fn(text, reply_markup=None):
                sent.append((text, reply_markup))
            # fresh uid уже на 3 успехах — следующий вызов on_generation_success
            # инкрементит до 4 и (т.к. should_auto_prompt всё ещё True) шлёт баннер.
            await fb.on_generation_success(fresh, send_fn)
            assert len(sent) == 1, f"банер не отправлен: {sent}"
            text, kb = sent[0]
            assert "опрос" in text.lower()
            kb_labels = [b.text for row in kb.inline_keyboard for b in row]
            assert "Пройти" in kb_labels
            assert "Не сейчас" in kb_labels
            assert "Не показывать" in kb_labels
            print("  ✓ on_generation_success: баннер отправлен с кнопками")

            # === 14. 'Не показывать' → больше не предлагаем ===
            upd = make_callback_update("fb:auto:never", user, chat)
            upd.effective_user = make_user(fresh)
            await fb.auto_prompt_cb(upd, ctx)
            assert not store.should_auto_prompt(fresh)
            st_fresh = store.get_uid_state(fresh)
            assert st_fresh["survey_completed_version"] == f"{fb.SURVEY_VERSION}_declined"
            print("  ✓ 'Не показывать' выставляет declined-флаг, prompts отключены")

            # === 15. rating_kb присутствует в _action_keyboard для всех workflow ===
            for wf in ("nb_t2i", "brand_t2i", "speaker", "nb_i2i", "brand_i2i"):
                kb = scn._action_keyboard("test_uid", workflow=wf)
                all_data = [b.callback_data for row in kb.inline_keyboard for b in row]
                assert any(d.startswith("fb:rate:test_uid:") and d.endswith(":up") for d in all_data), \
                    f"workflow={wf} missing 👍: {all_data}"
                assert any(d.startswith("fb:rate:test_uid:") and d.endswith(":down") for d in all_data), \
                    f"workflow={wf} missing 👎: {all_data}"
            print("  ✓ _action_keyboard содержит 👍/👎 для всех workflow")

            # === 16. menu_root_kb содержит 'Обратная связь' ===
            root_kb = scn._menu_root_kb()
            root_labels = [b.text for row in root_kb.inline_keyboard for b in row]
            assert "Обратная связь" in root_labels
            print("  ✓ _menu_root_kb содержит 'Обратная связь'")


# ═══════════════════════════════════════════════════════════════════════════
# main runner
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    # cli.load_session — чтобы BotState не пытался читать реальный session.json.
    # bot.auth._is_allowed — пропустить тестовых uid (в .env реальные whitelist'ы).
    with patch("cli.load_session", return_value=MagicMock()), \
         patch("bot.auth._is_allowed", return_value=True):

        print("\n━━━ SCENARIO 1: menu:help ━━━")
        await scenario_menu_help_callback()

        print("\n━━━ SCENARIO 2: полный t2i + все пост-задачные кнопки + cancel + capacity ━━━")
        await scenario_full_generate_with_post_actions()

        print("\n━━━ SCENARIO 3: img2img recipe сохраняет init-файлы ━━━")
        await scenario_i2i_recipe_persists_inits()

        print("\n━━━ SCENARIO 4: brand_t2i полный флоу + regen ━━━")
        await scenario_brand_t2i_flow()

        print("\n━━━ SCENARIO 5: brand_i2i полный флоу + kb без 'Изменить текст' + regen ━━━")
        await scenario_brand_i2i_flow()

        print("\n━━━ SCENARIO 6: brand_t2i variant=render ━━━")
        await scenario_brand_t2i_render_variant()

        print("\n━━━ SCENARIO 7: brand_t2i variant=isometric ━━━")
        await scenario_brand_t2i_isometric_variant()

        print("\n━━━ SCENARIO 8: prep_speaker полный флоу ━━━")
        await scenario_prep_speaker_flow()

        print("\n━━━ SCENARIO 9: safety-scrubber retry loop ━━━")
        await scenario_safety_scrubber_loop()

        print("\n━━━ SCENARIO 10: cancel:picker в разных FSM-стейтах ━━━")
        await scenario_cancel_in_picker()

        print("\n━━━ SCENARIO 11: StatusReporter timeline ━━━")
        await scenario_status_reporter_timeline()

        print("\n━━━ SCENARIO 12: whitelist reject ━━━")
        await scenario_whitelist_reject()

        print("\n━━━ SCENARIO 13: feedback — меню/опрос/rating/comment/auto-prompt ━━━")
        await scenario_feedback_survey_flow()

    print("\n✅ ALL SCENARIOS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\n❌ ASSERTION FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(2)
