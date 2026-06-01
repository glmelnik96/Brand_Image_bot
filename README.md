# Cloud.ru Image Bot

Telegram-бот + async Python-обёртка над приватным API Phygital+ (`app.phygital.plus`) для скриптовой
генерации изображений на платной подписке. Публичного API нет — клиент собран по recon HAR-капчам.

> Назначение — автоматизация собственного аккаунта (RPA-стиль). Бот работает для владельца + 5
> коллег по whitelist'у; снизу — CLI для отладки и батчей.

## Telegram-бот (`python -m bot.main`)

Запуск (после активации venv): `python -m bot.main`. Whitelist в env `PHYGITAL_BOT_WHITELIST`
(id через запятую).

> Команды ниже даны в нейтральной форме (`python …`) — предполагается, что venv активирован
> (`.venv\Scripts\activate` под Windows / `source .venv/bin/activate` под macOS/Linux).
> Если venv не активирован — подставь полный путь к интерпретатору: `.venv\Scripts\python.exe`
> на Windows или `.venv/bin/python` на macOS/Linux.

Управление — через единое меню. Слэш-команды-ярлыки сценариев убраны (2026-05-20),
чтобы был один путь — кнопки.

| Команда | Назначение |
|---|---|
| `/menu`, `/start` | главное меню |
| `/cancel` | выйти из текущего сценария / закрыть пикер |
| `/help` | справка |

Структура меню:
- **Создай изображение**
  - **Бренд изображения** — два шага (Gemini Text c вариантным system-prompt → Nano Banana). Варианты:
    - **Photo** — фотореалистичный брендовый стиль
    - **Render** — 3D-объекты и продуктовые рендеры
    - **2d Isometry** — 2D-изометрические сцены и иллюстрации
  - **Обычное изображение** — text→image напрямую в Nano Banana, без Gemini
- **Изменить изображение**
  - **Изменить изображение** — img2img: 1–4 исходника + новый текст
  - **Добавить Brand patterns** — brand img2img (Gemini сам опишет картинки)
- **Фотография спикера** — портрет спикера по референсу
- **Помощь** — справка

### Brand-сценарии (Cloud.ru Brand Enhancer)

Brand text→image (`workflows/brand_text2img.py`) и brand img2img (`workflows/brand_img2img.py`) —
двухшаговые composer'ы:

1. **Gemini Text** (нода id=72, `pro_3_1`, `thinking_level=high`) с system-prompt-документом
   из `docs/` → возвращает description.
2. **Nano Banana** (для img2img — с теми же `file_obj_id`, переиспользуем upload) → итоговая
   картинка.

System-prompt-документы лежат в `docs/`:
- `SYSTEM_PROMPT_Gemini3Pro_CloudRu_Photo_Enhancer.md` — text→image, вариант **Photo**
- `SYSTEM_PROMPT_Gemini3Pro_CloudRu_Render_Enhancer.md` — text→image, вариант **Render**
- `SYSTEM_PROMPT_Gemini3Pro_CloudRu_Isometric_Enhancer.md` — text→image, вариант **2d Isometry**
- `SYSTEM_PROMPT_Gemini3Pro_CloudRu_Img2Img_Enhancer.md` — image→image
- `SYSTEM_PROMPT_Gemini3Pro_NanoBanana_Scrubber.md` — surgical-cleaner, чистит флагнутые
  Nano-Banana safety-классификатором слова из description, сохраняя смысл.

`run_brand_text2img(..., variant="photo"|"render"|"isometric")` выбирает соответствующий .md.
В `TaskRecipe.params` сохраняется `variant`, чтобы regen использовал тот же system-prompt.

Эти файлы — копии источника в Vault (`Cloud.ru Brand Enhancer` проект). `workflows/brand_docs.py`
аплоадит каждый файл в Phygital storage **один раз**, кэширует `file_obj_id` + `sha256` в
`storage/brand_docs.json`. При изменении содержимого (`sha256` mismatch) — переаплоад автоматически.
То есть можно править .md без правок кода: следующий запрос подхватит новую версию.

