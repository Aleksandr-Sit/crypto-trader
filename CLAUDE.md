# crypto-trader — правила проекта

Правила выведены из реальных багов, найденных в аудитах.
Каждое правило нарушалось хотя бы раз и привело к реальному дефекту.

---

## Деплой

**После любого изменения `.py` файла:**
```bash
docker compose up --build -d bot
```
`restart` без `--build` не подхватывает изменения Python-кода.

**Проверка после деплоя:**
```bash
docker compose logs bot --tail 40
```
Ожидать: `Running 10 tasks. System ready.` (10 — с 05.07.2026, после отключения
breakout/scalper и добавления TSM; при изменении состава ботов число меняется)
и `State restored from Redis.` у каждого бота.

**SSH:**
- Hostkey (основной, crypto-trader): `ssh vps-trader` → root@151.244.251.34
- Senko (watchdog): `ssh vps-senko` → root@31.77.160.135, ключ `~/.ssh/vpn_key`

---

## asyncio — правила

### CancelledError — всегда перебрасывать
```python
# ПРАВИЛЬНО
except asyncio.CancelledError:
    raise

# НЕПРАВИЛЬНО — глотает отмену задачи
except Exception as e:
    logger.error(e)
```
`asyncio.CancelledError` является `BaseException`, не `Exception`. `except Exception` его не поймает — но `except BaseException` поймает и скроет. Явный `raise` обязателен.

### Долгоживущие мониторы — только `while True:`
```python
# ПРАВИЛЬНО — монитор выживает после emergency_stop + /resume
while True:
    ...

# НЕПРАВИЛЬНО — монитор умирает при первом стопе и не оживает
while self._emergency.is_running():
    ...
```
Мониторы (price_monitor, sentinel) должны жить независимо от состояния системы.
Проверка `is_running()` должна быть ВНУТРИ тела цикла — не в условии `while`.

### asyncio-задачи должны держаться живыми
```python
# Если задача — сервер (HTTP и т.п.), держи её:
try:
    await asyncio.Future()
except asyncio.CancelledError:
    await runner.cleanup()
    raise
```
Корутина, которая вернула `None`, завершила asyncio-задачу. Сервер остановится, но задача
в `gather()` пометится как завершённая без ошибки — это баг, не норма.

### `get_running_loop()`, не `get_event_loop()`
```python
# Python 3.10+: get_event_loop() устарел внутри корутин
loop = asyncio.get_running_loop()   # правильно
loop = asyncio.get_event_loop()     # устарело
```

---

## Emergency Stop — архитектура паузы

### Боты ПАУЗИРУЮТСЯ при emergency, а не умирают

`BaseBot._run_loop` реализован через паузу:
```python
while self._status == BotStatus.RUNNING:     # ← только явный stop() меняет статус
    if not self._emergency.is_running():
        await asyncio.sleep(5)               # ← пауза, не выход
        continue
    # ... tick() ...
```

**НЕПРАВИЛЬНО** — вызовет смерть бота при emergency:
```python
while self.is_running():   # is_running() = status AND emergency.is_running()
    await self.tick()      # при emergency loop выходит → задача завершена навсегда
```

**Следствие:** `/resume` работает только если боты паузированы, не завершены.
После `EmergencyStop.resume()` бот автоматически выходит из sleep-цикла и продолжает торговлю.

### `_on_stop()` должен вызываться и при нормальном выходе

```python
async def _run_loop(self) -> None:
    try:
        while self._status == BotStatus.RUNNING:
            ...
        await self._on_stop()       # ← нормальный выход (explicit stop)
    except asyncio.CancelledError:
        await self._on_stop()       # ← Docker kill / graceful shutdown
        raise
```

Если `_on_stop()` пропущен при нормальном выходе — финальное состояние не сохраняется в Redis.

### Grid Bot: все ордера пропали разом = emergency cancel, не fills

```python
# В _sync_live_fills: если все tracked-ордера исчезли с биржи —
# это emergency cancel, а не исполнение. Иначе — фиктивные прибыли.
if len(self._active_orders) >= 2 and not any(
    ao.order_id in open_ids for ao in self._active_orders.values()
):
    self._active_orders.clear()
    self._initialized = False
    await self._save_state()
    return
```

При нормальной торговле ордера исполняются по одному, и сразу ставится встречный.
Одновременное исчезновение всех ордеров = внешняя отмена (emergency). Не трактовать как fills.

---

## Персистентность состояния (Redis)

### После каждой мутации — немедленно `_save_state()`
```python
self._trades[symbol] = trade
await self._save_state()   # ← обязательно после КАЖДОГО изменения

del self._trades[symbol]
await self._save_state()   # ← и после удаления тоже
```
Вопрос для каждой строки мутации: *«если docker упадёт прямо здесь — что потеряется?»*
Если ответ «сделка/позиция/счётчик» — нужен `_save_state()`.

