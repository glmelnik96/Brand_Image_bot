#!/usr/bin/env python3
"""Sync uncommitted work from Documents/Phygital-bot, commit, and push."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path.home() / "Documents" / "Phygital-bot"
DST = Path(__file__).resolve().parent

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "logs",
    ".pytest_cache",
    ".omc",
    ".claude",
    "bot/tmp",
    "bot/regen_cache",
    "recon/captures",
    "user_data",
}

SKIP_FILE_NAMES = {
    ".env",
    "session.json",
    "storage_state.json",
    "brand_docs.json",
}

SKIP_SUFFIXES = (".log", ".bak-stale", ".har", ".har.gz")

COMMIT_MSG = """Sync full bot UX: menu overhaul, brand variants, speaker bg swap, safety scrubber

- Hierarchical /menu navigation; slash-command shortcuts removed from entry points
- Brand text2img split into Photo / Render / Isometric with dedicated system prompts
- Speaker prep prompt rewrite; post-result background swap on brand colors
- Nano Banana safety-word scrubber with automatic retry in brand_t2i
- StatusReporter for live step/elapsed updates; startup/shutdown broadcasts
- brand_docs TTL cache, stale-doc invalidation, new docs and tests"""


def should_skip(rel: Path) -> bool:
    if rel.name in SKIP_FILE_NAMES:
        return True
    if rel.suffix.lower() in SKIP_SUFFIXES:
        return True
    parts = rel.parts
    for i in range(len(parts)):
        sub = "/".join(parts[: i + 1])
        if sub in SKIP_DIR_NAMES or parts[i] in SKIP_DIR_NAMES:
            return True
    return False


def sync_tree() -> int:
    if not SRC.is_dir():
        print(f"Source repo not found: {SRC}", file=sys.stderr)
        return 1
    copied = 0
    for src in sorted(SRC.rglob("*")):
        rel = src.relative_to(SRC)
        if should_skip(rel):
            continue
        dst = DST / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    print(f"Synced {copied} files from {SRC}")
    return 0


def run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+ git", " ".join(args))
    return subprocess.run(["git", *args], cwd=DST, check=check, text=True, capture_output=not check)


def main() -> int:
    rc = sync_tree()
    if rc:
        return rc

    run_git(["add", "-A"])
    status = run_git(["status", "--porcelain"], check=False)
    out = (status.stdout or "").strip()
    print(out or "(nothing to commit)")
    if out:
        run_git(["commit", "-m", COMMIT_MSG])
    else:
        print("Nothing new to commit")
    run_git(["push", "origin", "main"])
    run_git(["log", "--oneline", "-3"], check=False)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