#### Safety-retry loop (brand_t2i)

Nano Banana иногда отдаёт `error_params` со словом-нарушителем в payload
(`Prompt is rejected by safety system. Remove harmful word \`knob\` from the prompt`).
В `workflows/brand_text2img.py` для этого есть retry-loop:

1. `_extract_flagged_word(error)` парсит слово из текста ошибки.
2. `_scrub_prompt()` запускает второй Gemini-Text проход с системным промптом
   `SYSTEM_PROMPT_Gemini3Pro_NanoBanana_Scrubber.md` (доку получаем через `get_scrubber_doc`),
   передавая ему `FLAGGED WORD: <word>\n\nPROMPT TO FIX:\n<prompt>`. Scrubber хирургически
   подменяет слово (`knob`→`disc`, `joystick`→`tall post` и т.п.), оставляя остальное.
3. Чистый prompt отправляется обратно в Nano Banana.

Цикл — до `MAX_SCRUB_RETRIES = 2` попыток. Если после второй всё ещё `error_params` — задача
завершается `failed` с понятным сообщением. На реальном API проверено в `tests/test_real_api.py:safety_retry`
(см. `tests/results/run-*/safety_retry/status_log.json`).

#### Progress callback

`brand_text2img` / `brand_img2img` принимают `progress_cb: Callable[[str], Awaitable[None]]` —
вызывается **с именами этапов** (а не свободным текстом):
`"Gemini Text"` → `"Nano Banana"` → (опционально `"Чистка safety-words: «knob» (попытка N)"`
→ `"Nano Banana (повтор)"`). `_execute_task` (`bot/scenarios.py`) прокидывает эти строки в
`StatusReporter.step(...)` — он сам подмешивает elapsed-time и накапливает breakdown для
финального сообщения. См. ниже секцию «StatusReporter».

**Timeout Gemini Text:** `GeminiTextWorkflow.wait` дефолт = 180s (3× медианы). Залипший таск
быстро `failed`, не держит `global_sem` 5+ минут.

При ошибке Gemini Text composer возвращает `GenerationJob(status="failed", error="Gemini Text: ...")`
с `raw={"gemini": ...}` — `_execute_task` показывает понятную отбивку.

### StatusReporter (live-статус задачи)

`bot/status_reporter.py:StatusReporter` — единая точка живой обратной связи по одной задаче.
Подключается в `_enqueue_task` сразу после создания статус-сообщения, отвечает за весь
жизненный цикл до `done`/`error`. Состояния:

| Метод | Текст в чате |
|---|---|
| `queued(queue_pos=N)` | `{label} — принято` / `очередь №N • 0:00 / ~{eta}` |
| `waiting_slot()` | `{label} — в очереди` / `жду свободный слот • {elapsed} / ~{eta}` |
| `start(first_step)` | `{label} — генерация` / `{first_step} • {elapsed} / ~{eta}` |
| `step(name)` | `{label} — генерация` / `{name} • {elapsed} / ~{eta}` |
| `done(job_id=…)` | `{label} — готово` + breakdown по этапам + `job {id}` |
| `error(msg)` / `crashed(msg)` | `{label} — ошибка` / краткое сообщение |

Тикер обновляет elapsed раз в `TICK_SECONDS=3.0` в фоновой `asyncio.Task` (запускается в
`start`, останавливается в `done/error/crashed`). `step()` записывает длительность
**предыдущего** этапа в `self._step_times`, который рендерится в финальном сообщении в виде
`Gemini Text — 0:30 · Nano Banana — 0:55 · ИТОГО — 1:25`. Декоративные эмодзи не используются.

Workflow → первый этап выбирается через `_FIRST_STEP_BY_WORKFLOW` (`bot/scenarios.py`).
`brand_*` начинают с `"Gemini Text"`, остальные — `"Nano Banana"`. Имена этапов приходят
из `progress_cb` (см. выше), `StatusReporter` сам ничего о пайплайне не знает.

### UX-слой: меню и пост-задачные действия

