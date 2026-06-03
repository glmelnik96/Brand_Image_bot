"""tools/digest.py — выжимка по логам бота.

Парсит `logs/bot.log` (и старые ротации `logs/bot.log.YYYY*`), выдаёт Markdown:
  1) Сценарии — счётчики запусков /generate, /img2img, /prep_speaker, regen, edit-prompt.
  2) HTTP-запросы — счётчики по endpoint'ам Phygital (агрегируются template-pattern'ом,
     queue-position/<id> схлопывается в один бакет).
  3) Промпты — список с частотой по сценариям.

Зависимостей нет — только stdlib. Запуск (после активации venv):
    python -m tools.digest                  # все доступные логи
    python -m tools.digest --since 24h      # за последние 24ч
    python -m tools.digest --since 7d
    python -m tools.digest --since 2026-05-13
    python -m tools.digest --logs logs/bot.log
    python -m tools.digest --out reports/digest-week.md

Что НЕ покрыто:
  - Корреляция «запрос → сценарий» (логи не несут task→scenario связи). Поэтому
    HTTP-запросы агрегируются глобально, без разбивки по сценариям.
  - Тела запросов/ответов (не пишутся в лог).
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"

# Формат loguru-строки:
#   2026-05-13 14:41:34.724 | DEBUG    | uid=438074662 | bot.scenarios:gen_prompt:313 | <msg>
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})"
    r"\s+\|\s+(?P<level>\w+)\s+\|\s+uid=(?P<uid>\S+)"
    r"\s+\|\s+(?P<src>[\w.]+):(?P<func>[\w<>_]+):(?P<line>\d+)"
    r"\s+\|\s+(?P<msg>.*)$"
)

# Старт сценариев в логах (по {func}, см. bot/scenarios.py).
SCENARIO_STARTS = {
    "gen_start": "/generate",
    "i2i_start": "/img2img",
    "sp_start": "/prep_speaker",
}

# Прямой источник промпта по {func} — для группировки промптов.
PROMPT_FUNC_TO_SCENARIO = {
    "gen_prompt": "/generate",
    "i2i_prompt": "/img2img",
    "_rerun_from_recipe": "rerun",  # уточнится по workflow в самом сообщении
}

# HTTP-метод+URL → имя бакета. Порядок важен.
ENDPOINT_BUCKETS = [
    (re.compile(r"GET\s+\S+/api/v2/tasks/queue-position/\d+"), "GET queue-position/<id>"),
    (re.compile(r"POST\s+\S+/api/v2/tasks/config_history"), "POST tasks/config_history"),
    (re.compile(r"POST\s+\S+/api/v2/tasks/$"), "POST tasks/ (submit)"),
    (re.compile(r"POST\s+\S+/api/v2/storage-object/storage-object$"), "POST storage-object (upload)"),
    (re.compile(r"POST\s+\S+/api/v2/storage-object/.+/download-links"), "POST download-links"),
    (re.compile(r"POST\s+\S+/api/v2/nodes/get_credits_price"), "POST nodes/get_credits_price"),
    (re.compile(r"POST\s+\S+/auth/session/refresh"), "POST /auth/session/refresh"),
]
HTTP_LINE_RE = re.compile(r"^(GET|POST|PUT|DELETE|PATCH)\s+https?://")

# Извлечение `prompt='...'` из сообщения; работает с одинарными или двойными кавычками.
PROMPT_RE = re.compile(r"prompt=(?P<q>['\"])(?P<text>.*?)(?<!\\)(?P=q)")
WORKFLOW_RE = re.compile(r"workflow=(\w+)")
TASK_RESULT_RE = re.compile(r"job result status=(?P<status>\w+)\s+urls=(?P<urls>\d+)\s+dur=(?P<dur>[\d.]+)s")
# admin-stat: gen_done uid=438074662 uname='glmelnik96' workflow=brand_t2i wf_count=3 user_total=12 urls=1 dur=58.7s
ADMIN_STAT_RE = re.compile(
    r"admin-stat:\s+gen_done\s+uid=(?P<uid>\d+)\s+uname=(?P<q>['\"])(?P<uname>.*?)(?<!\\)(?P=q)"
    r"\s+workflow=(?P<workflow>\w+)\s+wf_count=\d+\s+user_total=\d+"
)


def parse_since(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    now = datetime.now()
    if s.endswith("h"):
        return now - timedelta(hours=int(s[:-1]))
    if s.endswith("d"):
        return now - timedelta(days=int(s[:-1]))
    # YYYY-MM-DD
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"--since: ожидаю '24h', '7d' или 'YYYY-MM-DD', получил {s!r}")


def discover_logs(custom: list[str] | None) -> list[Path]:
    if custom:
        return [Path(p) for p in custom]
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob("bot.log*"))
    # Свежий — последний; loguru пишет ротации как bot.log.2026-05-...
    return files


def iter_lines(paths: list[Path], since: datetime | None):
    for p in paths:
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                m = LINE_RE.match(raw)
                if not m:
                    continue
                if since is not None:
                    try:
                        ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S.%f")
                    except ValueError:
                        continue
                    if ts < since:
                        continue
                yield m


def bucket_http(msg: str) -> str | None:
    if not HTTP_LINE_RE.match(msg):
        return None
    for rx, name in ENDPOINT_BUCKETS:
        if rx.match(msg):
            return name
    # неизвестный endpoint — берём first 80 символов (без host)
    return msg[:80]


def render(stats: dict, since: datetime | None) -> str:
    out: list[str] = []
    out.append("# Brand Image Bot — digest")
    if since:
        out.append(f"\n_Период: с {since.isoformat(timespec='seconds')} по {datetime.now().isoformat(timespec='seconds')}._")
    else:
        out.append(f"\n_Все доступные логи (по состоянию на {datetime.now().isoformat(timespec='seconds')})._")

    # 1) Сценарии
    out.append("\n## 1) Сценарии (запусков)")
    scen_starts: Counter = stats["scenario_starts"]
    rerun_by_wf: Counter = stats["rerun_by_workflow"]
    rerun_total = sum(rerun_by_wf.values())
    edits_total = stats["edits"]
    if scen_starts or rerun_total or edits_total:
        out.append("\n| Сценарий | Запусков |")
        out.append("|---|---:|")
        for key, label in (("/generate", "/generate"), ("/img2img", "/img2img"), ("/prep_speaker", "/prep_speaker")):
            out.append(f"| {label} | {scen_starts.get(key, 0)} |")
        if rerun_total:
            out.append(f"| 🔄 regen (всех) | {rerun_total - edits_total} |")
        if edits_total:
            out.append(f"| ✏️ edit-prompt | {edits_total} |")
        if rerun_by_wf:
            out.append("\nrerun по workflow:")
            for wf, n in sorted(rerun_by_wf.items(), key=lambda kv: -kv[1]):
                out.append(f"- `{wf}`: {n}")
    else:
        out.append("\n_Нет данных._")

    # 1.5) Результаты задач
    out.append("\n## 2) Результаты задач")
    res = stats["task_results"]
    if res["count"]:
        avg = res["dur_sum"] / res["count"]
        out.append("")
        out.append(f"- завершено: **{res['count']}**, статус completed: {res['completed']}")
        out.append(f"- средняя длительность: **{avg:.1f}s**, медианная: ~{sorted(res['durs'])[len(res['durs'])//2]:.1f}s")
        out.append(f"- картинок отдано: **{res['urls']}**")
    else:
        out.append("\n_Нет завершённых задач в периоде._")

    # 2.5) Per-user — успешные генерации (admin-stat)
    out.append("\n## 2.5) По пользователям (успешные генерации)")
    user_gens: dict[int, Counter] = stats.get("user_gens", {})
    user_names: dict[int, str] = stats.get("user_names", {})
    if user_gens:
        # Сортируем по убыванию total.
        ranked = sorted(
            user_gens.items(), key=lambda kv: -sum(kv[1].values())
        )
        out.append(
            f"\nВсего активных пользователей: **{len(ranked)}**, "
            f"всего успешных генераций: **{sum(sum(c.values()) for c in user_gens.values())}**\n"
        )
        out.append("| UID | Username | Total | По workflow |")
        out.append("|---|---|---:|---|")
        for uid_i, counter in ranked:
            total = sum(counter.values())
            uname = user_names.get(uid_i, "—")
            by_wf = ", ".join(
                f"{wf}={n}" for wf, n in sorted(counter.items(), key=lambda kv: -kv[1])
            )
            out.append(f"| `{uid_i}` | {uname} | {total} | {by_wf} |")
    else:
        out.append(
            "\n_Нет admin-stat-строк в логах. Они начали писаться вместе с этой версией digest'а_"
            "_ — старые логи не содержат счётчиков по пользователям._"
        )

    # 2) HTTP запросы
    out.append("\n## 3) HTTP-запросы (Phygital + auth)")
    http = stats["http"]
    total_http = sum(http.values())
    if total_http:
        out.append(f"\nВсего: **{total_http}** запросов\n")
        out.append("| Endpoint | Hits |")
        out.append("|---|---:|")
        for name, n in sorted(http.items(), key=lambda kv: -kv[1]):
            out.append(f"| `{name}` | {n} |")
    else:
        out.append("\n_Нет HTTP-запросов в периоде._")

    # 3) Промпты
    out.append("\n## 4) Промпты")
    prompts_by_scen: dict[str, Counter] = stats["prompts_by_scenario"]
    all_prompts: Counter = stats["prompts_all"]
    if all_prompts:
        top = all_prompts.most_common(20)
        out.append(f"\n**Топ-20 по частоте** (всего уникальных: {len(all_prompts)}, всего запусков: {sum(all_prompts.values())})")
        out.append("\n| × | Промпт |")
        out.append("|---:|---|")
        for prompt, n in top:
            short = prompt if len(prompt) <= 120 else prompt[:117] + "…"
            out.append(f"| {n} | {short} |")

        out.append("\n**Разбивка по сценариям**\n")
        for scenario, counter in sorted(prompts_by_scen.items()):
            if not counter:
                continue
            out.append(f"\n### {scenario} ({sum(counter.values())} запусков, {len(counter)} уникальных)")
            for prompt, n in counter.most_common(50):
                short = prompt if len(prompt) <= 200 else prompt[:197] + "…"
                marker = f"({n}×) " if n > 1 else ""
                out.append(f"- {marker}{short}")
    else:
        out.append("\n_Промпты не зафиксированы (нужны логи после обновления `bot/scenarios.py`)._")

    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Digest над logs/bot.log* (счётчики сценариев / запросов / промптов).")
    ap.add_argument("--since", help="'24h' | '7d' | 'YYYY-MM-DD' — отсечка по времени. По умолчанию: все логи.")
    ap.add_argument("--logs", nargs="*", help="Явный список лог-файлов. По умолчанию: logs/bot.log*")
    ap.add_argument("--out", help="Путь для записи Markdown-отчёта. По умолчанию — stdout.")
    args = ap.parse_args()

    since = parse_since(args.since)
    files = discover_logs(args.logs)
    if not files:
        print("Нет файлов логов в logs/. Запусти бота сначала.", file=sys.stderr)
        return 1

    stats: dict = {
        "scenario_starts": Counter(),
        "rerun_by_workflow": Counter(),
        "edits": 0,
        "http": Counter(),
        "prompts_all": Counter(),
        "prompts_by_scenario": defaultdict(Counter),
        "task_results": {"count": 0, "completed": 0, "dur_sum": 0.0, "urls": 0, "durs": []},
        # uid → Counter(workflow → n) — успешные генерации по пользователям (admin-stat).
        "user_gens": defaultdict(Counter),
        # uid → последний известный uname (берём из admin-stat-строки).
        "user_names": {},
    }

    for m in iter_lines(files, since):
        func = m["func"]
        msg = m["msg"]

        # 1) Сценарии — по старту сценария
        scenario = SCENARIO_STARTS.get(func)
        if scenario and "started" in msg.lower():
            stats["scenario_starts"][scenario] += 1

        # Gen_start / i2i_start пишут "entered prompt-collection state" — тоже считаем как старт.
        if func == "gen_start" and "prompt-collection" in msg:
            stats["scenario_starts"]["/generate"] += 1
        elif func == "i2i_start" and "img2img" in msg.lower():
            stats["scenario_starts"]["/img2img"] += 1

        # 2) Rerun
        if func == "_rerun_from_recipe" and msg.startswith("rerun:"):
            wfm = WORKFLOW_RE.search(msg)
            if wfm:
                stats["rerun_by_workflow"][wfm.group(1)] += 1
        if msg.startswith("rerun:") and "edit" in (m["src"] or ""):
            pass  # action tagged через bind, в msg не дублируем

        # 3) HTTP
        bucket = bucket_http(msg)
        if bucket:
            stats["http"][bucket] += 1

        # 4) Промпты
        pm = PROMPT_RE.search(msg)
        if pm:
            text = pm.group("text")
            # eval string escapes (\\n → \n) для отображения
            text = text.replace("\\n", "\n").replace("\\'", "'").replace('\\"', '"')
            scen_key = PROMPT_FUNC_TO_SCENARIO.get(func)
            if scen_key == "rerun":
                wfm = WORKFLOW_RE.search(msg)
                wf = wfm.group(1) if wfm else "unknown"
                scen_key = f"rerun ({wf})"
            if scen_key:
                stats["prompts_all"][text] += 1
                stats["prompts_by_scenario"][scen_key][text] += 1

        # 4.5) Per-user админ-статистика — отдельная строка на каждую успешную генерацию.
        am = ADMIN_STAT_RE.search(msg)
        if am:
            try:
                uid_i = int(am.group("uid"))
                wf = am.group("workflow")
                stats["user_gens"][uid_i][wf] += 1
                uname = am.group("uname") or ""
                if uname:
                    stats["user_names"][uid_i] = uname
            except (ValueError, KeyError):
                pass

        # 5) Результаты задач
        tm = TASK_RESULT_RE.search(msg)
        if tm:
            r = stats["task_results"]
            r["count"] += 1
            if tm.group("status") == "completed":
                r["completed"] += 1
            dur = float(tm.group("dur"))
            r["dur_sum"] += dur
            r["durs"].append(dur)
            r["urls"] += int(tm.group("urls"))

    # rerun-edit отдельно: считаем по action="edit" в extra. Но extra мы не пишем в string-формате.
    # Поэтому различим по тексту: prompt_override is not None → "rerun" с тем же src, отличить нельзя
    # без структурного лога. Пока edit вместе с regen в "rerun по workflow".

    report = render(stats, since)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"digest → {out_path}", file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
