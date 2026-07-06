"""
Абстрактный класс бота. Все боты наследуют от него.
Гарантирует единый интерфейс: start / stop / heartbeat / emergency_check.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional
from loguru import logger

from core.state import StateStore
from core.emergency_stop import EmergencyStop


class BotStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class BaseBot(ABC):
    HEARTBEAT_INTERVAL = 30  # секунд

    def __init__(
        self,
        name: str,
        state_store: StateStore,
        emergency_stop: EmergencyStop,
    ):
        self.name = name
        self._state = state_store
        self._emergency = emergency_stop
        self._status = BotStatus.IDLE
        self._last_heartbeat = 0.0
        self._error_count = 0
        self._max_errors = 5  # После N ошибок подряд → STOP

    @property
    def status(self) -> BotStatus:
        return self._status

    def is_running(self) -> bool:
        return (
            self._status == BotStatus.RUNNING
            and self._emergency.is_running()
        )

    async def start(self) -> None:
        self._status = BotStatus.RUNNING
        self._error_count = 0
        logger.info(f"[{self.name}] Starting...")
        saved = await self._state.get_bot_state(self.name)
        if saved:
            await self.restore_state(saved)
            logger.info(f"[{self.name}] State restored from Redis.")
        await self._run_loop()

    async def stop(self) -> None:
        self._status = BotStatus.STOPPED
        await self._on_stop()
        logger.info(f"[{self.name}] Stopped.")

    async def _run_loop(self) -> None:
        try:
            # Цикл выходит только при явной остановке (CancelledError или _status=STOPPED).
            # При emergency stop — ПАУЗА (5-секундный сон), а НЕ выход.
            # Это позволяет /resume возобновить работу без перезапуска Docker.
            while self._status == BotStatus.RUNNING:
                if not self._emergency.is_running():
                    await asyncio.sleep(5)
                    continue

                tick_start = time.time()
                errored = False
                try:
                    await self.tick()
                    await self._heartbeat()
                    self._error_count = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    errored = True
                    self._error_count += 1
                    logger.error(f"[{self.name}] Error #{self._error_count}: {e}")
                    if self._error_count >= self._max_errors:
                        logger.critical(
                            f"[{self.name}] Too many errors ({self._max_errors}). "
                            "Triggering emergency stop."
                        )
                        from core.emergency_stop import StopReason
                        await self._emergency.trigger(
                            StopReason.STRATEGY,
                            f"Bot {self.name} failed {self._max_errors} times: {e}",
                        )
                        break
                    await asyncio.sleep(5)

                if errored:
                    continue  # после ошибки не ждём полный interval

                # Спим только оставшееся время, чтобы tick() не сдвигал расписание
                elapsed = time.time() - tick_start
                interval = await self.get_sleep_interval()
                sleep_time = max(0.0, interval - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
            # Нормальный выход (explicit stop) — сохранить состояние
            await self._on_stop()
        except asyncio.CancelledError:
            logger.info(f"[{self.name}] Cancelled — saving state before exit...")
            await self._on_stop()
            raise

    async def _heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat >= self.HEARTBEAT_INTERVAL:
            self._last_heartbeat = now
            await self._state.heartbeat(self.name)

    # ------------------------------------------------------------------
    # Методы для реализации в каждом боте
    # ------------------------------------------------------------------

    @abstractmethod
    async def tick(self) -> None:
        """Основная логика — вызывается в каждой итерации цикла."""

    @abstractmethod
    async def get_sleep_interval(self) -> float:
        """Сколько секунд ждать между тиками."""

    async def restore_state(self, saved: dict) -> None:
        """Восстановить состояние из Redis после рестарта. Опционально."""

    async def _on_stop(self) -> None:
        """Вызывается при остановке. Опционально — сохранить состояние."""
        state = await self.get_state_snapshot()
        if state:
            await self._state.set_bot_state(self.name, state)

    async def get_state_snapshot(self) -> Optional[dict]:
        """Что сохранить в Redis. Опционально."""
        return None