- **Inline-меню** — иерархия (см. выше). Корень (`MENU_KEYBOARD` = `_menu_root_kb()`),
  подменю `make`/`make_brand`/`edit` — функции в `bot/scenarios.py`. Навигация между
  подменю и кнопка «Назад» обрабатываются `menu_router` (паттерн
  `^menu:(root|make|make_brand|edit|help)$`) через `edit_message_text` — пользователь
  не получает простыни новых сообщений.
  - Entry-кнопки сценариев (`menu:generate`, `menu:img2img`, `menu:brand_img2img`,
    `menu:brand_{photo,render,isometric}`, `menu:prep_speaker`) идут *прямо в `entry_points`*
    `ConversationHandler` через `CallbackQueryHandler` — они до `menu_router` не доходят.
  - `menu:help` → `menu_router` → отправка `HELP_TEXT` отдельным сообщением.
- **«Отмена» в пикерах** (`_kb(..., with_cancel=True)`): каждый InlineKeyboard конечного шага
  (ratio / resolution / gender) внизу имеет ряд `cancel:picker`. Обработчик —
  `CallbackQueryHandler(cmd_cancel, pattern=r"^cancel:")` в `fallbacks` всех conversations:
  чистит `user_data`, шлёт «отменено», возвращает `ConversationHandler.END`.
- **Действия на готовой картинке** (`_action_keyboard(task_uid, workflow=...)`) —
  раскладка зависит от сценария:

  | Сценарий (workflow) | Кнопки |
  |---|---|
  | `nb_t2i` (Обычное изображение) | Повторить · Изменить текст · Изменить изображение · Добавить Brand patterns |
  | `brand_t2i` (Photo/Render/Isometry) | Повторить · Изменить изображение |
  | `nb_i2i`, `brand_i2i`, `speaker` | Повторить |

  Callback-data осталась та же: `regen:{uid}`, `editp:{uid}`, `asi2i:{uid}` (label
  «Изменить изображение»), `asbi2i:{uid}` (label «Добавить Brand patterns»).
  Живут **24 часа** (`RECIPE_TTL_SEC=86400`).
  - `Повторить` → `regen_cb`: достаёт `TaskRecipe` по `task_uid`, диспатчит в `_rerun_from_recipe`,
    суффикс label `(regen)`. Для i2i — копирует init-файлы из `regen_cache` обратно в `task_tmp`.
    Для brand_t2i — берёт `variant` из `params`.
  - `Изменить текст` → `edit_prompt_cb`: устанавливает `state.set_pending_edit(uid, task_uid)`,
    шлёт исходный prompt в `<pre>`-блоке для копирования. Глобальный `MessageHandler`
    `pending_edit_listener` (группа 1, ниже приоритетом, чем активные conversations) ловит
    следующий текст от юзера → `pop_pending_edit` (одноразовое потребление) → `_rerun_from_recipe`
    с `prompt_override`, label `(edit)`. Доступна только под результатом `nb_t2i`.
  - `Изменить изображение` (callback `asi2i:`) → `as_i2i_cb`: **`entry_point` `conv_img2img`**.
    Подгружает результат из `regen_cache` в `user_data["init_paths"]`, возвращает `I2I_COLLECT`.
  - `Добавить Brand patterns` (callback `asbi2i:`) → `as_brand_i2i_cb`:
    **`entry_point` `conv_brand_i2i`**. Аналогично, но `BI2I_COLLECT` (без prompt-шага).
    Доступна только под результатом `nb_t2i`.

### Startup broadcast

`bot/main.py:_startup_broadcast` при запуске берёт текст `STARTUP_BROADCAST_TEXT`,
считает его `sha256[:12]`, сверяется с маркером `storage/startup_broadcast.<digest>.flag`.
Если такого файла нет — шлёт сообщение всем `settings.allowed_user_ids`, пишет маркер
с отчётом (sent/failed). Меняешь текст → меняется digest → следующий запуск разошлёт
повторно. Без смены текста рестарт ничего не шлёт.

