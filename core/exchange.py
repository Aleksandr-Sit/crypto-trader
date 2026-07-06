"""
Единый интерфейс для всех бирж.
Bybit — через официальный pybit SDK.
MEXC, OKX — через ccxt (запасные биржи).

Все торговые операции идут через этот модуль.
Withdrawal в API-ключах ОТКЛЮЧЁН намеренно —
вывод происходит только через whitelist-адреса на самой бирже.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from core.security import SecretsManager


def _new_link_id(prefix: str = "bot") -> str:
    """Уникальный orderLinkId: idempotency ключ для биржи."""
    return f"{prefix}-{int(time.time() * 1000) % 10_000_000_000}"


@dataclass
class Balance:
    coin: str
    available: float
    total: float


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str        # Buy / Sell
    order_type: str  # Limit / Market
    qty: float
    price: Optional[float]
    status: str


@dataclass
class Position:
    symbol: str
    side: str   # Buy / Sell
    size: float
    entry_price: float
    unrealized_pnl: float


class ExchangeAdapter(ABC):
    """Абстрактный интерфейс — все биржи реализуют одинаковые методы."""

    @abstractmethod
    async def get_balance(self, coin: str = "USDT") -> Balance: ...

    @abstractmethod
    async def get_ticker(self, symbol: str) -> float: ...

    @abstractmethod
    async def place_limit_order(
        self, symbol: str, side: str, qty: float, price: float
    ) -> Order: ...

    @abstractmethod
    async def place_market_order(
        self, symbol: str, side: str, qty: float
    ) -> Order: ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int: ...

    @abstractmethod
    async def get_open_positions(self) -> list[Position]: ...

    @abstractmethod
    async def close_all_positions(self) -> int: ...

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> list[Order]: ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> float: ...

    @abstractmethod
    async def get_funding_rates(self, symbols: list[str]) -> dict[str, float]: ...

    @abstractmethod
    async def place_perp_market_order(
        self, symbol: str, side: str, qty: float,
        stop_loss: Optional[float] = None,
    ) -> Order: ...

    @abstractmethod
    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict]: ...
    # interval: "1"=1m, "5"=5m, "60"=1h, "240"=4h, "D"=1d
    # returns list[{timestamp, open, high, low, close, volume}] sorted oldest→newest

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...
    # Устанавливает плечо для линейного (perp) рынка. Нет-оп в paper mode.

    @abstractmethod
    async def close_perp_position(self, symbol: str, side: str, qty: float) -> Order: ...
    # Закрывает перп-позицию через reduceOnly Market ордер.

    async def place_spot_stop_order(
        self, symbol: str, side: str, qty: float, trigger_price: float
    ) -> str:
        """Условный стоп-маркет ордер на спот. Возвращает orderId или '' если не поддерживается."""
        return ""

    async def get_adl_rank(self, symbol: str) -> int:
        """ADL-ранк позиции (1–5). 0 = не поддерживается биржей."""
        return 0

    async def get_instrument_meta(self, symbol: str) -> dict:
        """Метаданные инструмента для сайзинга (12_CARRY_WATCHLIST_SPEC §3).

        Ключи: spot_qty_step, spot_min_qty, perp_qty_step, perp_min_qty,
        perp_tick_size, funding_interval_sec. Пустой dict = не поддерживается
        адаптером или символа нет на обоих рынках (spot + linear).
        """
        return {}

    async def withdraw(
        self, coin: str, amount: float, address: str, chain: str = "TRC20"
    ) -> bool:
        """Вывод средств на whitelist-адрес. Реализуется в конкретном адаптере."""
        logger.warning(f"withdraw() not implemented for {type(self).__name__}")
        return False


# ---------------------------------------------------------------------------
# Bybit (основная биржа)
# ---------------------------------------------------------------------------

class BybitAdapter(ExchangeAdapter):
    def __init__(self, secrets: SecretsManager):
        from pybit.unified_trading import HTTP
        self._client = HTTP(
            testnet=secrets.is_testnet(),
            api_key=secrets.require("BYBIT_API_KEY"),
            api_secret=secrets.require("BYBIT_API_SECRET"),
        )
        self._paper = secrets.is_paper_mode()
        self._meta_cache: dict[str, dict] = {}
        logger.info(
            f"Bybit adapter ready. testnet={secrets.is_testnet()}, paper={self._paper}"
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def get_balance(self, coin: str = "USDT") -> Balance:
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.get_wallet_balance(accountType="UNIFIED", coin=coin),
        )
        coin_list = resp.get("result", {}).get("list", [{}])[0].get("coin", [])
        if not coin_list:
            return Balance(coin=coin, available=0.0, total=0.0)
        result = coin_list[0]
        return Balance(
            coin=coin,
            available=float(result.get("availableToWithdraw", 0.0) or 0.0),
            total=float(result.get("walletBalance", 0.0) or 0.0),
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    async def get_ticker(self, symbol: str) -> float:
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.get_tickers(category="spot", symbol=symbol),
        )
        return float(resp["result"]["list"][0]["lastPrice"])

    async def place_limit_order(
        self, symbol: str, side: str, qty: float, price: float
    ) -> Order:
        if self._paper:
            logger.info(f"[PAPER] Limit {side} {qty} {symbol} @ {price}")
            return Order(
                order_id="paper-0", symbol=symbol, side=side,
                order_type="Limit", qty=qty, price=price, status="Filled",
            )
        link_id = _new_link_id("lim")
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.place_order(
                category="spot", symbol=symbol,
                side=side, orderType="Limit",
                qty=str(qty), price=str(price),
                timeInForce="PostOnly",   # Maker-only: отклоняется если стал бы taker
                orderLinkId=link_id,
            ),
        )
        r = resp["result"]
        return Order(
            order_id=r["orderId"], symbol=symbol, side=side,
            order_type="Limit", qty=qty, price=price, status="New",
        )

    async def place_market_order(
        self, symbol: str, side: str, qty: float
    ) -> Order:
        if self._paper:
            price = await self.get_ticker(symbol)
            logger.info(f"[PAPER] Market {side} {qty} {symbol} @ ~{price}")
            return Order(
                order_id="paper-0", symbol=symbol, side=side,
                order_type="Market", qty=qty, price=price, status="Filled",
            )
        link_id = _new_link_id("mkt")
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.place_order(
                category="spot", symbol=symbol,
                side=side, orderType="Market", qty=str(qty),
                orderLinkId=link_id,
            ),
        )
        r = resp["result"]
        return Order(
            order_id=r["orderId"], symbol=symbol, side=side,
            order_type="Market", qty=qty, price=None, status="Filled",
        )

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        if self._paper:
            return 0
        total = 0

        # Всегда отменяем spot-ордера
        spot_kwargs: dict = {"category": "spot"}
        if symbol:
            spot_kwargs["symbol"] = symbol
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._client.cancel_all_orders(**spot_kwargs)
            )
            total += len(resp.get("result", {}).get("list", []))
        except Exception as e:
            logger.warning(f"Bybit: cancel spot orders error: {e}")

        # Без конкретного символа (Emergency Stop) — отменяем и linear-ордера тоже
        if not symbol:
            try:
                resp = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self._client.cancel_all_orders(
                        category="linear", settleCoin="USDT"
                    ),
                )
                total += len(resp.get("result", {}).get("list", []))
            except Exception as e:
                logger.warning(f"Bybit: cancel linear orders error: {e}")

        logger.info(f"Bybit: cancelled {total} orders total (symbol={symbol})")
        return total

    async def get_open_positions(self) -> list[Position]:
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.get_positions(category="linear", settleCoin="USDT"),
        )
        positions = []
        for p in resp["result"]["list"]:
            if float(p["size"]) > 0:
                positions.append(Position(
                    symbol=p["symbol"],
                    side=p["side"],
                    size=float(p["size"]),
                    entry_price=float(p["avgPrice"]),
                    unrealized_pnl=float(p["unrealisedPnl"]),
                ))
        return positions

    async def close_all_positions(self) -> int:
        if self._paper:
            return 0
        positions = await self.get_open_positions()
        count = 0
        for pos in positions:
            close_side = "Sell" if pos.side == "Buy" else "Buy"
            # Перп-позиции закрываем через reduceOnly — иначе spot-ордер не закроет linear
            await self.close_perp_position(pos.symbol, close_side, pos.size)
            count += 1
            logger.warning(f"Closed position: {pos.symbol} {pos.side} {pos.size}")
        return count

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    async def get_open_orders(self, symbol: str) -> list[Order]:
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.get_open_orders(
                category="spot", symbol=symbol, limit=50
            ),
        )
        orders = []
        for o in resp.get("result", {}).get("list", []):
            orders.append(Order(
                order_id=o["orderId"],
                symbol=o["symbol"],
                side=o["side"],
                order_type=o["orderType"],
                qty=float(o["qty"]),
                price=float(o["price"]) if o.get("price") else None,
                status=o["orderStatus"],
            ))
        return orders

    async def get_funding_rate(self, symbol: str) -> float:
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.get_funding_rate_history(
                category="linear", symbol=symbol, limit=1
            ),
        )
        return float(resp["result"]["list"][0]["fundingRate"])

    async def get_funding_rates(self, symbols: list[str]) -> dict[str, float]:
        """Batch: текущие ставки финансирования для списка символов."""
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.get_tickers(category="linear"),
        )
        sym_set = set(symbols)
        rates: dict[str, float] = {}
        for item in resp.get("result", {}).get("list", []):
            if item["symbol"] in sym_set:
                raw = item.get("fundingRate", "0") or "0"
                rates[item["symbol"]] = float(raw)
        return rates

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    async def get_instrument_meta(self, symbol: str) -> dict:
        """Шаги лота spot/perp, tickSize перпа, интервал funding (может быть 1–8h).

        Кэшируется на время жизни адаптера: метаданные меняются редко,
        рестарт контейнера обновляет кэш.
        """
        cached = self._meta_cache.get(symbol)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        spot_resp = await loop.run_in_executor(
            None,
            lambda: self._client.get_instruments_info(category="spot", symbol=symbol),
        )
        lin_resp = await loop.run_in_executor(
            None,
            lambda: self._client.get_instruments_info(category="linear", symbol=symbol),
        )
        spot_list = spot_resp.get("result", {}).get("list", [])
        lin_list = lin_resp.get("result", {}).get("list", [])
        if not spot_list or not lin_list:
            logger.warning(
                f"instrument meta {symbol}: нет spot- или linear-рынка на Bybit — "
                "carry по символу невозможен"
            )
            self._meta_cache[symbol] = {}
            return {}
        s_lot = spot_list[0].get("lotSizeFilter", {})
        l_lot = lin_list[0].get("lotSizeFilter", {})
        l_price = lin_list[0].get("priceFilter", {})
        meta = {
            "spot_qty_step": float(s_lot.get("basePrecision") or 0.000001),
            "spot_min_qty": float(s_lot.get("minOrderQty") or 0.0),
            "perp_qty_step": float(l_lot.get("qtyStep") or 0.000001),
            "perp_min_qty": float(l_lot.get("minOrderQty") or 0.0),
            "perp_tick_size": float(l_price.get("tickSize") or 0.0001),
            "funding_interval_sec": float(lin_list[0].get("fundingInterval") or 480) * 60.0,
        }
        self._meta_cache[symbol] = meta
        return meta

    async def place_perp_market_order(
        self, symbol: str, side: str, qty: float,
        stop_loss: Optional[float] = None,
    ) -> Order:
        """Рыночный ордер на линейный (перп) рынок. stop_loss выставляется на бирже."""
        if self._paper:
            price = await self.get_ticker(symbol)
            sl_info = f" SL={stop_loss:.4f}" if stop_loss else ""
            logger.info(f"[PAPER] Perp Market {side} {qty} {symbol} @ ~{price:.2f}{sl_info}")
            return Order("paper-perp-0", symbol, side, "Market", qty, price, "Filled")
        link_id = _new_link_id("perp")
        order_params: dict = {
            "category": "linear", "symbol": symbol,
            "side": side, "orderType": "Market", "qty": str(qty),
            "reduceOnly": False,
            "orderLinkId": link_id,
        }
        if stop_loss is not None:
            order_params["stopLoss"] = str(round(stop_loss, 4))
            order_params["slTriggerBy"] = "LastPrice"
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.place_order(**order_params),
        )
        r = resp["result"]
        return Order(r["orderId"], symbol, side, "Market", qty, None, "Filled")

    async def place_spot_stop_order(
        self, symbol: str, side: str, qty: float, trigger_price: float
    ) -> str:
        """Условный стоп-маркет ордер на спот. Возвращает orderId или '' в paper mode."""
        if self._paper:
            logger.info(
                f"[PAPER] Spot Stop {side} {qty} {symbol} trigger @ {trigger_price:.2f}"
            )
            return "paper-stop-0"
        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._client.place_order(
                    category="spot", symbol=symbol,
                    side=side, orderType="Market", qty=str(qty),
                    orderFilter="StopOrder",
                    triggerPrice=str(round(trigger_price, 2)),
                    triggerBy="LastPrice",
                ),
            )
            return resp["result"]["orderId"]
        except Exception as e:
            logger.warning(f"place_spot_stop_order {symbol}: {e}")
            return ""

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        if self._paper:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._client.set_leverage(
                    category="linear", symbol=symbol,
                    buyLeverage=str(leverage), sellLeverage=str(leverage),
                ),
            )
            logger.debug(f"Bybit: leverage {leverage}× set for {symbol}")
        except Exception as e:
            # Bybit возвращает ошибку если плечо уже такое — это не критично
            if "leverage not modified" not in str(e).lower():
                logger.warning(f"set_leverage({symbol}, {leverage}): {e}")

    async def close_perp_position(self, symbol: str, side: str, qty: float) -> Order:
        """Закрыть перп-позицию. reduceOnly=True гарантирует только закрытие, не реверс."""
        if self._paper:
            price = await self.get_ticker(symbol)
            logger.info(f"[PAPER] Close Perp {side} {qty} {symbol} @ ~{price:.2f}")
            return Order("paper-close-0", symbol, side, "Market", qty, price, "Filled")
        link_id = _new_link_id("cls")
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.place_order(
                category="linear", symbol=symbol,
                side=side, orderType="Market", qty=str(qty),
                reduceOnly=True,
                orderLinkId=link_id,
            ),
        )
        r = resp["result"]
        return Order(r["orderId"], symbol, side, "Market", qty, None, "Filled")

    async def get_adl_rank(self, symbol: str) -> int:
        """ADL-ранк позиции (1-5). Ранк 5 = высокий риск принудительного закрытия."""
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.get_positions(category="linear", symbol=symbol),
        )
        lst = resp["result"]["list"]
        if lst and lst[0].get("adlRankIndicator"):
            return int(lst[0]["adlRankIndicator"])
        return 0

    async def withdraw(
        self, coin: str, amount: float, address: str, chain: str = "TRC20"
    ) -> bool:
        """Вывод средств на whitelist-адрес.

        Требует: API-ключ с разрешением Withdraw + адрес в whitelist на Bybit.
        Средства выводятся из Funding-кошелька (не Unified).
        Если средства в Unified — предварительно переведи через Bybit UI.
        """
        if self._paper:
            logger.info(f"[PAPER] Withdraw {amount:.2f} {coin} → {address[:8]}... chain={chain}")
            return True
        try:
            import time as _t
            resp = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._client.withdraw(
                    coin=coin,
                    chain=chain,
                    address=address,
                    amount=str(round(amount, 2)),
                    timestamp=str(int(_t.time() * 1000)),
                    accountType="FUND",
                    feeType=0,
                ),
            )
            ret_code = resp.get("retCode", -1)
            if ret_code == 0:
                wd_id = resp.get("result", {}).get("id", "unknown")
                logger.info(
                    f"Withdraw OK: {amount:.2f} {coin} chain={chain} "
                    f"addr={address[:8]}... id={wd_id}"
                )
                return True
            logger.error(
                f"Withdraw failed: retCode={ret_code} msg={resp.get('retMsg')}"
            )
            return False
        except Exception as e:
            logger.error(f"Withdraw exception: {e}")
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=5))
    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict]:
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.get_kline(
                category="spot", symbol=symbol, interval=interval, limit=limit
            ),
        )
        result = []
        for k in resp["result"]["list"]:
            result.append({
                "timestamp": int(k[0]),
                "open":   float(k[1]),
                "high":   float(k[2]),
                "low":    float(k[3]),
                "close":  float(k[4]),
                "volume": float(k[5]),
            })
        return result[::-1]  # Bybit отдаёт newest first → разворачиваем

    async def get_coin_networks(self, coin: str = "USDT") -> list[dict]:
        """
        Список сетей для монеты с текущими комиссиями и статусом вывода.
        Возвращает: [{"chain": str, "fee": float, "withdraw_enabled": bool}]
        chain — Bybit chain ID: "ETH", "TRC20", "ARBI", "OP", etc.
        """
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._client.get_coin_info(coin=coin),
        )
        networks = []
        rows = resp.get("result", {}).get("rows", [])
        if rows:
            for net in rows[0].get("chains", []):
                chain_id = net.get("chain", "")
                if not chain_id:
                    continue
                networks.append({
                    "chain": chain_id,
                    "fee": float(net.get("withdrawFee", "999") or "999"),
                    "withdraw_enabled": net.get("chainWithdraw", "0") == "1",
                })
        return networks


# ---------------------------------------------------------------------------
# CCXT-адаптер (MEXC, OKX — запасные биржи)
# ---------------------------------------------------------------------------

class CCXTAdapter(ExchangeAdapter):
    def __init__(self, exchange_id: str, secrets: SecretsManager):
        import ccxt
        prefix = exchange_id.upper()
        exchange_class = getattr(ccxt, exchange_id)
        params: dict = {
            "apiKey": secrets.require(f"{prefix}_API_KEY"),
            "secret": secrets.require(f"{prefix}_API_SECRET"),
        }
        if exchange_id == "okx":
            params["password"] = secrets.require("OKX_PASSPHRASE")

        self._exchange = exchange_class(params)
        self._paper = secrets.is_paper_mode()
        self._id = exchange_id
        logger.info(f"{exchange_id.upper()} CCXT adapter ready.")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def get_balance(self, coin: str = "USDT") -> Balance:
        data = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._exchange.fetch_balance()
        )
        free = data.get(coin, {}).get("free", 0.0) or 0.0
        total = data.get(coin, {}).get("total", 0.0) or 0.0
        return Balance(coin=coin, available=float(free), total=float(total))

    async def get_ticker(self, symbol: str) -> float:
        data = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._exchange.fetch_ticker(symbol)
        )
        return float(data["last"])

    async def place_limit_order(
        self, symbol: str, side: str, qty: float, price: float
    ) -> Order:
        if self._paper:
            logger.info(f"[PAPER/{self._id}] Limit {side} {qty} {symbol} @ {price}")
            return Order("paper-0", symbol, side, "Limit", qty, price, "Filled")
        data = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._exchange.create_limit_order(
                symbol, side.lower(), qty, price
            ),
        )
        return Order(data["id"], symbol, side, "Limit", qty, price, data["status"])

    async def place_market_order(
        self, symbol: str, side: str, qty: float
    ) -> Order:
        if self._paper:
            price = await self.get_ticker(symbol)
            return Order("paper-0", symbol, side, "Market", qty, price, "Filled")
        data = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._exchange.create_market_order(symbol, side.lower(), qty),
        )
        return Order(data["id"], symbol, side, "Market", qty, None, data["status"])

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        if self._paper:
            return 0
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._exchange.cancel_all_orders(symbol),
        )
        return 0

    async def get_open_orders(self, symbol: str) -> list[Order]:
        data = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._exchange.fetch_open_orders(symbol)
        )
        return [
            Order(
                order_id=o["id"],
                symbol=o["symbol"],
                side=o["side"].capitalize(),
                order_type=o["type"].capitalize(),
                qty=float(o["amount"]),
                price=float(o["price"]) if o.get("price") else None,
                status=o["status"],
            )
            for o in data
        ]

    async def get_open_positions(self) -> list[Position]:
        return []

    async def close_all_positions(self) -> int:
        return 0

    async def get_funding_rate(self, symbol: str) -> float:
        return 0.0

    async def get_funding_rates(self, symbols: list[str]) -> dict[str, float]:
        rates: dict[str, float] = {}
        for sym in symbols:
            try:
                rates[sym] = await self.get_funding_rate(sym)
            except Exception:
                rates[sym] = 0.0
        return rates

    async def place_perp_market_order(
        self, symbol: str, side: str, qty: float,
        stop_loss: Optional[float] = None,
    ) -> Order:
        return await self.place_market_order(symbol, side, qty)

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        if self._paper:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._exchange.set_leverage(leverage, symbol)
            )
        except Exception as e:
            logger.warning(f"set_leverage({symbol}, {leverage}): {e}")

    async def close_perp_position(self, symbol: str, side: str, qty: float) -> Order:
        return await self.place_market_order(symbol, side, qty)

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict]:
        tf_map = {"1": "1m", "5": "5m", "15": "15m", "60": "1h", "240": "4h", "D": "1d"}
        tf = tf_map.get(interval, "1m")
        data = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._exchange.fetch_ohlcv(symbol, tf, limit=limit)
        )
        return [
            {"timestamp": d[0], "open": d[1], "high": d[2],
             "low": d[3], "close": d[4], "volume": d[5]}
            for d in data
        ]


# ---------------------------------------------------------------------------
# ExchangeManager — управляет всеми биржами сразу
# ---------------------------------------------------------------------------

class ExchangeManager:
    def __init__(self, secrets: SecretsManager):
        from core.withdrawal_router import WithdrawalRouter, NetworkOption as _NetworkOption
        self._NetworkOption = _NetworkOption

        self.adapters: dict[str, ExchangeAdapter] = {}
        self.primary = "bybit"

        self.adapters["bybit"] = BybitAdapter(secrets)

        if secrets.has_mexc():
            try:
                self.adapters["mexc"] = CCXTAdapter("mexc", secrets)
            except Exception as e:
                logger.warning(f"MEXC init failed: {e}")

        if secrets.has_okx():
            try:
                self.adapters["okx"] = CCXTAdapter("okx", secrets)
            except Exception as e:
                logger.warning(f"OKX init failed: {e}")

        self._router = WithdrawalRouter(secrets, self.adapters["bybit"])
        logger.info(f"ExchangeManager ready. Active: {list(self.adapters.keys())}")

    def get(self, name: str = "bybit") -> ExchangeAdapter:
        return self.adapters[name]

    async def close_all_everywhere(self) -> dict[str, int]:
        """Закрыть ВСЕ позиции на ВСЕХ биржах — используется Emergency Stop."""
        names = list(self.adapters.keys())
        coros = [self.adapters[n].close_all_positions() for n in names]
        outcomes = await asyncio.gather(*coros, return_exceptions=True)
        results = {}
        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, BaseException):
                logger.error(f"Error closing positions on {name}: {outcome}")
                results[name] = -1
            else:
                results[name] = outcome
        return results

    async def cancel_all_everywhere(self) -> dict[str, int]:
        """Отменить ВСЕ ордера на ВСЕХ биржах."""
        names = list(self.adapters.keys())
        coros = [self.adapters[n].cancel_all_orders() for n in names]
        outcomes = await asyncio.gather(*coros, return_exceptions=True)
        results = {}
        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, BaseException):
                logger.error(f"Error cancelling orders on {name}: {outcome}")
                results[name] = -1
            else:
                results[name] = outcome
        return results

    async def withdraw(
        self, coin: str, amount: float, address: str, chain: str = "TRC20"
    ) -> bool:
        """Вывод через основную биржу (Bybit)."""
        adapter = self.adapters.get(self.primary)
        if adapter is None:
            logger.error("withdraw: primary adapter not available")
            return False
        return await adapter.withdraw(coin, amount, address, chain)

    async def withdraw_smart(self, coin: str, amount: float):
        """
        Вывод с автовыбором дешевейшей доступной сети.
        Возвращает (success: bool, net: NetworkOption | None).
        net is None означает — нет настроенных адресов вывода.
        """
        net = await self._router.pick_best(coin)
        if net is None:
            return False, None
        ok = await self.withdraw(coin, amount, net.address, net.chain)
        return ok, net

    async def get_best_withdrawal_network(self, coin: str = "USDT"):
        """Запрашивает оптимальную сеть для вывода без исполнения вывода."""
        return await self._router.pick_best(coin)

    async def get_total_balance_usdt(self) -> float:
        """Суммарный баланс USDT по всем биржам."""
        total = 0.0
        for name, adapter in self.adapters.items():
            try:
                bal = await adapter.get_balance("USDT")
                total += bal.available
            except Exception as e:
                logger.warning(f"Cannot fetch balance from {name}: {e}")
        return total