### Каждый счётчик/флаг/дата — в snapshot И в restore
```python
# get_state_snapshot():
return {
    "total_trades":   self._total_trades,    # ← не забыть
    "rebuild_date":   self._rebuild_date,    # ← не забыть
    "rebuilds_today": self._rebuilds_today,  # ← не забыть
}

# restore_state():
self._total_trades   = saved.get("total_trades", 0)
self._rebuild_date   = saved.get("rebuild_date", "")
self._rebuilds_today = saved.get("rebuilds_today", 0)
```
Поле в snapshot без restore = потеря счётчика при каждом рестарте.
Поле в restore без snapshot = поле всегда `None` после рестарта.

---

## Bybit SDK

### `category` — всегда явно
```python
# ПРАВИЛЬНО
self._client.cancel_all_orders(category="spot", symbol="BTCUSDT")
self._client.get_positions(category="linear", settleCoin="USDT")

# НЕПРАВИЛЬНО — category="spot" по умолчанию не всегда подходит
self._client.cancel_all_orders(symbol="BTCUSDT")
```

### `reduceOnly=True` — обязателен для ВСЕХ закрытий перп-позиций
```python
self._client.place_order(
    category="linear",
    symbol=symbol,
    side="Sell",
    orderType="Market",
    qty=size,
    reduceOnly=True,   # ← без этого открывается новая позиция, а не закрывается
)
```

### Глобальная отмена ордеров — и spot, и linear
```python
# Emergency Stop → отменять ОБА типа
await self._exchange.cancel_all_orders()          # без symbol → spot + linear

# Отмена конкретного символа → только spot (grid rebalance)
await self._exchange.cancel_all_orders(symbol="BTCUSDT")   # только spot
```
`cancel_all_orders(category="spot")` не трогает фьючерсные limit-ордера.
При глобальном стопе они остаются на бирже и открывают позиции.

### Sizing для перп (с плечом)
```python
# ПРАВИЛЬНО — notional = capital * leverage
qty = math.floor(
    (capital_per_leg * leverage / price) * 10 ** QTY_PRECISION
) / 10 ** QTY_PRECISION

# НЕПРАВИЛЬНО — qty без плеча (в 3× раз меньше нужного)
qty = capital / price
```
Если `leverage` уже заложен в `qty` — не умножай на него PnL отдельно.

---

## Telegram — уведомления

### `send()` применяет `html.escape()` — HTML-теги запрещены
```python
# ПРАВИЛЬНО — уведомления бота без HTML
msg = (
    "SENTINEL УРОВЕНЬ 1 — НАБЛЮДЕНИЕ\n"   # CAPS вместо <b>
    f"Заголовок: {headline}"
)
await self._notify(msg)   # через send() → escape → OK

# НЕПРАВИЛЬНО — <b> превратится в &lt;b&gt;
msg = f"<b>SENTINEL УРОВЕНЬ 1</b>\n{headline}"
await self._notify(msg)
```

### HTML в Telegram только через `reply_text()` напрямую
```python
# Команды /status, /profit и т.д. — HTML OK (не идут через send())
await update.message.reply_text(
    f"<b>Статус</b>\nPnL: {pnl:.2f}",
    parse_mode="HTML"
)
```

---

## Grid Bot — особые правила

### Перед инициализацией сетки — отменить старые ордера
```python
async def _initialize_grid(self, price: float) -> None:
    if not self._paper:
        try:
            await self._exchange.cancel_all_orders(self._symbol)
        except Exception as e:
            logger.warning(f"Pre-init cancel error: {e}")
    # ... далее выставлять новые ордера
```
Если рестарт произошёл в середине rebalance — старые ордера остаются на бирже.
Без предварительной отмены новые ордера создадут дубликаты.

---

## asyncio.gather() — BaseException, не Exception

```python
outcomes = await asyncio.gather(*coros, return_exceptions=True)
for name, outcome in zip(names, outcomes):
    if isinstance(outcome, BaseException):   # ПРАВИЛЬНО
        logger.error(f"Error on {name}: {outcome}")
    # НЕПРАВИЛЬНО: isinstance(outcome, Exception)
    # asyncio.CancelledError — BaseException, не Exception.
    # При isinstance(Exception) отменённая задача пройдёт как результат int → TypeError.
```

Правило: **всегда** `isinstance(outcome, BaseException)` при разборе gather-результатов.

---

## _save_state() — в том числе после инициализации

```python
async def _initialize_grid(self, price: float) -> None:
    # ... расставить ордера ...
    self._initialized    = True
    self._last_rebalance = time.time()
    await self._save_state()   # ← ОБЯЗАТЕЛЬНО после завершения инита
```

Если бот падает сразу после инициализации и до первого тика — Redis содержит старое состояние
(`_initialized = False`, старые order_id). При рестарте бот заново инициализируется и создаёт
дубли ордеров.