### TaskRecipe и regen_cache

- `TaskRecipe` (`bot/state.py`): dataclass с `task_uid`, `user_id`, `label`, `workflow`
  (`nb_t2i|nb_i2i|gpt_t2i|gpt_i2i|speaker|brand_t2i|brand_i2i`), `prompt`, `params: dict`, `init_paths: list[Path]`,
  `result_path: Path`, `created_at: datetime`.
- Хранится в памяти процесса (`BotState.recipes: dict[task_uid → TaskRecipe]`). При рестарте
  бота — теряется (это сознательно: 24-часовая «memory of last result»).
- **`bot/regen_cache/<uid>/<task_uid>/`** — файлы, нужные для повторного запуска: копии init-картинок
  + готовый результат. Хранятся 24 ч.
- Очистка — три уровня:
  1. **Startup sweep** в `BotState.__init__`: `shutil.rmtree(REGEN_ROOT)` при старте процесса.
     После рестарта `self.recipes` пуст → файлы на диске становятся сиротами (кнопки в старых
     TG-сообщениях всё равно ответят «параметры устарели»), сносим всё разом.
  2. **Lazy** в `get_recipe(task_uid)`: если `age > 24ч` — `_drop_recipe` + `rmtree`.
  3. **Фоновый purger** `_purger_loop` (запускается при первом `save_recipe`, интервал
     `PURGER_INTERVAL_SEC=600`). На каждом тике сносит recipes старше 24 ч + их `regen_cache/`.
- `set_pending_edit(uid, task_uid)`/`pop_pending_edit(uid)` — separate dict с TTL
  `PENDING_EDIT_TTL_SEC=300` (5 мин). Pop-once семантика: чтение очищает запись, защита от
  повторного триггера.

Две ноды (`/api/v2/nodes/`): **Nano Banana** (Gemini Image API, id=94, ~3.5 мин/картинка) и
**GPT Image 2** (id=98, ~8.5 мин/картинка). ETA берётся из `averageTimeInSeconds` и показывается
на выборе ноды + в статусе задачи.

**Лимиты параллелизма:**
- `Semaphore(5)` глобально (`settings.bot_max_concurrency`).
- На пользователя: до **2 задач одновременно** + FIFO-очередь до **5 слотов** (включая активные).
- Каждый user имеет persistent `asyncio.Task`-воркер, который тянет из своей `asyncio.Queue` и пускает задачи через per-user `Semaphore(2)` и общий глобальный.
- На входе каждого сценария (entry-points в `bot/scenarios.py`) — capacity-check: если `inflight+queued ≥ 5`, отбивка «очередь заполнена».
- Каждая задача работает в изолированном `bot/tmp/<uid>/<task_uid>/` — соседняя задача того же юзера не трогает её файлы; чистится в `finally`.

## CLI (для отладки и батчей)

| Сценарий | CLI |
|---|---|
| **Text-to-image** (Nano Banana v3) | `python -m cli generate "prompt" -n N` |
| **Img2img generic** (один или несколько reference) | `python -m cli generate "prompt" --init-img a.jpg --init-img b.jpg` |
| **Speaker portrait prep** (Nano Banana 3.1, 3:4, 2K, зашитый промпт под чёрное худи) | `python -m cli prep-speaker photo.jpg [--gender man\|woman] [--reference path]` |
| **Сессия — статус / refresh** | `python -m cli session info`, `python -m cli session refresh` |

Все генерации идут параллельно (`-n N`), скачиваются в `outputs/`.

## Архитектура

