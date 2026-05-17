"""Smoke-тест GeminiTextWorkflow: один короткий вызов против живого API.
Проверяем форму ответа (как именно description приходит в outputs)."""
import asyncio
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from client.api import PhygitalClient
from client.config import settings
from client.session import SessionManager
from cli import load_session
from workflows.gemini_text import GeminiTextWorkflow


async def main():
    mgr = SessionManager(settings.session_file)
    sess = load_session(mgr)
    async with PhygitalClient(sess, session_manager=mgr) as cli:
        wf = GeminiTextWorkflow(cli, model="pro_3", thinking_level="low")
        # короткий вход — экономим credits и время
        job = await wf.run_text(prompt="Reply with exactly the single word PONG and nothing else.")
        print("STATUS:", job.status)
        print("ERROR:", job.error)
        print("RESULT_TEXT:", repr(job.result_text))
        print("RAW (truncated):")
        print(json.dumps(job.raw, ensure_ascii=False, indent=2, default=str)[:4000])


if __name__ == "__main__":
    asyncio.run(main())
