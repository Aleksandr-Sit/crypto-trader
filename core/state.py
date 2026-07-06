"""
Redis-хранилище состояния.
При рестарте бот восстанавливает своё состояние из Redis,
не теряя открытые ордера и позиции.
"""

import json
import time
from typing import Any, Optional
from loguru import logger

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class StateStore:
    """Async Redis клиент с fallback на in-memory dict."""

    def __init__(self, redis_url: str):
        self._url = redis_url
        self._client: Any = None
        self._memory: dict[str, str] = {}   # Fallback если Redis недоступен
        self._use_redis = False

    async def connect(self) -> None:
        if not REDIS_AVAILABLE:
            logger.warning("redis package not installed. Using in-memory state (lost on restart).")
            return
        try:
            self._client = aioredis.from_url(self._url, decode_responses=True)
            await self._client.ping()
            self._use_redis = True
            logger.info(f"Redis connected: {self._url}")
        except Exception as e:
            logger.warning(f"Redis unavailable ({e}). Using in-memory state.")
            self._use_redis = False

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        data = json.dumps(value)
        if self._use_redis and self._client:
            if ttl:
                await self._client.setex(key, ttl, data)
            else:
                await self._client.set(key, data)
        else:
            self._memory[key] = data

    async def get(self, key: str, default: Any = None) -> Any:
        try:
            if self._use_redis and self._client:
                raw = await self._client.get(key)
            else:
                raw = self._memory.get(key)

            if raw is None:
                return default
            return json.loads(raw)
        except Exception as e:
            logger.error(f"State.get({key}) error: {e}")
            return default

    async def delete(self, key: str) -> None:
        if self._use_redis and self._client:
            await self._client.delete(key)
        else:
            self._memory.pop(key, None)

    async def heartbeat(self, bot_name: str) -> None:
        """Записывает timestamp последнего heartbeat бота."""
        await self.set(f"heartbeat:{bot_name}", int(time.time()), ttl=300)

    async def get_last_heartbeat(self, bot_name: str) -> Optional[int]:
        return await self.get(f"heartbeat:{bot_name}")

    async def set_bot_state(self, bot_name: str, state: dict) -> None:
        await self.set(f"bot_state:{bot_name}", state)

    async def get_bot_state(self, bot_name: str) -> dict:
        return await self.get(f"bot_state:{bot_name}", default={})

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
