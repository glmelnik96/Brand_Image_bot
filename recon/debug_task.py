"""Debug: проверить статус конкретного task_id."""
import asyncio, json, sys
from client.api import PhygitalClient
from client.session import SessionManager
from recon.smoke_test import latest_storage_dump

async def main():
    task_id = int(sys.argv[1])
    session = SessionManager.from_recon_dump(latest_storage_dump())
    async with PhygitalClient(session) as client:
        data = await client.task_status(task_id)
        print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])

if __name__ == "__main__":
    asyncio.run(main())
