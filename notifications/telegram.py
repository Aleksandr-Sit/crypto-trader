"""
Telegram-бот: уведомления + команды управления.

Безопасность:
- Команды принимаются ТОЛЬКО от TELEGRAM_CHAT_ID из .env
- API-ключи никогда не передаются в сообщениях
- /stopall и /withdraw требуют подтверждения
"""

import html as _html
from typing import Optional, TYPE_CHECKING
from loguru import logger
from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core.security import SecretsManager

if TYPE_CHECKING:
    from core.emergency_stop import EmergencyStop
    from core.portfolio import PortfolioManager
    from core.exchange import ExchangeManager


class TelegramNotifier:
    def __init__(self, secrets: SecretsManager):
        self._token = secrets.require("TELEGRAM_BOT_TOKEN")
        self._chat_id = int(secrets.require("TELEGRAM_CHAT_ID"))
        self._bot = Bot(token=self._token)
        self._app: Optional[Application] = None

        self._emergency_stop: Optional["EmergencyStop"] = None
        self._portfolio: Optional["PortfolioManager"] = None
        self._sentinel = None
        self._exchange_manager: Optional["ExchangeManager"] = None
        self._bots: dict = {}   # name → bot instance (GridBot, FundingArbBot, ScalperBot)

        self._pending_confirm: Optional[str] = None

    def set_emergency_stop(self, es: "EmergencyStop") -> None:
        self._emergency_stop = es

    def set_portfolio(self, pm: "PortfolioManager") -> None:
        self._portfolio = pm

    def set_sentinel(self, sentinel) -> None:
        self._sentinel = sentinel

    def set_exchange_manager(self, em: "ExchangeManager") -> None:
        self._exchange_manager = em

    def register_bot(self, name: str, bot) -> None:
        self._bots[name] = bot

    # ------------------------------------------------------------------
    # Отправка сообщений — используется всеми модулями
    # ------------------------------------------------------------------

    async def send(self, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=_html.escape(text),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    # ------------------------------------------------------------------
    # Middleware: проверка что сообщение от владельца
    # ------------------------------------------------------------------

    def _is_owner(self, update: Update) -> bool:
        return update.effective_chat.id == self._chat_id

    async def _reject(self, update: Update) -> None:
        logger.warning(
            f"Unauthorized command from chat_id={update.effective_chat.id}"
        )
        # Намеренно не отвечаем — не раскрываем что бот существует

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return

        state = self._emergency_stop.state.value if self._emergency_stop else "unknown"
        mode  = "PAPER" if any(
            getattr(b, "_paper", False) for b in self._bots.values()
        ) else "LIVE"

        lines = [
            f"🤖 <b>Crypto-Trader</b> — {mode}",
            f"📡 Состояние: <code>{state}</code>",
            "",
        ]

        # Портфель
        if self._portfolio:
            snap = await self._portfolio.get_snapshot()
            pnl_icon = "📈" if snap.daily_pnl >= 0 else "📉"
            lines += [
                f"💼 Баланс: <b>${snap.total_usdt:.2f}</b>",
                f"{pnl_icon} За день: {snap.daily_pnl:+.2f}$  |  Всего: {snap.total_pnl:+.2f}$",
                "",
            ]

        lines.append("━━━━━━━━━━━━━━━━━")

        # Grid Bot
        grid = self._bots.get("grid")
        if grid:
            s = grid.get_stats()
            if s["grid_low"] > 0:
                lines += [
                    f"📊 <b>Grid Bot</b> [{s['symbol']}] • {s['mode']}",
                    f"  Сетка: ${s['grid_low']:,.0f} — ${s['grid_high']:,.0f} | шаг ${s['grid_step']:.0f}",
                    f"  Ордера: {s['buy_orders']} buy / {s['sell_orders']} sell",
                    f"  Сделок: {s['total_trades']} | P&amp;L: +${s['total_profit_usd']:.4f}",
                    "",
                ]
            else:
                lines += ["📊 <b>Grid Bot</b> — инициализируется...", ""]

        # Funding Arb Bot
        arb = self._bots.get("funding_arb")
        if arb:
            s = arb.get_stats()
            lines.append("📊 <b>Funding Arb Bot</b>")
            if s["active_positions"] > 0:
                for sym, p in s["positions"].items():
                    lines.append(
                        f"  {sym}: rate {p['entry_rate_pct']:.4f}% | "
                        f"собрано ${p['funding_collected']:.4f} | "
                        f"{p['hold_hours']:.1f}ч"
                    )
            else:
                lines.append(f"  Позиций нет | Закрыто: {s['total_closed_positions']}")
            lines += [
                f"  Заработано всего: ${s['total_funding_earned']:.4f}",
                "",
            ]

        # Scalper Bot
        scalper = self._bots.get("scalper")
        if scalper:
            s = scalper.get_stats()
            wr = f"{s['win_rate_pct']:.0f}%" if s["total_trades"] > 0 else "—"
            lines += [
                "📊 <b>Scalper Bot</b>",
                f"  Открыто: {s['active_trades']} | Всего: {s['total_trades']} | Win: {wr}",
                f"  P&amp;L: ${s['total_pnl_usdt']:+.4f}",
                "",
            ]

        # Breakout Bot
        breakout = self._bots.get("breakout")
        if breakout:
            s = breakout.get_stats()
            wr = f"{s['win_rate_pct']:.0f}%" if s["total_trades"] > 0 else "—"
            active = s.get("active_trades", 0)
            lines += [
                "📊 <b>Breakout Bot</b>",
                f"  Открыто: {active} | Всего: {s['total_trades']} | Win: {wr}",
                f"  P&amp;L: ${s['total_pnl_usdt']:+.4f}",
                "",
            ]

        # StatArb Bot
        stat_arb = self._bots.get("stat_arb")
        if stat_arb:
            s = stat_arb.get_stats()
            if s["has_position"]:
                pos_str = f"{s['direction']} ({s['hold_hours']:.1f}h)"
            else:
                pos_str = "нет позиции"
            wr = f"{s['win_rate_pct']:.0f}%" if s["total_trades"] > 0 else "—"
            lines += [
                "📊 <b>StatArb</b> (ETH/BTC)",
                f"  Позиция: {pos_str}",
                f"  Сделок: {s['total_trades']} | Win: {wr}",
                f"  P&amp;L: ${s['total_pnl_usdt']:+.4f}",
                "",
            ]

        lines.append("━━━━━━━━━━━━━━━━━")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_stopall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        self._pending_confirm = "stopall"
        await update.message.reply_text(
            "⚠️ Подтверди остановку ВСЕХ ботов:\n"
            "Напиши <code>ДА СТОП</code> для подтверждения",
            parse_mode="HTML",
        )

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        if self._emergency_stop:
            await self._emergency_stop.resume()
        await update.message.reply_text("▶️ Команда resume отправлена.")

    async def cmd_profit(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        if not self._portfolio:
            await update.message.reply_text("Portfolio не инициализирован.")
            return
        snap = await self._portfolio.get_snapshot()
        await update.message.reply_text(
            self._portfolio.format_report(snap), parse_mode="HTML"
        )

    async def cmd_sentinel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        if self._sentinel:
            report = self._sentinel.get_status_report()
        else:
            report = "Sentinel не запущен."
        await update.message.reply_text(report, parse_mode="HTML")

    async def cmd_withdraw_test(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Тестовый вывод $1 для проверки маршрута. Показывает выбранную сеть до подтверждения."""
        if not self._is_owner(update):
            return

        net_preview = ""
        if self._exchange_manager:
            try:
                net = await self._exchange_manager.get_best_withdrawal_network("USDT")
                if net:
                    fee_str = f"${net.fee:.4f}" if net.fee > 0 else "уточняется"
                    addr_hint = f"{net.address[:8]}...{net.address[-6:]}"
                    net_preview = (
                        f"\n\nВыбрана сеть: <code>{net.chain}</code> | fee {fee_str}"
                        f"\nАдрес: <code>{addr_hint}</code>"
                    )
                else:
                    net_preview = "\n\n⚠️ Нет настроенных адресов вывода в .env"
            except Exception as e:
                net_preview = f"\n\n⚠️ Не удалось запросить сети: {e}"

        self._pending_confirm = "withdraw_test"
        await update.message.reply_text(
            f"⚠️ Тестовый вывод $1 USDT на холодный кошелёк.{net_preview}\n\n"
            "Напиши <code>ДА ВЫВОД</code> для подтверждения",
            parse_mode="HTML",
        )

    async def cmd_debug_drawdown(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Тест drawdown guard: симулирует просадку -13% и проверяет что Emergency Stop срабатывает.
        Только в paper mode. После теста автоматически вызывает resume."""
        if not self._is_owner(update):
            return

        if not self._emergency_stop:
            await update.message.reply_text("❌ EmergencyStop не инициализирован.")
            return

        is_paper = any(getattr(b, "_paper", False) for b in self._bots.values())
        if not is_paper:
            await update.message.reply_text(
                "⛔ /debug_drawdown доступна только в paper mode."
            )
            return

        em = self._emergency_stop
        if not em.is_running():
            await update.message.reply_text(
                "⚠️ Emergency Stop уже активен. Сначала /resume, затем повтори тест."
            )
            return

        # Установить фиктивный пик $1000 и симулировать просадку -13%
        em._peak_balance = 1000.0
        test_balance = 870.0   # -13% от 1000 > порога 12%
        await update.message.reply_text(
            f"🧪 Тест drawdown guard...\n"
            f"Пик: $1000.00 | Симулирую баланс: ${test_balance:.2f} (-13%)\n"
            f"Порог срабатывания: {em.MAX_DRAWDOWN_PCT:.0f}%"
        )

        await em.on_balance_update(test_balance)

        if not em.is_running():
            await update.message.reply_text(
                f"✅ Drawdown guard СРАБОТАЛ корректно\n"
                f"Причина: {em._stop_reason.value if em._stop_reason else '?'}\n"
                f"Автоматически возобновляю (resume)..."
            )
            em._state = em._state.__class__.RUNNING
            em._stop_reason = None
            em._peak_balance = 0.0
            await update.message.reply_text("▶️ Система возобновлена. Тест пройден.")
        else:
            await update.message.reply_text(
                "❌ Drawdown guard НЕ сработал. Проверь логику on_balance_update()."
            )

    async def cmd_exchange_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        if not self._exchange_manager:
            await update.message.reply_text("ExchangeManager не инициализирован.")
            return

        await update.message.reply_text("📊 Запрашиваю балансы...")
        lines = ["💰 <b>Балансы на биржах</b>", ""]
        total = 0.0

        for name, adapter in self._exchange_manager.adapters.items():
            try:
                bal_usdt = await adapter.get_balance("USDT")
                bal_btc  = await adapter.get_balance("BTC")
                btc_price = await adapter.get_ticker("BTCUSDT")
                usdt_equiv = bal_usdt.available + bal_btc.available * btc_price
                total += usdt_equiv
                lines.append(
                    f"<b>{name.upper()}</b>\n"
                    f"  USDT: ${bal_usdt.available:.2f}\n"
                    f"  BTC:  {bal_btc.available:.6f} (≈${bal_btc.available * btc_price:.2f})"
                )
            except Exception as e:
                lines.append(f"<b>{name.upper()}</b>: ошибка ({e})")

        lines += ["", f"<b>Итого: ≈${total:.2f}</b>"]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        text = (
            "📋 <b>Команды управления</b>\n\n"
            "/status — состояние системы и портфеля\n"
            "/profit — P&L за день/месяц\n"
            "/sentinel — статус мониторинга новостей\n"
            "/exchanges — балансы на всех биржах\n"
            "/stopall — 🛑 остановить все боты\n"
            "/resume — ▶️ возобновить торговлю\n"
            "/withdraw_test — тест маршрута вывода ($1)\n"
            "/debug_drawdown — тест drawdown guard (только paper)\n"
            "/help — эта справка"
        )
        await update.message.reply_text(text, parse_mode="HTML")

    # ------------------------------------------------------------------
    # Обработка подтверждений опасных операций
    # ------------------------------------------------------------------

    async def handle_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return
        text = (update.message.text or "").strip().upper()

        if self._pending_confirm == "stopall" and text == "ДА СТОП":
            self._pending_confirm = None
            await update.message.reply_text("🛑 Останавливаю все боты...")
            if self._emergency_stop:
                from core.emergency_stop import StopReason
                await self._emergency_stop.trigger(
                    StopReason.MANUAL, "Ручная остановка через Telegram"
                )

        elif self._pending_confirm == "withdraw_test" and text == "ДА ВЫВОД":
            self._pending_confirm = None
            await update.message.reply_text("💸 Тестовый вывод $1 USDT инициирован...")
            if not self._exchange_manager:
                await update.message.reply_text("❌ ExchangeManager не доступен.")
            else:
                try:
                    ok, net = await self._exchange_manager.withdraw_smart("USDT", 1.0)
                    if net is None:
                        await update.message.reply_text(
                            "❌ Нет настроенных адресов вывода.\n"
                            "Добавь хотя бы один адрес в .env и перезапусти бота:\n"
                            "WITHDRAWAL_ADDRESS_USDT_ARB / OP / TRC20 / ERC20"
                        )
                    elif ok:
                        addr_hint = f"{net.address[:8]}...{net.address[-6:]}"
                        fee_str = f"${net.fee:.4f}" if net.fee > 0 else "0"
                        await update.message.reply_text(
                            f"✅ $1 USDT отправлен\n"
                            f"Сеть: {net.chain} | fee {fee_str}\n"
                            f"Адрес: {addr_hint}\n"
                            "Маршрут вывода работает."
                        )
                    else:
                        addr_hint = f"{net.address[:8]}...{net.address[-6:]}"
                        await update.message.reply_text(
                            f"❌ Вывод не выполнен через {net.chain}\n"
                            f"Адрес: {addr_hint}\n"
                            "Проверь:\n"
                            "• API-ключ имеет право Withdraw\n"
                            "• Адрес добавлен в whitelist на Bybit\n"
                            "• USDT находятся в Funding wallet (не Unified)"
                        )
                except Exception as e:
                    await update.message.reply_text(f"❌ Ошибка вывода: {e}")

        elif self._pending_confirm:
            self._pending_confirm = None
            await update.message.reply_text("❌ Отменено.")

    # ------------------------------------------------------------------
    # Запуск
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._app = (
            Application.builder()
            .token(self._token)
            .build()
        )
        self._app.add_handler(CommandHandler("status", self.cmd_status))
        self._app.add_handler(CommandHandler("stopall", self.cmd_stopall))
        self._app.add_handler(CommandHandler("resume", self.cmd_resume))
        self._app.add_handler(CommandHandler("profit", self.cmd_profit))
        self._app.add_handler(CommandHandler("sentinel", self.cmd_sentinel))
        self._app.add_handler(CommandHandler("exchanges", self.cmd_exchange_status))
        self._app.add_handler(CommandHandler("withdraw_test", self.cmd_withdraw_test))
        self._app.add_handler(CommandHandler("debug_drawdown", self.cmd_debug_drawdown))
        self._app.add_handler(CommandHandler("help", self.cmd_help))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text)
        )

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started.")
        await self.send("🚀 Crypto-Trader запущен. /help для команд.")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
