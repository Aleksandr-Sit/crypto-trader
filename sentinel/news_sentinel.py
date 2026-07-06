"""
News Sentinel — мониторинг угроз для российских пользователей.

Проверяет новости каждые 15 минут.
При обнаружении угрозы — автоматически выполняет протокол по уровню.

Уровень 1: уведомление → ничего не делаем
Уровень 2: снизить позиции, вывести 30% USDT на кошелёк
Уровень 3: закрыть всё, вывести всё на кошелёк
"""

import asyncio
import html as _html
import time
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable
from pathlib import Path
from loguru import logger
import yaml

from sentinel.sources.cryptopanic import CryptoPanicSource
from sentinel.sources.rss_monitor import RSSMonitor
from sentinel.analyzer import NewsAnalyzer, ThreatLevel


NotifyCallback = Callable[[str], Awaitable[None]]


@dataclass
class ThreatEvent:
    level: ThreatLevel
    source: str
    headline: str
    url: str
    timestamp: float
    matched_keyword: str


class NewsSentinel:
    CHECK_INTERVAL = 900          # 15 минут
    LEVEL2_WITHDRAW_PCT = 0.30    # Вывести 30% при уровне 2
    LEVEL2_REDUCE_PCT = 0.50      # Снизить позиции на 50% при уровне 2
    LEVEL_DECAY_HOURS = 4         # Через 4ч без новых триггеров уровень снижается на 1

    def __init__(
        self,
        cryptopanic_key: Optional[str],
        keywords_path: Optional[Path] = None,
    ):
        kw_path = keywords_path or Path(__file__).parent / "keywords.yaml"
        with open(kw_path, encoding="utf-8") as f:
            self._keywords = yaml.safe_load(f)

        self._analyzer = NewsAnalyzer(self._keywords)
        self._cryptopanic = (
            CryptoPanicSource(cryptopanic_key) if cryptopanic_key else None
        )
        self._rss = RSSMonitor(self._keywords.get("rss_feeds", []))

        self._current_level = ThreatLevel.NONE
        self._level_triggered_at: float = 0.0   # когда уровень был последний раз повышен
        self._last_check: float = 0.0
        self._recent_events: list[ThreatEvent] = []

        self._notify: Optional[NotifyCallback] = None
        self._emergency_stop = None
        self._exchange_manager = None

    def set_notifier(self, cb: NotifyCallback) -> None:
        self._notify = cb

    def set_emergency_stop(self, es) -> None:
        self._emergency_stop = es

    def set_exchange_manager(self, em) -> None:
        self._exchange_manager = em

    async def run(self) -> None:
        logger.info("News Sentinel started. Checking every 15 minutes.")
        while True:
            try:
                await self._check_cycle()
            except Exception as e:
                logger.error(f"Sentinel check error: {e}")
            await asyncio.sleep(self.CHECK_INTERVAL)

    async def _check_cycle(self) -> None:
        self._last_check = time.time()
        all_news: list[tuple[str, str, str]] = []  # (headline, url, source)

        # Собираем новости из всех источников
        if self._cryptopanic:
            try:
                cp_news = await self._cryptopanic.fetch()
                all_news.extend(cp_news)
            except Exception as e:
                logger.warning(f"CryptoPanic fetch failed: {e}")

        try:
            rss_news = await self._rss.fetch()
            all_news.extend(rss_news)
        except Exception as e:
            logger.warning(f"RSS fetch failed: {e}")

        max_level = ThreatLevel.NONE
        triggering_event: Optional[ThreatEvent] = None

        for headline, url, source in all_news:
            level, keyword = self._analyzer.analyze(headline)
            if level > max_level:
                max_level = level
                triggering_event = ThreatEvent(
                    level=level,
                    source=source,
                    headline=headline,
                    url=url,
                    timestamp=time.time(),
                    matched_keyword=keyword,
                )

        now = time.time()

        # Decay: если прошло LEVEL_DECAY_HOURS без новых триггеров — снижаем уровень на 1
        if (
            self._current_level > ThreatLevel.NONE
            and max_level < self._current_level
            and self._level_triggered_at > 0
            and now - self._level_triggered_at > self.LEVEL_DECAY_HOURS * 3600
        ):
            prev_level = self._current_level
            self._current_level = ThreatLevel(self._current_level - 1)
            self._level_triggered_at = now  # сбрасываем таймер для следующего шага decay
            logger.info(
                f"Sentinel: уровень угрозы снижен {prev_level.name} → "
                f"{self._current_level.name} (нет триггеров за {self.LEVEL_DECAY_HOURS}ч)"
            )
            await self._send(
                f"Sentinel: уровень угрозы снижен\n"
                f"{prev_level.name} → {self._current_level.name}\n"
                f"Нет новых срабатываний за {self.LEVEL_DECAY_HOURS} часов."
            )

        if max_level > self._current_level:
            self._current_level = max_level
            self._level_triggered_at = now
            if triggering_event:
                self._recent_events.append(triggering_event)
                self._recent_events = self._recent_events[-20:]  # Держим 20 последних
                await self._respond(triggering_event)

    async def _respond(self, event: ThreatEvent) -> None:
        # Сообщения идут через telegram.send() → html.escape() → нельзя использовать
        # HTML-теги. Используем CAPSLOCK + эмодзи для визуального выделения.
        level = event.level

        if level == ThreatLevel.WATCH:
            msg = (
                f"👁 SENTINEL УРОВЕНЬ 1 — НАБЛЮДЕНИЕ\n"
                f"Источник: {event.source}\n"
                f"Заголовок: {event.headline}\n"
                f"Ключевое слово: {event.matched_keyword}\n"
                f"Ссылка: {event.url}\n\n"
                f"Действий не требуется. Слежу за развитием."
            )
            await self._send(msg)

        elif level == ThreatLevel.CAUTION:
            msg = (
                f"⚠️ SENTINEL УРОВЕНЬ 2 — ТРЕВОГА\n"
                f"Источник: {event.source}\n"
                f"Заголовок: {event.headline}\n\n"
                f"Автоматически: снижаю позиции 50%, вывожу 30% USDT..."
            )
            await self._send(msg)
            await self._execute_level2()

        elif level == ThreatLevel.DANGER:
            msg = (
                f"🚨 SENTINEL УРОВЕНЬ 3 — ЭКСТРЕННЫЙ ВЫВОД\n"
                f"Источник: {event.source}\n"
                f"Заголовок: {event.headline}\n\n"
                f"Закрываю ВСЁ и вывожу на холодный кошелёк!"
            )
            await self._send(msg)
            await self._execute_level3()

    async def _execute_level2(self) -> None:
        """Снизить позиции, вывести 30% USDT через оптимальную сеть."""
        try:
            if not self._exchange_manager:
                await self._send("Уровень 2: exchange_manager не подключён — вывод невозможен!")
                return
            balance = await self._exchange_manager.get_total_balance_usdt()
            withdraw_amount = round(balance * self.LEVEL2_WITHDRAW_PCT, 2)
            logger.warning(f"Level 2: withdrawing ${withdraw_amount:.2f} USDT ({self.LEVEL2_WITHDRAW_PCT*100:.0f}%)")
            ok, net = await self._exchange_manager.withdraw_smart("USDT", withdraw_amount)
            if net is None:
                await self._send(
                    f"ОШИБКА вывода Уровень 2: нет настроенных адресов вывода!\n"
                    f"Выведи ${withdraw_amount:.2f} USDT ВРУЧНУЮ на Bybit."
                )
                return
            addr_hint = f"{net.address[:8]}...{net.address[-6:]}"
            fee_str = f"${net.fee:.4f}" if net.fee > 0 else "0"
            if ok:
                await self._send(
                    f"Уровень 2 ВЫВОД: ${withdraw_amount:.2f} USDT отправлен\n"
                    f"Сеть: {net.chain} (fee {fee_str}) | Адрес: {addr_hint}\n"
                    f"Снижение позиций на 50%: выполни ВРУЧНУЮ на Bybit."
                )
            else:
                await self._send(
                    f"ОШИБКА вывода ${withdraw_amount:.2f} USDT через {net.chain} (Уровень 2)!\n"
                    f"API-ключ не имеет права Withdraw или адрес не в whitelist.\n"
                    f"Выведи ВРУЧНУЮ на Bybit. Адрес: {addr_hint}"
                )
        except Exception as e:
            logger.error(f"Level 2 execution error: {e}")
            await self._send(f"Ошибка автовывода Уровень 2: {e}\nПроверь вручную!")

    async def _execute_level3(self) -> None:
        """Закрыть всё и вывести всё через оптимальную сеть."""
        try:
            if self._emergency_stop:
                from core.emergency_stop import StopReason
                await self._emergency_stop.trigger(
                    StopReason.SENTINEL_EMERGENCY,
                    "Критическая новость об ограничении биржи",
                    force=True,
                )
            # Даём 15 секунд на закрытие позиций биржей
            await asyncio.sleep(15)
            if not self._exchange_manager:
                await self._send("Уровень 3: exchange_manager не подключён — вывод невозможен!")
                return
            balance = await self._exchange_manager.get_total_balance_usdt()
            # Оставляем 10 USDT на сетевые комиссии
            withdraw_amount = max(0.0, round(balance - 10.0, 2))
            if withdraw_amount < 1.0:
                await self._send(
                    f"Уровень 3: баланс слишком мал для автовывода (${balance:.2f}).\n"
                    "Выведи ВРУЧНУЮ на Bybit."
                )
                return
            logger.warning(f"Level 3: withdrawing ${withdraw_amount:.2f} USDT (all)")
            ok, net = await self._exchange_manager.withdraw_smart("USDT", withdraw_amount)
            if net is None:
                await self._send(
                    f"КРИТИЧНО: Нет настроенных адресов вывода!\n"
                    f"НЕМЕДЛЕННО выведи ${withdraw_amount:.2f} USDT вручную на Bybit."
                )
                return
            addr_hint = f"{net.address[:8]}...{net.address[-6:]}"
            fee_str = f"${net.fee:.4f}" if net.fee > 0 else "0"
            if ok:
                await self._send(
                    f"Уровень 3 ВЫВОД: ${withdraw_amount:.2f} USDT отправлен\n"
                    f"Сеть: {net.chain} (fee {fee_str}) | Адрес: {addr_hint}"
                )
            else:
                await self._send(
                    f"КРИТИЧНО: Автовывод ${withdraw_amount:.2f} USDT через {net.chain} НЕ ВЫПОЛНЕН!\n"
                    f"API-ключ не имеет права Withdraw или адрес не в whitelist.\n"
                    f"НЕМЕДЛЕННО выведи вручную на Bybit. Адрес: {addr_hint}"
                )
        except Exception as e:
            logger.error(f"Level 3 execution error: {e}")
            await self._send(
                f"КРИТИЧНО: Уровень 3 не выполнен: {e}\n"
                "НЕМЕДЛЕННО выведи средства вручную на Bybit!"
            )

    def get_status_report(self) -> str:
        level_emoji = {
            ThreatLevel.NONE: "🟢",
            ThreatLevel.WATCH: "🟡",
            ThreatLevel.CAUTION: "🟠",
            ThreatLevel.DANGER: "🔴",
        }
        emoji = level_emoji.get(self._current_level, "⚪")
        last = (
            time.strftime("%H:%M:%S", time.localtime(self._last_check))
            if self._last_check else "не проверялось"
        )
        lines = [
            "📡 <b>News Sentinel</b>",
            f"Уровень угрозы: {emoji} {self._current_level.name}",
            f"Последняя проверка: {last}",
            f"Событий: {len(self._recent_events)}",
        ]
        if self._recent_events:
            last_event = self._recent_events[-1]
            lines.append(f"\nПоследнее: {_html.escape(last_event.headline[:80])}...")
        return "\n".join(lines)

    async def _send(self, text: str) -> None:
        if self._notify:
            try:
                await self._notify(text)
            except Exception as e:
                logger.error(f"Sentinel notify failed: {e}")