Правило: `_save_state()` после **каждого перехода состояния**, включая завершение `_initialize_*`.

---

## Файлы с Unicode — всегда encoding="utf-8"

```python
# ПРАВИЛЬНО
with open(path, encoding="utf-8") as f:
    data = yaml.safe_load(f)

# НЕПРАВИЛЬНО — Windows открывает как cp1251, UTF-8 с кириллицей → UnicodeDecodeError
with open(path) as f:
    data = yaml.safe_load(f)
```

Затронутые файлы проекта: `sentinel/keywords.yaml`, `config.yaml`.
Правило: **все** `open()` для файлов проекта — с `encoding="utf-8"`.

---

## Комиссии (fee tracking) — шаблон для каждого бота

Каждый торговый бот обязан отслеживать комиссии. Шаблон:

```python
# __init__:
self._fee_pct: float        = config.get("fee_pct", 0.0020)   # из config.yaml
self._total_fees_usdt: float = 0.0

# При закрытии позиции (_close / _exit):
fee_usdt  = self._fee_pct * notional   # notional = qty * entry_price
gross_pnl = ...
net_pnl   = gross_pnl - fee_usdt
self._total_fees_usdt += fee_usdt
self._total_pnl       += net_pnl       # в PnL всегда net, не gross

# get_state_snapshot() — добавить:
"total_fees_usdt": self._total_fees_usdt,

# restore_state() — добавить:
self._total_fees_usdt = saved.get("total_fees_usdt", 0.0)

# get_stats() — добавить:
"total_fees_usdt": round(self._total_fees_usdt, 4),
```

Значения `fee_pct` по типам ордеров (Bybit):
- Grid (maker-maker, spot):      `-0.0002` (rebate)
- Scalper (maker вход, смешанный выход): `0.0009`
- Breakout (market-market, perp × 2): `0.0022`
- StatArb (4 market-ордера, 2 ноги):  `0.0040`
- FundingArb (4 market-ордера):        `0.0040`

---

## Тесты — asyncio.run(), не get_event_loop()

```python
# ПРАВИЛЬНО — Python 3.10+
result = asyncio.run(some_coroutine())

# НЕПРАВИЛЬНО — Python 3.12+ выбрасывает RuntimeError в тестах
result = asyncio.get_event_loop().run_until_complete(some_coroutine())
```

При нескольких async-вызовах в одном тесте — оборачивай в inner async function:
```python
def test_state_roundtrip(self):
    async def _run():
        snap = await bot.get_state_snapshot()
        await ss.set_bot_state("name", snap)
        bot2 = make_bot(...)
        saved = await ss.get_bot_state("name")
        await bot2.restore_state(saved)
        return bot2
    bot2 = asyncio.run(_run())
    assert bot2._total_pnl == pytest.approx(77.0)
```

Мокировать `pandas`/`ta` перед импортом ботов (pandas нет локально):
```python
import sys
sys.modules.setdefault("pandas", MagicMock())
sys.modules.setdefault("ta", MagicMock())
sys.modules.setdefault("ta.volatility", MagicMock())
sys.modules.setdefault("ta.momentum", MagicMock())
# ← это должно быть в начале файла, ДО любых from bots.* import
```

---

## Чек-лист при добавлении нового бота или фичи

- [ ] Цикл бота: `while self._status == BotStatus.RUNNING:` + пауза при `not emergency.is_running()`
- [ ] `_on_stop()` вызывается и при нормальном выходе, и при `CancelledError`
- [ ] Все мониторы (не боты) используют `while True:`, а не `while flag:`
- [ ] `asyncio.CancelledError` явно перебрасывается во всех `try/except`
- [ ] `_save_state()` после КАЖДОЙ мутации И после завершения `_initialize_*()`
- [ ] Все поля класса есть в `get_state_snapshot()` AND `restore_state()`
- [ ] `fee_pct` читается из конфига; `total_fees_usdt` — в __init__ / snapshot / restore / stats
- [ ] PnL в `_close()/_exit()` — всегда **net** (gross - fee), никогда gross
- [ ] Все `open()` файлов — с `encoding="utf-8"`
- [ ] При `asyncio.gather(return_exceptions=True)` — проверять `isinstance(outcome, BaseException)`
- [ ] Уведомления через `send()` — без HTML тегов; в `reply_text(parse_mode="HTML")` — `&amp;` вместо `&`
- [ ] Perp close — всегда `reduceOnly=True`
- [ ] Sizing perp — `capital * leverage / price`, не `capital / price`
- [ ] При live init сетки — сначала отмена старых ордеров
- [ ] Global cancel (Emergency Stop) отменяет и spot, и linear
- [ ] Если бот отслеживает биржевые ордера: обработать случай "все пропали разом" (emergency cancel)
- [ ] Тесты: `asyncio.run()` вместо `get_event_loop()`; `sys.modules` mock для pandas/ta
