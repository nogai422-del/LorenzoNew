import os
import asyncio
import aiohttp

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def main():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            print(r.status)
            print(await r.text())

asyncio.run(main())