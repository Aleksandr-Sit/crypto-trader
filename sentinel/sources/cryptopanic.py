"""CryptoPanic API — крипто-новости с тегами."""

import aiohttp
from loguru import logger


class CryptoPanicSource:
    BASE_URL = "https://cryptopanic.com/api/v1/posts/"

    def __init__(self, api_key: str):
        self._key = api_key

    async def fetch(self) -> list[tuple[str, str, str]]:
        """Возвращает список (headline, url, source)."""
        params = {
            "auth_token": self._key,
            "kind": "news",
            "filter": "hot",
            "public": "true",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                self.BASE_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"CryptoPanic API returned {resp.status}")
                    return []
                data = await resp.json()

        results = []
        for item in data.get("results", []):
            title = item.get("title", "")
            url = item.get("url", "")
            source = item.get("source", {}).get("title", "CryptoPanic")
            if title:
                results.append((title, url, source))
        return results