```
recon/                    Playwright HAR capture + анализ HAR
client/
  session.py              SuperTokens cookies → access/refresh, persist в storage/
                          + jwt_ttl_seconds() + auto-fallback к recon-дампу при 418
  auth.py                 /auth/session/refresh (header-mode)
  api.py                  httpx-клиент (HTTP/2 + truststore), upload, submit, polling
                          + retry с backoff на 5xx и httpx pool/timeout errors
workflows/
  base.py                 Workflow ABC (build_payload → submit → wait)
  image_gen.py            Text-to-image (workflow id 94, Nano Banana)
  image_to_image.py       Img2img + автонормализация входов (HEIC, EXIF, RGBA, 2048-cap)
  speaker_prep.py         Пресет «портрет спикера», v3_1 / r_3_4 / k2, зашитые промпты
  gpt_image.py            GPT Image API (workflow id 98), submit/config_history по HAR
  gemini_text.py          Gemini Text (workflow id 72), pro_3_1 + thinking_level=high; wait timeout=180s
  brand_docs.py           docs/*.md → Phygital storage (upload + sha256-кэш в storage/brand_docs.json)
  brand_text2img.py       Composer: Gemini Text (с system-prompt-doc) → Nano Banana, progress_cb между шагами
  brand_img2img.py        Composer: один upload init → Gemini Text → Nano Banana (те же file_obj_id)
bot/
  main.py                 Application factory; split HTTPXRequest pools (poll=1, action=20);
                          DER/PEM fallback для kwts.pem; loguru file sink;
                          pre-flight refresh при _post_init;
                          регистрация menu/regen/edit-listener в group 0/1
  state.py                BotState: SessionManager, global Semaphore(5),
                          per-user queue + worker + Semaphore(2),
                          task_tmp(uid, task_uid) изоляция, capacity-check API;
                          TaskRecipe + regen_cache + pending_edits + purger-task
  auth.py                 @whitelist_only по PHYGITAL_BOT_WHITELIST
  status_reporter.py      StatusReporter: live-статус задачи с elapsed time + breakdown
                          по этапам, фоновый тикер (3 сек), без декоративных эмодзи
  scenarios.py            5 ConversationHandler (generate / img2img / brand_t2i / brand_i2i /
                          prep_speaker) с пикерами + ETA-подсказками + inline-меню +
                          cancel:picker + action_keyboard; _enqueue_task создаёт
                          StatusReporter и кладёт задачу в per-user queue;
                          _execute_task под global_sem с cleanup_paths/task_tmp;
                          _persist_recipe + _rerun_from_recipe для пост-задачных действий;
                          pending_edit_listener (group=1, не перебивает активные conv);
                          _FIRST_STEP_BY_WORKFLOW → начальный этап для StatusReporter
  tmp/<uid>/<task_uid>/   изолированный рабочий каталог одной задачи (auto-cleanup)
  regen_cache/<uid>/      24-часовый кэш init+result для повторных запусков
cli.py                    argparse: generate, prep-speaker, session
```

### Поток одной генерации

```
POST /api/v2/tasks/                                      → task_id
POST /api/v2/tasks/config_history                        ← обязателен, иначе pending навсегда
GET  /api/v2/tasks/queue-position/<id>  (polling 1.5с)   → status=done
POST /api/v2/storage-object/.../download-links           → S3 urls
```

Img2img дополнительно:
```
POST /api/v2/storage-object/storage-object  (multipart, поле fileobject)  → file_obj_id
```
Подставляется в `inputs.init_img.value=[fid, ...]` с параллельным `meta.dimensions=[{h,w}, ...]`.

## Setup

**Windows (PowerShell / cmd):**
```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt        # httpx[http2], truststore, loguru,
                                       # playwright, ijson, Pillow, pillow-heif, pydantic
playwright install chromium
```

**macOS / Linux:**
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Дальше команды в README даны в нейтральной форме (`python …`) — venv предполагается активированным.

## Bootstrap сессии

> **Авторизация — только через recon.** У Phygital+ нет публичного API и нет login-by-password
> ручки. Cookies/JWT попадают в `storage/session.json` **только** после прогона
> `python -m recon.capture` — Playwright откроет Chromium, в нём ты логинишься руками своим
> аккаунтом, recon экспортирует cookies. Без этого ни бот, ни CLI, ни тесты не стартуют.
>
> **Смена аккаунта** = снести старое состояние и прогнать capture заново:
> ```bash
> rm storage/session.json
> rm -rf user_data/          # persistent-профиль Chromium с залогиненным юзером
> python -m recon.capture    # залогиниться новым аккаунтом
> ```
> Никаких `PHYGITAL_EMAIL/PASSWORD` в `.env` нет — это конфиг не используется кодом
> (фантомные поля из ранней версии). Аутентификация всегда идёт через сохранённую
> SuperTokens-сессию + auto-refresh.

