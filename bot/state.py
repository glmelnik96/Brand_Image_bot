"""Глобальное shared-состояние бота: SessionManager, семафор, per-user очередь, tmp, recipes.

Концепция параллелизма:
- `global_sem`: глобальный лимит на одновременные Phygital-submit'ы (settings.bot_max_concurrency).
- `user_queues[uid]`: FIFO задач конкретного пользователя (`asyncio.Queue`, maxsize=USER_QUEUE_LIMIT).
- `user_workers[uid]`: persistent worker-task, который тянет из своей очереди и пускает задачи
  через свой per-user семафор MAX_PER_USER_INFLIGHT и общий `global_sem`.
- `user_inflight[uid]`: текущее число активно работающих задач юзера (для UI и для entry-точек).

Tmp и кэш:
- `bot/tmp/<uid>/`             — корень для in-progress сценариев (collected init images до /done).
- `bot/tmp/<uid>/<task_uid>/`  — `task_tmp`: изолированная папка под конкретную задачу,
                                  удаляется в `_execute_task.finally` через `clear_task_tmp`.
                                  Корень юзера зачищается через `clear_user_tmp` только
                                  когда `user_inflight[uid] == 0`.
- `bot/regen_cache/<uid>/<task_uid>/`  — пост-задачный кэш (24ч):
                                          init-картинки задачи + первая result-картинка.
                                          Нужен для кнопок 🔄 Повторить / ✏️ Уточнить / 🖼 Как img2img.
                                          Чистится тремя путями:
                                            1) lazy при `get_recipe` если age > TTL;
                                            2) фоновым воркером `_purger_loop` (каждые 10 мин);
                                            3) startup sweep в __init__ (recipes пустые после
                                               рестарта → все папки автоматически сироты).

Recipes:
- `recipes[task_uid] = TaskRecipe(...)` — снимок параметров завершённой задачи + пути в regen_cache.
  Сохраняется в `_execute_task` после успешной отправки результата. TTL = RECIPE_TTL_SEC.

Pending edits:
- `pending_edits[uid] = (task_uid, expires_at)` — состояние «юзер кликнул ✏️ Уточнить,
  следующее текстовое сообщение от него = новый промпт». TTL = PENDING_EDIT_TTL_SEC.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from loguru import logger

from client.config import settings
from client.session import SessionManager

ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = ROOT / "bot" / "tmp"
REGEN_ROOT = ROOT / "bot" / "regen_cache"

# Сколько задач юзер может держать «в полёте» одновременно (между этим и
# `global_sem` стоит общий глобальный лимит — никогда не больше bot_max_concurrency).
MAX_PER_USER_INFLIGHT = 2
# Лимит длины per-user очереди (включая активные). Сверх — отбивка на entry point.
USER_QUEUE_LIMIT = 5

# Сколько живёт recipe в памяти и его файлы в regen_cache.
RECIPE_TTL_SEC = 24 * 3600
# Сколько ждём, что юзер пришлёт новый промпт после клика ✏️ Уточнить.
PENDING_EDIT_TTL_SEC = 5 * 60
# Период фонового purge.
PURGER_INTERVAL_SEC = 10 * 60


# Тип воркера задачи — callable, который сам пушит через семафоры и шлёт UI-апдейты.
# Параметры запихиваем в closure внутри scenarios.py.
TaskRunner = Callable[[], Awaitable[None]]


@dataclass
class TaskRecipe:
    """Снимок завершённой задачи. Достаточен, чтобы повторно её запустить (🔄 / ✏️) или
    использовать её результат как init для img2img (🖼).

    Поля:
      task_uid     — короткий uid задачи (тот же, что в `bot/tmp/<uid>/<task_uid>/`).
      user_id      — кто запускал; нужно для regen_dir и проверок ownership.
      label        — человекочитаемая метка задачи (та же, что в чате).
      workflow     — тип workflow: 'nb_t2i' | 'nb_i2i' | 'gpt_t2i' | 'gpt_i2i' | 'speaker'.
      prompt       — финальный промпт (для speaker — собран из speaker_prompt(gender)).
      params       — все параметры пикеров: model/ratio/resolution/quality/aspect/bg/gender.
      init_paths   — пути в regen_cache (копии файлов, переданных в Phygital как init).
                     Для t2i — пустой список.
      result_path  — путь к скачанной первой картинке-результату в regen_cache.
                     None если результат пока не успели сохранить.
      created_at   — time.time() момента сохранения recipe.
    """

    task_uid: str
    user_id: int
    label: str
    workflow: str
    prompt: str
    params: dict
    init_paths: list[Path] = field(default_factory=list)
    result_path: Optional[Path] = None
    created_at: float = field(default_factory=time.time)

    def age_sec(self) -> float:
        return time.time() - self.created_at


class BotState:
    """Один на процесс. Создаётся в main() через get_state()."""

    def __init__(self) -> None:
        self.session_manager = SessionManager(settings.session_file)
        from cli import load_session  # отложенный импорт, чтобы не было цикла
        self.session = load_session(self.session_manager)

        self.global_sem = asyncio.Semaphore(settings.bot_max_concurrency)

        # Per-user структуры. Создаются лениво в _ensure_user_lane().
        self.user_queues: dict[int, asyncio.Queue[TaskRunner]] = {}
        self.user_workers: dict[int, asyncio.Task] = {}
        self.user_sems: dict[int, asyncio.Semaphore] = {}
        self.user_inflight: Counter[int] = Counter()

        # Recipes & pending edits (для пост-задачных кнопок).
        self.recipes: dict[str, TaskRecipe] = {}
        self.pending_edits: dict[int, tuple[str, float]] = {}
        self._purger_task: Optional[asyncio.Task] = None

        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        # Startup sweep: после рестарта self.recipes пуст → файлы в regen_cache становятся
        # сиротами (кнопки 🔄/✏️/🖼 в старых TG-сообщениях всё равно ответят «устарели»).
        # Сносим всё разом, чтобы не копить мусор от прошлых сессий.
        if REGEN_ROOT.exists():
            shutil.rmtree(REGEN_ROOT, ignore_errors=True)
        REGEN_ROOT.mkdir(parents=True, exist_ok=True)

    # ── tmp dirs ──────────────────────────────────────────────────────────
    def user_tmp(self, user_id: int) -> Path:
        """Корневая папка пользователя — для общих файлов сценария (collected init images)."""
        d = TMP_ROOT / str(user_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def task_tmp(self, user_id: int, task_uid: str) -> Path:
        """Изолированная папка под конкретную задачу. Безопасно чистится в finally."""
        d = TMP_ROOT / str(user_id) / task_uid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def clear_task_tmp(self, user_id: int, task_uid: str) -> None:
        d = TMP_ROOT / str(user_id) / task_uid
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    def clear_user_tmp(self, user_id: int) -> None:
        """Полная очистка папки юзера. Только если у него нет активных задач."""
        if self.user_inflight.get(user_id, 0) > 0:
            return
        d = TMP_ROOT / str(user_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    # ── regen cache (24ч) ─────────────────────────────────────────────────
    def regen_dir(self, user_id: int, task_uid: str) -> Path:
        """Долгоживущая папка под результаты + init задачи (для регенерации/asi2i)."""
        d = REGEN_ROOT / str(user_id) / task_uid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_recipe(self, recipe: TaskRecipe) -> None:
        self.recipes[recipe.task_uid] = recipe
        self._ensure_purger()

    def get_recipe(self, task_uid: str) -> Optional[TaskRecipe]:
        rec = self.recipes.get(task_uid)
        if rec is None:
            return None
        if rec.age_sec() > RECIPE_TTL_SEC:
            self._drop_recipe(task_uid)
            return None
        return rec

    def _drop_recipe(self, task_uid: str) -> None:
        rec = self.recipes.pop(task_uid, None)
        if rec is None:
            return
        d = REGEN_ROOT / str(rec.user_id) / task_uid
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    # ── pending edit prompts ───────────────────────────────────────────────
    def set_pending_edit(self, user_id: int, task_uid: str) -> None:
        self.pending_edits[user_id] = (task_uid, time.time() + PENDING_EDIT_TTL_SEC)

    def pop_pending_edit(self, user_id: int) -> Optional[str]:
        """Возвращает task_uid если у юзера есть свежее pending-edit, иначе None.
        После вызова — pending снимается (одноразовый)."""
        entry = self.pending_edits.pop(user_id, None)
        if entry is None:
            return None
        task_uid, expires_at = entry
        if time.time() > expires_at:
            return None
        return task_uid

    def has_pending_edit(self, user_id: int) -> bool:
        entry = self.pending_edits.get(user_id)
        if entry is None:
            return False
        if time.time() > entry[1]:
            self.pending_edits.pop(user_id, None)
            return False
        return True

    def clear_pending_edit(self, user_id: int) -> None:
        self.pending_edits.pop(user_id, None)

    # ── background purger ─────────────────────────────────────────────────
    def _ensure_purger(self) -> None:
        if self._purger_task is None or self._purger_task.done():
            try:
                self._purger_task = asyncio.create_task(
                    self._purger_loop(), name="recipe-purger"
                )
            except RuntimeError:
                # нет активного event loop — не страшно, поднимется при первом save в рантайме
                self._purger_task = None

    async def _purger_loop(self) -> None:
        log = logger.bind(uid=0, action="purger")
        log.debug("purger started")
        try:
            while True:
                await asyncio.sleep(PURGER_INTERVAL_SEC)
                self._purge_expired()
        except asyncio.CancelledError:
            log.debug("purger cancelled")
            raise

    def _purge_expired(self) -> None:
        now = time.time()
        expired_recipes = [k for k, r in self.recipes.items() if now - r.created_at > RECIPE_TTL_SEC]
        for k in expired_recipes:
            self._drop_recipe(k)
        expired_edits = [u for u, (_, exp) in self.pending_edits.items() if now > exp]
        for u in expired_edits:
            self.pending_edits.pop(u, None)
        if expired_recipes or expired_edits:
            logger.bind(uid=0, action="purger").info(
                f"purged recipes={len(expired_recipes)} pending_edits={len(expired_edits)}"
            )

    # ── per-user lane (queue + worker) ────────────────────────────────────
    def _ensure_user_lane(self, user_id: int) -> tuple[asyncio.Queue, asyncio.Semaphore]:
        """Лениво поднимает очередь, семафор и воркера для пользователя."""
        if user_id not in self.user_queues:
            self.user_queues[user_id] = asyncio.Queue(maxsize=USER_QUEUE_LIMIT)
            self.user_sems[user_id] = asyncio.Semaphore(MAX_PER_USER_INFLIGHT)
            self.user_workers[user_id] = asyncio.create_task(
                self._user_worker(user_id), name=f"user-worker-{user_id}"
            )
        return self.user_queues[user_id], self.user_sems[user_id]

    async def _user_worker(self, user_id: int) -> None:
        """Тянет задачи из очереди и запускает их параллельно (до MAX_PER_USER_INFLIGHT)."""
        q = self.user_queues[user_id]
        sem = self.user_sems[user_id]
        log = logger.bind(uid=user_id, action="user-worker")
        log.debug("worker started")
        try:
            while True:
                runner = await q.get()
                # Acquire per-user семафор. Если занято MAX_PER_USER_INFLIGHT — ждём.
                await sem.acquire()
                # Запускаем задачу в отдельной таске; семафор отпускает она сама.
                asyncio.create_task(
                    self._run_one(user_id, runner, sem, q),
                    name=f"user-task-{user_id}",
                )
        except asyncio.CancelledError:
            log.debug("worker cancelled")
            raise

    async def _run_one(
        self,
        user_id: int,
        runner: TaskRunner,
        sem: asyncio.Semaphore,
        q: asyncio.Queue,
    ) -> None:
        log = logger.bind(uid=user_id, action="user-task")
        self.user_inflight[user_id] += 1
        try:
            await runner()
        except Exception as e:
            log.opt(exception=e).error(f"user-task crashed at lane level: {e!r}")
        finally:
            self.user_inflight[user_id] -= 1
            if self.user_inflight[user_id] <= 0:
                self.user_inflight.pop(user_id, None)
                # Когда последняя задача юзера ушла — снесём корень его tmp.
                self.clear_user_tmp(user_id)
            sem.release()
            q.task_done()

    def user_load(self, user_id: int) -> tuple[int, int]:
        """Возвращает (inflight, queued) для UI/контроля."""
        inflight = self.user_inflight.get(user_id, 0)
        q = self.user_queues.get(user_id)
        queued = q.qsize() if q else 0
        return inflight, queued

    async def submit_task(self, user_id: int, runner: TaskRunner) -> bool:
        """Кладёт задачу в очередь юзера. Возвращает True если приняли, False если очередь полна."""
        q, _ = self._ensure_user_lane(user_id)
        try:
            q.put_nowait(runner)
            return True
        except asyncio.QueueFull:
            return False


_state: Optional[BotState] = None


def get_state() -> BotState:
    global _state
    if _state is None:
        _state = BotState()
    return _state


def reset_state_for_tests() -> None:
    """Сбрасывает singleton — нужно только в тестах."""
    global _state
    _state = None
