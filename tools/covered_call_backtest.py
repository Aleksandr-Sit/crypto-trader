"""covered_call_backtest.py — стадия 1 трека D (13_COVERED_CALL_SELECTION).

Ключевая честность модели:
  - путь цены и пайоффы max(S_T-K,0) — РЕАЛЬНЫЕ (Bybit spot BTCUSDT 1D);
  - модельная только премия: Black-Scholes(r=0) c sigma = RV30 * kappa,
    kappa калибруется по живому стакану Bybit options на дату запуска
    (медиана IV_mid/RV30 по коллам +5..+15% OTM ближайшей месячной серии);
  - издержки: fee 0.02% нотионала + haircut премии за спред (1/2/5% для
    d=5/10/15%), спот-нога 0.085% на переключение фильтра.

Варианты: U (безусловный) и F (20d-Donchian flatten, решения на роллах),
d in {5,10,15}%, kappa in {0.9, kappa_calib}. Пороги PASS — в 13_*.md.

Запуск: py -3 tools/covered_call_backtest.py
Вывод ASCII; сырьё в data/research/covered_call/.
"""

import json
import math
import os
import time
import urllib.request
from datetime import datetime, timezone

BASE = "https://api.bybit.com"
START_MS = int(datetime(2020, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "research", "covered_call")
FEE_NOTIONAL = 0.0002          # Bybit options: 0.02% нотионала за сделку
SPOT_SWITCH_COST = 0.00085     # taker+slip на вход/выход спота по фильтру
HAIRCUTS = {0.05: 0.01, 0.10: 0.02, 0.15: 0.05}
ROLL_BARS = 30
RV_WINDOW = 30
MONTH_MAP = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def get(path: str, **params) -> dict:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{BASE}{path}?{q}",
                                 headers={"User-Agent": "cc-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode())
    if data.get("retCode") != 0:
        raise RuntimeError(f"{data.get('retCode')}: {data.get('retMsg')}")
    time.sleep(0.05)
    return data["result"]


def fetch_daily_closes() -> list[tuple[int, float]]:
    rows: dict[int, float] = {}
    end = int(time.time() * 1000)
    while True:
        res = get("/v5/market/kline", category="spot", symbol="BTCUSDT",
                  interval="D", end=end, limit=1000)
        batch = res["list"]
        if not batch:
            break
        for it in batch:
            rows[int(it[0])] = float(it[4])
        oldest = min(int(it[0]) for it in batch)
        if oldest <= START_MS or len(batch) < 1000:
            break
        end = oldest - 1
    out = sorted((ts, c) for ts, c in rows.items() if ts >= START_MS)
    return out


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(s: float, k: float, t_years: float, sigma: float) -> float:
    if sigma <= 0 or t_years <= 0:
        return max(s - k, 0.0)
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(s / k) + 0.5 * sigma * sigma * t_years) / sq
    return s * norm_cdf(d1) - k * norm_cdf(d1 - sq)


def implied_vol(price: float, s: float, k: float, t_years: float) -> float:
    lo, hi = 0.05, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_call(s, k, t_years, mid) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def parse_expiry(symbol: str) -> datetime:
    # BTC-31JUL26-70000-C-USDT
    token = symbol.split("-")[1]
    day = int(token[:-5])
    mon = MONTH_MAP[token[-5:-2]]
    year = 2000 + int(token[-2:])
    return datetime(year, mon, day, 8, 0, tzinfo=timezone.utc)  # экспирация 08:00 UTC


def calibrate_kappa(spot: float, rv30_now: float) -> tuple[float, list[dict]]:
    res = get("/v5/market/tickers", category="option", baseCoin="BTC")["list"]
    now = datetime.now(tz=timezone.utc)
    quotes = []
    for it in res:
        sym = it["symbol"]
        if not sym.endswith("-C-USDT"):
            continue
        try:
            exp = parse_expiry(sym)
        except Exception:
            continue
        dte = (exp - now).days
        if not 20 <= dte <= 45:
            continue
        k = float(sym.split("-")[2])
        if not 1.05 <= k / spot <= 1.15:
            continue
        bid, ask = float(it["bid1Price"] or 0), float(it["ask1Price"] or 0)
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2
        iv = implied_vol(mid, spot, k, dte / 365.0)
        quotes.append({"symbol": sym, "dte": dte, "strike": k, "mid": mid,
                       "iv": iv, "iv_over_rv": iv / rv30_now})
    if not quotes:
        raise RuntimeError("no live quotes 5-15% OTM, 20-45 dte")
    ratios = sorted(q["iv_over_rv"] for q in quotes)
    kappa = ratios[len(ratios) // 2]
    return kappa, quotes


def run_variant(closes: list[float], rv: list[float], state_on: list[bool],
                d: float, kappa: float, filtered: bool) -> dict:
    """Возвращает метрики варианта. Нотионал нормирован на 1 (слайс).

    F: дневная state-machine (Donchian 20 с гистерезисом). Внутримесячный
    выход = продажа спота + выкуп колла по модели (с haircut и fee) в день
    сигнала; возврат в рынок — только на дате ролла при state_on.
    """
    eq = 1.0
    peak, maxdd = 1.0, 0.0
    monthly = []       # (idx_settle, ret, prem_net_pct, assigned, active)
    holding = not filtered or state_on[RV_WINDOW + 21]
    switch_costs = 0.0
    start = RV_WINDOW + 21   # нужен RV30 и Donchian20 по закрытым барам
    t = start
    while t + ROLL_BARS < len(closes):
        s0 = closes[t]
        active = (state_on[t] or holding) if filtered else True
        if filtered and active and not holding:
            eq *= (1 - SPOT_SWITCH_COST)          # возврат в рынок на ролле
            switch_costs += SPOT_SWITCH_COST
            holding = True
        if not filtered:
            active = True
        if active:
            k = s0 * (1 + d)
            prem = bs_call(s0, k, ROLL_BARS / 365.0, rv[t] * kappa)
            prem_net = prem * (1 - HAIRCUTS[d]) - FEE_NOTIONAL * s0
            prem_pct = prem_net / s0
            exited_mid = False
            ret = 0.0
            if filtered:
                for j in range(t + 1, t + ROLL_BARS):
                    if not state_on[j]:
                        s_j = closes[j]
                        t_left = (t + ROLL_BARS - j) / 365.0
                        buyback = (bs_call(s_j, k, t_left, rv[j] * kappa)
                                   * (1 + HAIRCUTS[d]) + FEE_NOTIONAL * s_j)
                        ret = ((s_j - s0) / s0 + (prem_net - buyback) / s0
                               - SPOT_SWITCH_COST)
                        switch_costs += SPOT_SWITCH_COST
                        holding = False
                        exited_mid = True
                        break
            if not exited_mid:
                s_t = closes[t + ROLL_BARS]
                payoff = max(s_t - k, 0.0)
                ret = (s_t - s0) / s0 + (prem_net - payoff) / s0
            assigned = (not exited_mid) and closes[t + ROLL_BARS] > k
        else:
            ret, prem_pct, assigned = 0.0, 0.0, False
        eq *= (1 + ret)
        peak = max(peak, eq)
        maxdd = max(maxdd, 1 - eq / peak)
        monthly.append((t + ROLL_BARS, ret, prem_pct, assigned, active))
        t += ROLL_BARS
    n = len(monthly)
    act = [m for m in monthly if m[4]]
    prem_avg = sum(m[2] for m in monthly) / n if n else 0.0        # на весь период
    prem_avg_act = sum(m[2] for m in act) / len(act) if act else 0.0
    return {
        "equity_final": eq, "max_dd": maxdd, "months": n,
        "active_months": len(act),
        "assigned_share": sum(1 for m in act if m[3]) / len(act) if act else 0,
        "prem_net_pct_per_month": prem_avg,
        "prem_net_pct_per_active_month": prem_avg_act,
        "switch_costs": switch_costs,
        "monthly": monthly,
    }


def yearly_returns(monthly, timestamps) -> dict[str, float]:
    by_year: dict[str, float] = {}
    for idx, ret, *_ in monthly:
        y = datetime.fromtimestamp(timestamps[idx] / 1000, tz=timezone.utc).strftime("%Y")
        by_year[y] = by_year.get(y, 1.0) * (1 + ret)
    return {y: v - 1 for y, v in sorted(by_year.items())}


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    data = fetch_daily_closes()
    ts = [r[0] for r in data]
    closes = [r[1] for r in data]
    print(f"klines: {len(closes)} daily bars "
          f"{datetime.fromtimestamp(ts[0]/1000, tz=timezone.utc):%Y-%m-%d} -> "
          f"{datetime.fromtimestamp(ts[-1]/1000, tz=timezone.utc):%Y-%m-%d}")

    # RV30 (по закрытым барам: значение на t использует бары t-30..t-1)
    logret = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    rv = [0.0] * len(closes)
    for i in range(RV_WINDOW + 1, len(closes)):
        win = logret[i - RV_WINDOW:i]
        mean = sum(win) / len(win)
        var = sum((x - mean) ** 2 for x in win) / (len(win) - 1)
        rv[i] = math.sqrt(var * 365)

    # 20d-Donchian state-machine с гистерезисом (13_ §1, поправка 06.07):
    # off при закрытии ниже min(20 прошлых закрытий), on при закрытии выше
    # max(20 прошлых закрытий); между пробоями состояние держится.
    state_on = [True] * len(closes)
    cur = True
    for i in range(21, len(closes)):
        low20 = min(closes[i - 20:i])
        high20 = max(closes[i - 20:i])
        if cur and closes[i] < low20:
            cur = False
        elif not cur and closes[i] > high20:
            cur = True
        state_on[i] = cur

    spot_now = closes[-1]
    rv_now = rv[-1]
    kappa, quotes = calibrate_kappa(spot_now, rv_now)
    print(f"calibration: spot={spot_now:,.0f} RV30={rv_now*100:.1f}% "
          f"kappa={kappa:.3f} ({len(quotes)} live quotes)")
    for q in quotes[:6]:
        print(f"  {q['symbol']:<28} dte={q['dte']:<3d} mid={q['mid']:>7.1f} "
              f"IV={q['iv']*100:5.1f}%  IV/RV={q['iv_over_rv']:.3f}")

    results = {}
    print()
    print(f"{'variant':<22}{'prem%/mo':>9}{'eq_final':>10}{'maxDD':>8}"
          f"{'assigned':>10}{'2022':>8}{'2024+':>8}")
    for filtered in (False, True):
        for d in (0.05, 0.10, 0.15):
            for kap, kap_name in ((kappa, "cal"), (0.9, "0.9")):
                r = run_variant(closes, rv, state_on, d, kap, filtered)
                yr = yearly_returns(r["monthly"], ts)
                r["yearly"] = yr
                name = f"{'F' if filtered else 'U'} d={int(d*100)}% k={kap_name}"
                results[name] = r
                y2022 = yr.get("2022", 0.0)
                y24p = 1.0
                for y in ("2024", "2025", "2026"):
                    y24p *= 1 + yr.get(y, 0.0)
                print(f"{name:<22}{r['prem_net_pct_per_month']*100:>9.2f}"
                      f"{r['equity_final']:>10.3f}{r['max_dd']*100:>7.0f}%"
                      f"{r['assigned_share']*100:>9.0f}%{y2022*100:>7.0f}%"
                      f"{(y24p-1)*100:>7.0f}%")
    # бенчмарки
    hold = closes[-1] / closes[RV_WINDOW + 21]
    print(f"\nbenchmark pure hold BTC (same window): x{hold:.2f}")

    # Гейты (13_COVERED_CALL_SELECTION §3)
    base = {d: results[f"U d={int(d*100)}% k=cal"] for d in (0.05, 0.10, 0.15)}
    g1 = {d: base[d]["prem_net_pct_per_month"] >= 0.005 for d in base}
    f10 = results["F d=10% k=cal"]
    g2 = f10["max_dd"] <= 0.30
    g3 = f10["yearly"].get("2022", 0.0) >= -0.10
    pess = {d: results[f"U d={int(d*100)}% k=0.9"]["prem_net_pct_per_month"] >= 0.0035
            for d in (0.05, 0.10, 0.15)}
    g4 = (sum(1 for v in g1.values() if v) >= 2) and (sum(1 for v in pess.values() if v) >= 2)
    g1_view = {int(d * 100): v for d, v in g1.items()}
    print("\nPASS gates:")
    print(f"  1 premium >=0.5%/mo (kappa cal): {g1_view}")
    print(f"  2 F d=10% maxDD<=30%:           {f10['max_dd']*100:.0f}% -> "
          f"{'PASS' if g2 else 'FAIL'}")
    print(f"  3 F d=10% year 2022 >= -10%:    "
          f"{f10['yearly'].get('2022', 0.0)*100:+.1f}% -> {'PASS' if g3 else 'FAIL'}")
    print(f"  4 plateau + kappa=0.9 robust:   {'PASS' if g4 else 'FAIL'}")

    dump = {
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(),
        "spot_now": spot_now, "rv30_now": rv_now, "kappa": kappa,
        "quotes": quotes,
        "results": {k: {kk: vv for kk, vv in v.items() if kk != "monthly"}
                    for k, v in results.items()},
        # месячные серии — для портфельного MC (15_): [ts_ms, ret, prem, assigned, active]
        "monthly_series": {
            k: [[ts[m[0]], m[1], m[2], m[3], m[4]] for m in v["monthly"]]
            for k, v in results.items()
        },
        "gates": {"g1": {str(k): v for k, v in g1.items()},
                  "g2": g2, "g3": g3, "g4": g4},
    }
    with open(os.path.join(OUT_DIR, "cc_backtest.json"), "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=1)
    print(f"\nsaved: {os.path.join(OUT_DIR, 'cc_backtest.json')}")


if __name__ == "__main__":
    main()
