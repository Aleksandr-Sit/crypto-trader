"""
Analyzer Bot — ежедневный отчёт + параметрические рекомендации.

Запускается раз в сутки в report_time_utc (UTC, формат HH:MM).
1. Снимает статистику со всех ботов → сохраняет в Redis (35 дней)
2. Читает историю за 7 дней
3. ParameterAdvisor анализирует тренды и предлагает изменения
4. Отправляет отчёт в Telegram
"""

from datetime import datetime, timezone
from loguru import logger

from bots.base import BaseBot
from core.analytics import PerformanceTracker, ParameterAdvisor
from core.state import StateStore
from core.emergency_stop import EmergencyStop


class AnalyzerBot(BaseBot):
    TICK_INTERVAL = 60.0  # проверяем время каждую минуту

    def __init__(
        self,
        state_store: StateStore,
        emergency_stop: EmergencyStop,
        portfolio,
        config: dict,
        full_config: dict,
        notifier=None,
    ):
        super().__init__("analyzer", state_store, emergency_stop)
        self._portfolio = portfolio
        self._cfg       = config
        self._full_cfg  = full_config
        self._notify    = notifier
        self._tracker   = PerformanceTracker(state_store)
        self._advisor   = ParameterAdvisor()
        self._bots: dict = {}
        self._last_report_date: str = ""

        report_time = config.get("report_time_utc", "08:00")
        h, m = report_time.split(":")
        self._report_hour = int(h)
        self._report_min  = int(m)

    def register_bot(self, name: str, bot) -> None:
        self._bots[name] = bot

    # ------------------------------------------------------------------
    # Основной тик (раз в минуту, отчёт раз в сутки)
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        now   = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        if now.hour != self._report_hour or now.minute != self._report_min:
            return
        if self._last_report_date == today:
            return

        self._last_report_date = today
        logger.info(f"[analyzer] Running daily report for {today}")

        try:
            snapshot = await self._tracker.save_snapshot(self._bots, self._portfolio)
            history  = await self._tracker.get_history(days=7)
            hints    = self._advisor.analyze(history, self._full_cfg)
            report   = self._format_report(snapshot, history, hints)
            if self._notify:
                await self._notify(report)
        except Exception as e:
            logger.error(f"[analyzer] Report failed: {e}")

    # ------------------------------------------------------------------
    # Форматирование отчёта
    # ------------------------------------------------------------------

    def _format_report(
        self, snap: dict, history: list[dict], hints: list[str]
    ) -> str:
        lines = [f"📊 DAILY REPORT [{snap['date']} {self._report_hour:02d}:{self._report_min:02d} UTC]", ""]

        # Grid
        grid = snap.get("bots", {}).get("grid_bot", {})
        if grid:
            mode = "paper" if grid.get("mode") == "paper" else "live"
            lines += [
                f"🔷 Grid Bot [{grid.get('symbol', 'BTC')}] [{mode}]",
                f"  Сетка: ${grid.get('grid_low', 0):.0f}–${grid.get('grid_high', 0):.0f}"
                f" | шаг ${grid.get('grid_step', 0):.0f}",
                f"  Ордера: {grid.get('buy_orders', 0)}↓ {grid.get('sell_orders', 0)}↑"
                f" | Сделок: {grid.get('total_trades', 0)}",
                f"  Прибыль: ${grid.get('total_profit_usd', 0):.2f} | Пересборок: {grid.get('rebuilds_today', 0)}",
                "",
            ]

        # Funding Arb
        arb = snap.get("bots", {}).get("funding_arb", {})
        if arb:
            max_pos = self._full_cfg.get("funding_arb", {}).get("max_positions", 3)
            lines += [
                "💸 Funding Arb",
                f"  Позиций: {arb.get('active_positions', 0)}/{max_pos}",
                f"  Собрано: ${arb.get('total_funding_earned', 0):.4f}"
                f" | Закрыто: {arb.get('total_closed_positions', 0)}",
                "",
            ]

        # Scalper
        scalper = snap.get("bots", {}).get("scalper", {})
        if scalper:
            lines += [
                "⚡ Scalper",
                f"  Сделок: {scalper.get('total_trades', 0)} | Win: {scalper.get('win_rate_pct', 0):.0f}%",
                f"  PnL: ${scalper.get('total_pnl_usdt', 0):+.4f}",
                "",
            ]

        # Breakout
        breakout = snap.get("bots", {}).get("breakout", {})
        if breakout:
            lines += [
                "🚀 Breakout Bot",
                f"  Сделок: {breakout.get('total_trades', 0)} | Win: {breakout.get('win_rate_pct', 0):.0f}%",
                f"  Активных: {breakout.get('active_trades', 0)} | PnL: ${breakout.get('total_pnl_usdt', 0):+.4f}",
                "",
            ]

        # Stat Arb
        stat_arb = snap.get("bots", {}).get("stat_arb", {})
        if stat_arb:
            if stat_arb.get("has_position"):
                pos_str = f"{stat_arb.get('direction', '—')} ({stat_arb.get('hold_hours', 0):.1f}h)"
            else:
                pos_str = "нет"
            lines += [
                "⚖️ StatArb (ETH/BTC)",
                f"  Позиция: {pos_str}",
                f"  Сделок: {stat_arb.get('total_trades', 0)} | Win: {stat_arb.get('win_rate_pct', 0):.0f}%",
                f"  PnL: ${stat_arb.get('total_pnl_usdt', 0):+.4f}",
                "",
            ]

        # Portfolio
        pf = snap.get("portfolio")
        if pf:
            icon = "📈" if (pf.get("daily_pnl") or 0) >= 0 else "📉"
            lines += [
                "💼 Портфель",
                f"  Баланс: ${pf.get('total_usdt', 0):.2f}",
                f"  {icon} За день: ${pf.get('daily_pnl', 0):+.2f}",
                f"  Всего P&L: ${pf.get('total_pnl', 0):+.2f}",
            ]
            if len(history) >= 2:
                week_pnl = sum(
                    (h.get("portfolio") or {}).get("daily_pnl", 0) or 0
                    for h in history
                )
                lines.append(f"  За {len(history)}д: ${week_pnl:+.2f}")
            lines.append("")

        # Advisor
        lines.append("⚙️ СОВЕТНИК")
        for hint in hints:
            for line in hint.split("\n"):
                lines.append(f"  {line}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # BaseBot interface
    # ------------------------------------------------------------------

    async def get_sleep_interval(self) -> float:
        return self.TICK_INTERVAL

    async def get_state_snapshot(self):
        return {"last_report_date": self._last_report_date}

    async def restore_state(self, saved: dict) -> None:
        self._last_report_date = saved.get("last_report_date", "")

    def get_stats(self) -> dict:
        return {
            "last_report_date": self._last_report_date,
            "bots_monitored":   list(self._bots.keys()),
        }
