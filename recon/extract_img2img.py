"""
Извлекает из последнего HAR'а:
  1. Все POST /api/v2/storage-object/storage-object (upload) — метаданные multipart
  2. Все POST /api/v2/tasks/ (submit) c init_img != []  — img2img payload
  3. Соответствующие POST /api/v2/tasks/config_history

Пишет компактный JSON в recon/captures/img2img_extract.json.
HAR грузим стримово через ijson, чтобы не упасть на 213МБ.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import ijson  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "recon" / "captures"


def main() -> None:
    har_files = sorted(CAPTURES.glob("phygital-*.har"))
    if not har_files:
        sys.exit("No HAR files")
    har = har_files[-1]
    print(f"Reading {har} ({har.stat().st_size/1e6:.0f}MB)")

    uploads: list[dict] = []
    submits: list[dict] = []
    configs: list[dict] = []
    prices: list[dict] = []

    with har.open("rb") as f:
        for entry in ijson.items(f, "log.entries.item"):
            req = entry.get("request", {})
            resp = entry.get("response", {})
            url = req.get("url", "")
            method = req.get("method", "")

            if method != "POST":
                continue

            # upload
            if url.endswith("/api/v2/storage-object/storage-object"):
                # multipart body слишком тяжёлый — пишем только метаданные
                post = req.get("postData", {})
                resp_text = (resp.get("content", {}) or {}).get("text", "")
                try:
                    resp_json = json.loads(resp_text) if resp_text else None
                except json.JSONDecodeError:
                    resp_json = resp_text[:200]
                uploads.append({
                    "url": url,
                    "status": resp.get("status"),
                    "request_headers": [
                        h for h in req.get("headers", [])
                        if h["name"].lower() in {"content-type", "content-length"}
                    ],
                    "postData_mimeType": post.get("mimeType"),
                    "postData_params": post.get("params"),  # обычно есть для multipart
                    "response": resp_json,
                })
                continue

            # submit
            if url.endswith("/api/v2/tasks/"):
                body_text = (req.get("postData", {}) or {}).get("text", "")
                try:
                    body = json.loads(body_text)
                except json.JSONDecodeError:
                    continue
                # фильтр: только img2img
                init_img_input = next(
                    (i for i in body.get("inputs", []) if i.get("name") == "init_img"),
                    None,
                )
                if init_img_input and init_img_input.get("value"):
                    resp_text = (resp.get("content", {}) or {}).get("text", "")
                    try:
                        resp_json = json.loads(resp_text)
                    except json.JSONDecodeError:
                        resp_json = None
                    submits.append({"request": body, "response": resp_json})
                continue

            # config_history
            if url.endswith("/api/v2/tasks/config_history"):
                body_text = (req.get("postData", {}) or {}).get("text", "")
                try:
                    body = json.loads(body_text)
                except json.JSONDecodeError:
                    continue
                configs.append({"taskId": body.get("taskId"), "config": body.get("config")})
                continue

            # price
            if url.endswith("/api/v2/nodes/get_credits_price"):
                body_text = (req.get("postData", {}) or {}).get("text", "")
                try:
                    body = json.loads(body_text)
                except json.JSONDecodeError:
                    continue
                resp_text = (resp.get("content", {}) or {}).get("text", "")
                try:
                    resp_json = json.loads(resp_text)
                except json.JSONDecodeError:
                    resp_json = None
                # только когда есть init_img
                init = next((i for i in body.get("inputs", []) if i.get("name") == "init_img"), None)
                if init:
                    prices.append({"request": body, "response": resp_json})
                continue

    out = {
        "uploads": uploads,
        "submits_img2img": submits,
        "config_history": configs,
        "prices_img2img": prices,
    }
    out_path = CAPTURES / "img2img_extract.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"uploads={len(uploads)}  submits={len(submits)}  configs={len(configs)}  prices={len(prices)}")
    print(f"→ {out_path}")


if __name__ == "__main__":
    main()
