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
    assert any("В img2img" == b.text for b in btns)
    assert any("В brand img2img" == b.text for b in btns)
    print("  ✓ result отправлен с inline-клавиатурой [Повторить/Изменить текст/В img2img/В brand img2img]")

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
    assert "Phygital" in sent and "/menu" in sent
    print("  ✓ menu:help → HELP_TEXT отправлен")


def make_async_func_mock(captured: dict, *, name: str):
    """Возвращает AsyncMock с side_effect, сохраняющим kwargs в captured["calls"]."""
    fake_job = FakeJob()

    async def fn(*args, **kwargs):
        captured.setdefault("calls", []).append({"name": name, "args": args, "kwargs": kwargs})
        return fake_job

    return fn


async def scenario_brand_t2i_flow():
    """Brand t2i: prompt → ratio → res → enqueue. Recipe.workflow='brand_t2i',
    kb с тремя кнопками (Повторить/Изменить текст/В img2img)."""
    from bot import scenarios as scn
    from bot.state import get_state, reset_state_for_tests

    reset_state_for_tests()
    state = get_state()
    user = make_user(77)
    chat = make_chat(77)
    ctx = make_context()

    # Меню → /brand_generate
    upd = make_callback_update("menu:brand_generate", user, chat)
    state_id = await scn.bt2i_start(upd, ctx)
    assert state_id == scn.BT2I_PROMPT
    print("  ✓ menu:brand_generate → BT2I_PROMPT")

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

    # run_brand_text2img вызван с правильными kwargs
    assert len(captured["calls"]) == 1
    kw = captured["calls"][0]["kwargs"]
    assert kw["prompt"] == "Cloud.ru hero shot, IT specialist on stage"
    assert kw["model_name"] == "v3_1"
    assert kw["ratio"] == "r_16_9"
    assert kw["resolution"] == "k2"
    print("  ✓ run_brand_text2img(prompt=..., model_name=v3_1, ratio=r_16_9, resolution=k2)")

    # Recipe
    recipes = list(state.recipes.values())
    assert len(recipes) == 1
    rec = recipes[0]
    assert rec.workflow == "brand_t2i"
    assert rec.prompt == "Cloud.ru hero shot, IT specialist on stage"
    print(f"  ✓ recipe.workflow=brand_t2i prompt сохранён")

    # KB: четыре кнопки, включая «Изменить текст» и «В brand img2img»
    last_call = chat.send_photo.await_args
    kb = last_call.kwargs["reply_markup"]
    btns = [b for row in kb.inline_keyboard for b in row]
    assert any("Повторить" in b.text for b in btns)
    assert any("Изменить текст" in b.text for b in btns)
    assert any("В img2img" == b.text for b in btns)
    assert any("В brand img2img" == b.text for b in btns)
    print("  ✓ kb: [Повторить/Изменить текст/В img2img/В brand img2img]")

    # Regen — должен ещё раз вызвать run_brand_text2img с тем же prompt
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
    assert re_kw["ratio"] == "r_16_9"
    print("  ✓ regen → run_brand_text2img со старым промптом")


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

    # KB: Повторить + В img2img + В brand img2img, БЕЗ «Изменить текст»
    last_call = chat.send_photo.await_args
    kb = last_call.kwargs["reply_markup"]
    btns = [b for row in kb.inline_keyboard for b in row]
    assert any("Повторить" in b.text for b in btns)
    assert any("В img2img" == b.text for b in btns)
    assert any("В brand img2img" == b.text for b in btns)
    assert not any("Изменить текст" in b.text for b in btns), \
        "brand_i2i kb не должен содержать «Изменить текст» (нет пользовательского текста)"
    print("  ✓ kb brand_i2i: [Повторить/В img2img/В brand img2img] (без «Изменить текст»)")

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
