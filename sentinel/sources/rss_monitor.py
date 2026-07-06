"""RSS-мониторинг крипто-новостных сайтов."""

import asyncio
import feedparser
from loguru import logger


class RSSMonitor:
    def __init__(self, feeds: list[dict]):
        self._feeds = feeds  # [{url: ..., name: ..., weight: ...}]

    async def fetch(self) -> list[tuple[str, str, str]]:
        """Возвращает список (headline, url, source)."""
        tasks = [self._fetch_one(feed) for feed in self._feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        news: list[tuple[str, str, str]] = []
        for result in results:
            if isinstance(result, BaseException):
                continue
            news.extend(result)
        return news

    async def _fetch_one(
        self, feed: dict
    ) -> list[tuple[str, str, str]]:
        url = feed["url"]
        name = feed.get("name", url)
        try:
            loop = asyncio.get_running_loop()
            parsed = await loop.run_in_executor(None, feedparser.parse, url)
            items = []
            for entry in parsed.entries[:20]:  # Последние 20 записей
                title = entry.get("title", "")
                link = entry.get("link", "")
                if title:
                    items.append((title, link, name))
            return items
        except Exception as e:
            logger.warning(f"RSS fetch failed for {name}: {e}")
            return []
