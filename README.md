# Phygital-bot

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

| Команда | Сценарий | Параметры в пикере |
|---|---|---|
| `/menu`, `/start` | главное меню (inline-кнопки) | — |
| `/brand_generate` | brand text→image (Gemini Text → Nano Banana) | prompt → ratio → resolution |
| `/brand_img2img` | brand image→image (Gemini Text → Nano Banana с теми же init) | init images → /done → ratio → resolution |
| `/generate` | text→image | prompt → node → (NB: model/ratio/resolution) \| (GPT: quality/aspect/background) |
| `/img2img` | image→image (1-4 init) | init images → /done → prompt → node → та же ветка |
| `/prep_speaker` | портрет спикера в едином стиле | фото → пол → ratio → resolution (NB v3.1, зашитый промпт) |
| `/cancel`, `/help` | служебные | — |

### Brand-сценарии (Cloud.ru Brand Enhancer)

`/brand_generate` и `/brand_img2img` — двухшаговые composer'ы (`workflows/brand_text2img.py`,
`workflows/brand_img2img.py`):

1. **Gemini Text** (нода id=72, `pro_3_1`, `thinking_level=high`) с system-prompt-документом
   из `docs/` → возвращает description.
2. **Nano Banana** (для img2img — с теми же `file_obj_id`, переиспользуем upload) → итоговая
   картинка.

System-prompt-документы лежат в `docs/`:
- `SYSTEM_PROMPT_Gemini3Pro_CloudRu_Enhancer.md` — text→image (v2.9)
- `SYSTEM_PROMPT_Gemini3Pro_CloudRu_Img2Img_Enhancer.md` — image→image (v1.0)

Эти файлы — копии источника в Vault (`Cloud.ru Brand Enhancer` проект). `workflows/brand_docs.py`
аплоадит каждый файл в Phygital storage **один раз**, кэширует `file_obj_id` + `sha256` в
`storage/brand_docs.json`. При изменении содержимого (`sha256` mismatch) — переаплоад автоматически.

`brand_text2img` / `brand_img2img` принимают `progress_cb: Callable[[str], Awaitable[None]]` —
вызывается между Gemini и Nano Banana («Gemini готов, запускаю Nano Banana…»), чтобы юзер
видел смену стадии в long-running цепочке (Gemini Text медиана 26-60s + Nano Banana ~45s).
В `_execute_task` это связано со `status.edit_text` (`bot/scenarios.py`).

**Timeout Gemini Text:** `GeminiTextWorkflow.wait` дефолт = 180s (3× медианы). Залипший таск
быстро `failed`, не держит `global_sem` 5+ минут.

При ошибке Gemini Text composer возвращает `GenerationJob(status="failed", error="Gemini Text: ...")`
с `raw={"gemini": ...}` — `_execute_task` показывает понятную отбивку.

### UX-слой: меню и пост-задачные действия

- **Inline-меню** (`MENU_KEYBOARD`): `/menu` и `/start` шлют клавиатуру с 6 кнопками:
  brand-варианты первыми (`Brand генерация`, `Brand img2img`), затем generic
  (`Сгенерировать`, `Img2img`, `Speaker prep`, `Help`). Кнопки
  `menu:brand_generate|brand_img2img|generate|img2img|prep_speaker` идут *прямо в `entry_points`*
  соответствующих `ConversationHandler` через `CallbackQueryHandler` с нужным `pattern` — то есть
  кнопка эквивалентна вводу команды. `menu:help` обрабатывается отдельным callback (`menu_router`)
  и эхает `HELP_TEXT`.
- **«✖️ Отмена» в пикерах** (`_kb(..., with_cancel=True)`): каждый InlineKeyboard конечного шага
  (выбор ноды / модели / ratio / resolution / quality / background) внизу имеет ряд `cancel:picker`.
  Обработчик — `CallbackQueryHandler(cmd_cancel, pattern=r"^cancel:")` в `fallbacks` всех trex
  conversations: чистит `user_data`, шлёт «отменено», возвращает `ConversationHandler.END`.