1. **Полный capture (если профиля ещё нет):**
   ```bash
   python -m recon.capture
   ```
   В открывшемся Chromium залогиниться → сделать одну генерацию → Enter в терминале.
   Появится `recon/captures/storage-<ts>.json` и `phygital-<ts>.har`. Эти файлы — bootstrap-источник
   для `storage/session.json`.

2. **Headless-refresh (когда `user_data/` уже авторизован):**
   ```bash
   python -m recon.refresh_capture
   ```
   Открывает persistent-профиль в headless, ждёт `networkidle`, дампит свежие cookies. Заняло ~8 сек.

3. **После любого capture — обнулить старую сессию, чтобы CLI подхватил свежий дамп:**
   ```bash
   # Windows (PowerShell):  Remove-Item storage\session.json -ErrorAction Ignore
   # macOS/Linux:           rm -f storage/session.json
   ```

`SessionManager` сам обновит `st-access-token` через `/auth/session/refresh` при 401/418
от любого запроса. Refresh защищён `asyncio.Lock`.

### `user_data/` — Playwright persistent profile (recon)

`recon/capture.py` и `recon/refresh_capture.py` запускают Chromium через
`launch_persistent_context(user_data_dir=ROOT/"user_data")` — это `--user-data-dir` Chromium'а
со всем юзерским состоянием (cookies, localStorage, IndexedDB, ShaderCache, GraphiteDawnCache,
профиль `Default/`). Логин на `app.phygital.plus` сохраняется между recon-запусками — без этого
каждый capture начинался бы с пустого браузера.

- **Используется только recon-pipeline'ом**, бот к нему не обращается.
- **Никем не управляется по размеру** (растёт за счёт Chromium-кэшей). 100–200 МБ — нормально.
- В `.gitignore`, никогда не коммитится (содержит куки логина).
- Удаление = повторный onboarding recon (нужно будет залогиниться руками при следующем `recon.capture`).

### Устойчивость сессии (в боте)

`bot/main.py` при старте делает **pre-flight refresh** (`_preflight_session`): если access-JWT
доживёт ≥15 мин — пропускаем; иначе пробуем refresh; при `RefreshError 418` — `SessionManager.refresh()`
автоматически ищет в `recon/captures/storage-*.json` дамп новее текущей сессии с непросроченным
JWT и переключается на него прозрачно. Если даже это не помогло — `SystemExit` с инструкцией.

**Почему важно:** SuperTokens рoтирует refresh-token. Параллельный браузерный таб на
`app.phygital.plus` или одновременный `recon/capture.py` продвинут серверную цепочку, и наш
сохранённый refresh-token станет невалидным («v0» против серверного «v1»). Без pre-flight это
ловилось бы серией одинаковых ошибок в чат уже на работающих задачах.

`save()` обновляет `captured_at` при каждом успешном refresh — по полю всегда видна свежесть.

### Параллелизм и изоляция задач

Иерархия лимитов:

```
                global_sem = Semaphore(5)
                       ▲
                       │ acquire в _execute_task
                       │
   user A          user B          ...
   queue(maxsize=5) queue(maxsize=5)
   sem(2)          sem(2)
   worker-task     worker-task
```

