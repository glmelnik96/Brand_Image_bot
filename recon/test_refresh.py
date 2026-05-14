"""Cold-тест refresh: смотрим, что приходит на /auth/session/refresh с валидной сессией."""
import asyncio
from loguru import logger

from client.auth import refresh_session
from client.session import SessionManager, ACCESS_COOKIE, REFRESH_COOKIE
from recon.smoke_test import latest_storage_dump


async def main():
    dump = latest_storage_dump()
    session = SessionManager.from_recon_dump(dump)
    before_acc = session.access_token
    before_ref = session.refresh_token
    logger.info(f"BEFORE: access_token len={len(before_acc) if before_acc else 0} refresh_token len={len(before_ref) if before_ref else 0}")
    logger.info(f"  access head: {before_acc[:40]}...")

    await refresh_session(session)

    after_acc = session.access_token
    after_ref = session.refresh_token
    logger.info(f"AFTER:  access_token len={len(after_acc)} refresh_token len={len(after_ref) if after_ref else 0}")
    logger.info(f"  access head: {after_acc[:40]}...")
    logger.info(f"  access changed: {before_acc != after_acc}")
    logger.info(f"  refresh changed: {before_ref != after_ref}")


if __name__ == "__main__":
    asyncio.run(main())
