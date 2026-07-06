"""
Redistributor — ежедневное реинвестирование прибыли (сложный процент).

Запускается раз в сутки в run_at_utc (по умолчанию 00:00 UTC).

Логика:
1. Считает суммарную прибыль всех ботов за день (через PortfolioManager)
2. Если прибыль > min_profit_usd — распределяет по аллокации из конфига:
     grid_bot    → 50%
     funding_arb → 30%
     nfi_bot     → 20%
3. Логирует отчёт и отправляет в Telegram

Не торгует сам — только учёт и репортинг.
"""

from datetime import datetime, timezone
from typing import Optional
from loguru import logger

from bots.base import BaseBot
from core.state import StateStore
from core.emergency_stop import EmergencyStop
from core.portfolio import PortfolioManager


class RedistributorBot(BaseBot):
    TICK_INTERVAL = 60.0  # проверяем каждую минуту — не пропустить нужное время

    def __init__(
        self,
        config: dict,
        state_store: StateStore,
        emergency_stop: EmergencyStop,
        portfolio: PortfolioManager,
        notifier=None,
    ):
        super().__init__("redistributor", state_store, emergency_stop)
        self._cfg        = config
        self._portfolio  = portfolio
        self._notify     = notifier

        # Время запуска (UTC час)
        run_at = config.get("run_at_utc", "00:00")
        self._run_hour, self._run_minute = map(int, run_at.split(":"))

        self._min_profit    = config.get("min_profit_usd", 5.0)
        self._allocation    = config.get("allocation", {
            "grid_bot": 0.50, "funding_arb": 0.30, "nfi_bot": 0.20
        })

        self._last_run_date: str = ""      # дата последнего запуска "YYYY-MM-DD"
        self._total_redistributed: float = 0.0
        self._run_count: int = 0

    # ------------------------------------------------------------------

    async def tick(self) -> None:
        now = datetime.now(timezone.utc)

        today = now.strftime("%Y-%m-%d")
        # Запускаем только один раз в нужное время, не повторяем в тот же день
        if (now.hour != self._run_hour or
                now.minute != self._run_minute or
                today == self._last_run_date):
            return

        self._last_run_date = today
        await self._run_redistribution(now)

    async def _run_redistribution(self, now: datetime) -> None:
        logger.info(f"[redistributor] Запуск реинвестирования {now.strftime('%Y-%m-%d %H:%M UTC')}")

        result = await self._portfolio.redistribute_daily()

        if result is None:
            msg = (
                f"📊 Реинвестирование {now.strftime('%d.%m')}:\n"
                f"Прибыль < ${self._min_profit:.0f} — пропущено"
            )
            logger.info(f"[redistributor] {msg}")
        else:
            total_added = sum(result.values())
            self._total_redistributed += total_added
            self._run_count += 1

            snap = await self._portfolio.get_snapshot()
            lines = [
                f"💰 Реинвестирование {now.strftime('%d.%m.%Y')}",
                f"Распределено: ${total_added:.2f}",
                "",
            ]
            for bot_name, amount in result.items():
                pct = self._allocation.get(bot_name, 0) * 100
                lines.append(f"  {bot_name}: +${amount:.2f} ({pct:.0f}%)")

            lines += [
                "",
                f"Портфель: ${snap.total_usdt:.2f}",
                f"Итого реинвестировано: ${self._total_redistributed:.2f}",
            ]
            msg = "\n".join(lines)
            logger.info(f"[redistributor] {msg}")

        if self._notify:
            await self._notify(msg)

    async def get_sleep_interval(self) -> float:
        return self.TICK_INTERVAL

    async def get_state_snapshot(self) -> Optional[dict]:
        return {
            "last_run_date":         self._last_run_date,
            "total_redistributed":   self._total_redistributed,
            "run_count":             self._run_count,
        }

    async def restore_state(self, saved: dict) -> None:
        # backward compat: старое поле last_run_day (int) → игнорируем, начнём с чистого листа
        self._last_run_date       = saved.get("last_run_date", "")
        self._total_redistributed = saved.get("total_redistributed", 0.0)
        self._run_count           = saved.get("run_count", 0)
        logger.info(
            f"[redistributor] State restored. Runs: {self._run_count}, "
            f"redistributed: ${self._total_redistributed:.2f}"
        )