- **Per-user FIFO**: `BotState.user_queues[uid]` — `asyncio.Queue(maxsize=5)`. Лениво создаётся в `_ensure_user_lane`.
- **Per-user воркер**: `_user_worker(uid)` — persistent `asyncio.Task`, в цикле `await q.get()` → `await sem.acquire()` → `asyncio.create_task(_run_one)`. Семафор отпускается в `finally` задачи.
- **Per-user inflight=2**: `user_sems[uid] = Semaphore(2)`. Юзер может крутить 2 задачи параллельно, остальные ждут в очереди.
- **Global inflight=5**: `global_sem` берётся уже внутри `_execute_task` под Phygital submit/poll, чтобы запуск пары пользователей не «съел» весь глобальный бюджет до того, как кто-то третий получит хотя бы одно место.
- **Entry-point capacity check** (`_check_user_capacity`): на старте каждого сценария (`gen_start`, `i2i_start`, `sp_start`) считает `(inflight + queued)`. Если ≥ 5 — `ConversationHandler.END` с отбивкой; иначе пускаем в пикеры.
- **Per-task tmp** (`state.task_tmp(uid, task_uid)`): каждой задаче — свой uuid-subdir `bot/tmp/<uid>/<uuid>/`. В `_execute_task.finally` удаляется через `clear_task_tmp`. Корень `bot/tmp/<uid>/` зачищается только когда `user_inflight[uid] == 0` (`clear_user_tmp`).
- **Cleanup of init files**: `_enqueue_task` принимает `cleanup_paths` — список путей `init images`/`photo`, скачанных на этапе пикера и лежащих в корне `bot/tmp/<uid>/`. Удаляются в `_execute_task.finally`, после успешной/неуспешной задачи.

### ETA-константы (откалиброванные)
- Nano Banana: **45s** (медиана из `logs/bot.log` первого рана, было 200s по `averageTimeInSeconds`).
- GPT Image: **500s** (~8.5 мин, пока не калибровалось — мало данных).

Показывается на выборе ноды, в статусе задачи (`в очереди… (обычно ~X)` → `генерирую… (обычно ~X)`), в `/help`.

## Подводные камни (грабли в коде)

- `POST /api/v2/tasks/` без последующего `config_history` оставляет таск в `pending` навсегда.
- `outputs: [{"name":"image","type":"array","value":""}]` в payload — пустой массив не годится.
- Multipart upload требует поле именно `fileobject` (иначе 422 `body.fileobject: missing`).
- HTTP/2 рвёт большие multipart-тела через ~1 сек (`httpx.ReadError`) → upload-клиент форсированно
  на HTTP/1.1, таймаут 300 сек.
- UI ресайзит входные картинки до ≤2048 по длинной стороне + RGB JPEG-90 перед заливкой.
  `_prepare_for_upload` повторяет это: HEIC через `pillow_heif`, `ImageOps.exif_transpose`,
  RGBA сводится на белый фон.
- SuperTokens возвращает **418 «try refresh»** (а не только 401) → оба статуса триггерят авто-refresh.
- corporate/MITM CA: `truststore.SSLContext` → системный keychain (certifi-бандла недостаточно).
- **Отправка результата в TG**: сначала `reply_photo(url)` (Telegram сам тянет S3). На любой фейл —
  fallback `httpx.AsyncClient(verify=_SSL_CTX, ...).get(url)` → upload bytes. `verify=_SSL_CTX` —
  тот же truststore-контекст, что и в `client/api.py`; без него под Cloud.ru MITM проксёй падает
  `SSL: CERTIFICATE_VERIFY_FAILED`. На >10 МБ — switch с photo на document.

## Логи и выжимки

- `logs/bot.log` — основной лог (loguru, level=DEBUG, ротация 20 MB × 5).
- `bot/scenarios.py` пишет промпт в виде `prompt received: chars=N prompt='...'` (text-to-image,
  img2img) и `rerun: workflow=... chars=N prompt='...'` (regen/edit). Переводы строк в промпте
  заменяются на `\n` чтобы каждая запись помещалась в одну строку — нужно для парсера.
- `client/api.py:_request` пишет `DEBUG | METHOD URL` на каждый HTTP-вызов Phygital.

### `tools/digest.py` — Markdown-выжимка

```bash
python -m tools.digest                      # все доступные логи
python -m tools.digest --since 24h          # последние сутки
python -m tools.digest --since 7d           # неделя
python -m tools.digest --since 2026-05-13   # с даты
python -m tools.digest --out reports/week.md
```