- **Действия на готовой картинке** (`_action_keyboard(task_uid, workflow=...)`):
  2-row inline-клавиатура:
  - row 1: `[Повторить]` + (для всех кроме `brand_i2i`) `[Изменить текст]`. У `brand_i2i` нет
    пользовательского prompt — кнопка прячется.
  - row 2: `[В img2img]` + `[В brand img2img]` — две раздельные «продолжалки».

  Callback-data: `regen:{uid}`, `editp:{uid}`, `asi2i:{uid}`, `asbi2i:{uid}`. Живут **24 часа**
  (`RECIPE_TTL_SEC=86400`).
  - `Повторить` → `regen_cb`: достаёт `TaskRecipe` по `task_uid`, диспатчит в `_rerun_from_recipe`,
    суффикс label `(regen)`. Для i2i — копирует init-файлы из `regen_cache` обратно в `task_tmp`.
  - `Изменить текст` → `edit_prompt_cb`: устанавливает `state.set_pending_edit(uid, task_uid)`,
    шлёт исходный prompt в `<pre>`-блоке для копирования. Глобальный `MessageHandler`
    `pending_edit_listener` (группа 1, ниже приоритетом, чем активные conversations) ловит
    следующий текст от юзера → `pop_pending_edit` (одноразовое потребление) → `_rerun_from_recipe`
    с `prompt_override`, label `(edit)`.
  - `В img2img` → `as_i2i_cb`: **это `entry_point` `conv_img2img`** с
    `pattern=r"^asi2i:"`. Колбэк подгружает результат из `regen_cache` в `user_data["init_paths"]`,
    возвращает `I2I_COLLECT` — пользователь сразу может слать ещё init-картинки или `/done`.
  - `В brand img2img` → `as_brand_i2i_cb`: **это `entry_point` `conv_brand_i2i`** с
    `pattern=r"^asbi2i:"`. Аналогично, но возвращает `BI2I_COLLECT` (brand-флоу без prompt-шага).

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
- На входе (`/generate`, `/img2img`, `/prep_speaker`) — capacity-check: если `inflight+queued ≥ 5`, отбивка «слишком много задач, дождись одной из текущих».
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
  scenarios.py            3 ConversationHandler с пикерами + ETA-подсказками
                          + inline-меню + cancel:picker + action_keyboard;
                          _enqueue_task → submit_task в очередь,
                          _execute_task под global_sem с cleanup_paths/task_tmp;
                          _persist_recipe + _rerun_from_recipe для пост-задачных действий;
                          pending_edit_listener (group=1, не перебивает активные conv)
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
1. **Сценарии** — счётчики запусков `/generate`, `/img2img`, `/prep_speaker` + rerun по workflow.
2. **Результаты задач** — completed/total, средняя/медианная длительность, картинок отдано.
3. **HTTP-запросы** — счётчики по endpoint'ам (queue-position бакетируется в один, поэтому polling
   не зашумляет картину).
4. **Промпты** — топ-20 по частоте + полная разбивка по сценариям (тексты, число повторов).

Stdlib-only, без зависимостей. Корреляции «запрос ↔ конкретный сценарий» нет — логи Phygital-вызовов
живут с `uid=-` (общий клиент). Если нужно — это отдельный кусок работы (структурный лог либо tagged
context manager на `_request`).

## Тесты

- `tests/test_bot_emulation.py` — реальный эмуляционный ран ConversationHandler-ов с моками
  `cli.load_session` и `bot.auth._is_allowed`. Покрывает: меню → conv через CallbackQuery,
  полный t2i-сценарий, регистрацию recipe + action keyboard, regen / edit-prompt / asi2i,
  cancel:picker, expired recipe, capacity-check, persist init-файлов в `regen_cache` при i2i.
  Запуск (после активации venv): `python -m tests.test_bot_emulation`.

## Что дальше

- Накопить реальные тайминги в `logs/bot.log` (`dur=N.Ns`) и обновить ETA-константы
  в `bot/scenarios.py` (сейчас взяты из `averageTimeInSeconds` нод-регистра)
- Дополнительные сценарии: batch-обработка папки, video-workflow, virality predictor, upscale
