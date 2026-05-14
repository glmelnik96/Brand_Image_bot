"""
Recon: разбор HAR-файла после capture.py.

Запуск:
    python -m recon.analyze recon/captures/phygital-<ts>.har
    # либо без аргумента — возьмёт самый свежий HAR

Печатает:
- список уникальных endpoints app.phygital.plus с методом, частотой и средним размером
- для каждого endpoint — пример запроса (headers с маскировкой + body, обрезанный до 2KB)
- список WebSocket URL
- кандидатов на auth-токены (Authorization, x-*-token, cookies)
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "recon" / "captures"
TARGET_HOST = "app.phygital.plus"

SENSITIVE_HEADER_RE = re.compile(r"(authorization|cookie|x-.*token|x-.*auth|api[-_]?key)", re.I)
MAX_BODY_PREVIEW = 2048

console = Console()


def latest_har() -> Path:
    files = sorted(CAPTURES.glob("phygital-*.har"))
    if not files:
        raise SystemExit(f"No HAR files in {CAPTURES}. Run recon/capture.py first.")
    return files[-1]


def mask(value: str) -> str:
    if not value:
        return value
    if len(value) <= 12:
        return value[:2] + "…"
    return f"{value[:6]}…{value[-4:]}  (len={len(value)})"


def truncate(text: str, limit: int = MAX_BODY_PREVIEW) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, total {len(text)} chars]"


def load_har(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw)


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else ""


def analyze(har: dict[str, Any]) -> None:
    entries = har.get("log", {}).get("entries", [])
    console.print(f"[bold]Total HAR entries:[/bold] {len(entries)}")

    target = [e for e in entries if host_of(e["request"]["url"]).endswith(TARGET_HOST)
              or "phygital" in host_of(e["request"]["url"])]
    console.print(f"[bold]Entries for *.phygital.*:[/bold] {len(target)}")

    if not target:
        console.print("[red]Нет запросов к phygital. Проверь, что capture поймал трафик.[/red]")
        return

    # ── Группировка по endpoint ────────────────────────────────────────────
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in target:
        req = e["request"]
        # Нормализуем path: убираем query, заменяем числовые/uuid сегменты на :id
        url = req["url"].split("?", 1)[0]
        path = re.sub(r"https?://[^/]+", "", url)
        path = re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/:uuid", path)
        path = re.sub(r"/\d{3,}", "/:id", path)
        groups[(req["method"], path)].append(e)

    table = Table(title="Endpoints (grouped)", show_lines=False)
    table.add_column("Method", style="cyan")
    table.add_column("Path", style="white")
    table.add_column("Count", justify="right", style="yellow")
    table.add_column("Avg resp size", justify="right")
    table.add_column("Status codes")
    table.add_column("Content-Type")

    for (method, path), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        sizes = [e["response"].get("bodySize", 0) or 0 for e in items]
        avg = sum(sizes) // max(len(sizes), 1)
        statuses = sorted({e["response"].get("status", 0) for e in items})
        ctypes = sorted({
            next((h["value"].split(";")[0] for h in e["response"].get("headers", [])
                  if h["name"].lower() == "content-type"), "")
            for e in items
        })
        table.add_row(method, path, str(len(items)), f"{avg}", str(statuses), ", ".join(c for c in ctypes if c))
    console.print(table)

    # ── WebSocket URL ──────────────────────────────────────────────────────
    ws_urls = sorted({e["request"]["url"] for e in target
                      if e["request"]["url"].startswith(("ws://", "wss://"))})
    if ws_urls:
        console.print("\n[bold magenta]WebSocket connections:[/bold magenta]")
        for u in ws_urls:
            console.print(f"  • {u}")

    # ── Auth-кандидаты ─────────────────────────────────────────────────────
    console.print("\n[bold green]Auth-кандидаты (sensitive headers, маскировано):[/bold green]")
    seen: set[str] = set()
    for e in target:
        for h in e["request"].get("headers", []):
            if SENSITIVE_HEADER_RE.search(h["name"]):
                key = h["name"].lower()
                if key in seen:
                    continue
                seen.add(key)
                console.print(f"  {h['name']}: {mask(h['value'])}")

    # ── Примеры payload для топ-5 endpoints с не-GET методом ───────────────
    console.print("\n[bold cyan]Sample payloads (top non-GET endpoints):[/bold cyan]")
    non_get = [(k, v) for k, v in groups.items() if k[0] != "GET"]
    non_get.sort(key=lambda kv: -len(kv[1]))
    for (method, path), items in non_get[:5]:
        e = items[0]
        console.rule(f"{method} {path}")
        req = e["request"]
        console.print(f"[dim]URL:[/dim] {req['url']}")
        # headers (маскируем чувствительные)
        for h in req.get("headers", []):
            val = mask(h["value"]) if SENSITIVE_HEADER_RE.search(h["name"]) else h["value"]
            console.print(f"  [blue]{h['name']}[/blue]: {val}")
        # body
        post = req.get("postData", {})
        if post:
            text = post.get("text", "")
            console.print(f"\n[dim]Body (Content-Type: {post.get('mimeType', '?')}):[/dim]")
            console.print(truncate(text))
        # response preview
        resp = e["response"]
        content = resp.get("content", {})
        if content.get("text"):
            console.print(f"\n[dim]Response ({resp.get('status')}, "
                          f"{content.get('mimeType', '?')}):[/dim]")
            console.print(truncate(content["text"], 1024))


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = latest_har()
    console.print(f"[bold]Analyzing:[/bold] {path}\n")
    analyze(load_har(path))


if __name__ == "__main__":
    main()