Что в отчёте:
1. **Сценарии** — счётчики запусков по workflow (`nb_t2i`/`nb_i2i`/`brand_t2i`/`brand_i2i`/`speaker`) + rerun.
2. **Результаты задач** — completed/total, средняя/медианная длительность, картинок отдано.
3. **HTTP-запросы** — счётчики по endpoint'ам (queue-position бакетируется в один, поэтому polling
   не зашумляет картину).
4. **Промпты** — топ-20 по частоте + полная разбивка по сценариям (тексты, число повторов).

Stdlib-only, без зависимостей. Корреляции «запрос ↔ конкретный сценарий» нет — логи Phygital-вызовов
живут с `uid=-` (общий клиент). Если нужно — это отдельный кусок работы (структурный лог либо tagged
context manager на `_request`).

## Тесты

Два эшелона: **mocked harness** (быстро, без API) и **real-API harness** (тратит кредиты).

### `tests/test_bot_emulation.py` — mocked harness (12 сценариев)

Эмуляционный прогон ConversationHandler'ов с моками `cli.load_session`, `bot.auth._is_allowed`,
`PhygitalClient`, workflow-классов. Меню → conv через CallbackQuery, без сетевых вызовов.

Покрытие:
1. `menu:help` callback
2. полный t2i + все пост-задачные кнопки (regen / edit / asi2i / asbi2i) + cancel + capacity-check
3. img2img recipe сохраняет init-файлы в `regen_cache`
4. brand_t2i полный флоу + regen (variant=photo)
5. brand_i2i полный флоу + kb без «Изменить текст» + regen
6. brand_t2i variant=render
7. brand_t2i variant=isometric
8. prep_speaker полный флоу + cleanup_paths
9. **safety-scrubber retry loop** — мокаем Gemini/Nano так, что Nano первый раз отдаёт
   `error_params: 'knob'`, проверяем что Gemini-scrubber вызван (с doc_id скраббера) и
   что результат retry-Nano = completed. Проверяет последовательность progress-событий.
10. cancel:picker в разных FSM-стейтах (GEN_RATIO / I2I_PROMPT / SP_GENDER)
11. `StatusReporter.queued/waiting_slot/start/step/done/error` — формат сообщений
12. whitelist reject — non-whitelist user получает reject-сообщение, в FSM не входит

Запуск: `python -m tests.test_bot_emulation`. Все 12 проходят за ~2 сек.

### `tests/test_real_api.py` — real-API harness (тратит кредиты)

Прямые вызовы Phygital через `PhygitalClient` (harness-level, без Telegram). Использует
`storage/session.json`; сессия рефрешится автоматически. Артефакты — в
`tests/results/run-{YYYYMMDD-HHMMSS}/{scenario}/`:

```
result.{jpg,png}        — финальное изображение
prompt.txt              — итоговый prompt + Gemini description если был
status_log.json         — поток progress-events + final job status
timing.json             — wall-clock тайминги: start, end, total, per-step
```

Сценарии: `nb_t2i`, `nb_i2i`, `brand_t2i_photo`, `brand_t2i_render`, `brand_t2i_isometric`,
`brand_i2i`, `speaker`, `safety_retry` (намеренно триггерит slovo «knob»). После прогона —
`summary.json` в корне run-папки.

Бюджет: ~600-800 кредитов на полный прогон (зависит от текущих цен нод). Запуск:
`python -m tests.test_real_api`.

> Подсказка: чтобы дёшево проверить session/auth — запусти один сценарий вручную через
> `import tests.test_real_api` и вызови `run_scenario_nb_t2i(client, dir)` (см. smoke-блок
> в истории).

## Что дальше

- Накопить реальные тайминги в `logs/bot.log` (`dur=N.Ns`) и обновить ETA-константы
  в `bot/scenarios.py` (сейчас взяты из `averageTimeInSeconds` нод-регистра)
- Дополнительные сценарии: batch-обработка папки, video-workflow, virality predictor, upscale
