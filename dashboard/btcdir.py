"""BTC 5m / 15m direction: walk-forward tested rules, then a live call from the ones that held up.
Read-only, Hyperliquid public candles. Research is cached; a call is only as good as its out-of-sample test."""
import itertools
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-hl"))
import hl  # noqa: E402
from btc import ema  # noqa: E402

FEE = 0.0009 + 0.0002   # taker round trip plus a little slippage
HOLD = 48
MIN_TEST_TRADES = 15


def prep(cs, per_hour):
    c = [x[3] for x in cs]
    n = len(cs)
    d = {"cs": cs, "c": c, "e20": ema(c, 20), "e50": ema(c, 50), "rsi": [None] * n, "atr": [None] * n, "vavg": [None] * n,
         "hi20": [None] * n, "lo20": [None] * n, "htf": [0] * n}
    g = l = 0.0
    for i in range(1, n):
        dd = c[i] - c[i - 1]
        if i <= 14:
            g += max(dd, 0) / 14
            l += max(-dd, 0) / 14
        else:
            g = (g * 13 + max(dd, 0)) / 14
            l = (l * 13 + max(-dd, 0)) / 14
        if i >= 14:
            d["rsi"][i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    tr = [0.0] + [max(cs[i][1] - cs[i][2], abs(cs[i][1] - c[i - 1]), abs(cs[i][2] - c[i - 1])) for i in range(1, n)]
    for i in range(20, n):
        d["atr"][i] = sum(tr[i - 13:i + 1]) / 14
        d["vavg"][i] = sum(x[4] for x in cs[i - 20:i]) / 20
        d["hi20"][i] = max(x[1] for x in cs[i - 20:i])
        d["lo20"][i] = min(x[2] for x in cs[i - 20:i])
    # higher-timeframe trend: EMA20 vs EMA50 on 1h closes, no look-ahead (only completed hours)
    hc = [c[k] for k in range(per_hour - 1, n, per_hour)]
    he20, he50 = ema(hc, 20), ema(hc, 50)
    for i in range(n):
        j = (i + 1) // per_hour - 1
        if j >= 50:
            d["htf"][i] = 1 if (he20[j] > he50[j] and hc[j] > he20[j]) else -1 if (he20[j] < he50[j] and hc[j] < he20[j]) else 0
    return d


def s_breakout(d, i):
    if d["hi20"][i] is None or d["vavg"][i] in (None, 0) or d["cs"][i][4] < 1.5 * d["vavg"][i]:
        return 0
    return 1 if d["c"][i] > d["hi20"][i] else -1 if d["c"][i] < d["lo20"][i] else 0


def s_breakout_htf(d, i):
    s = s_breakout(d, i)
    return s if s and s == d["htf"][i] else 0


def s_pullback(d, i):
    """With the 1h trend: price dips through the 20 EMA and the candle closes back on the trend side."""
    if i < 52 or d["htf"][i] == 0:
        return 0
    c, e = d["c"], d["e20"]
    if d["htf"][i] == 1 and c[i - 1] < e[i - 1] and c[i] > e[i] and d["rsi"][i] and d["rsi"][i] < 60:
        return 1
    if d["htf"][i] == -1 and c[i - 1] > e[i - 1] and c[i] < e[i] and d["rsi"][i] and d["rsi"][i] > 40:
        return -1
    return 0


def s_fade(d, i):
    r = d["rsi"][i]
    if r is None:
        return 0
    return 1 if r < 22 else -1 if r > 78 else 0


STRATS = {"breakout": s_breakout, "breakout_with_1h_trend": s_breakout_htf, "pullback_with_1h_trend": s_pullback, "rsi_fade": s_fade}
GRID = list(itertools.product((1.0, 1.5, 2.0), (1.0, 1.5, 2.0)))  # stop ATRs, target R


def simulate(d, fn, lo, hi, stop_atr, tgt_r, only=0):
    """Trades with signal candle in [lo,hi). Enter next open; stop wins ties; one trade at a time."""
    cs, out, i = d["cs"], [], lo
    while i < min(hi, len(cs) - 2):
        s = fn(d, i) if d["atr"][i] else 0
        if s and (only == 0 or s == only):
            entry = cs[i + 1][0]
            risk = stop_atr * d["atr"][i]
            stop, tgt = entry - s * risk, entry + s * tgt_r * risk
            r, k = None, 0
            for k, (o, h, l, c, v) in enumerate(cs[i + 1:i + 1 + HOLD]):
                if (l <= stop) if s == 1 else (h >= stop):
                    r = -1.0
                    break
                if (h >= tgt) if s == 1 else (l <= tgt):
                    r = tgt_r
                    break
            if r is None:
                last = cs[min(i + HOLD, len(cs) - 1)][3]
                r = s * (last - entry) / risk
            out.append((s, r - FEE * entry / risk, i))
            i += k + 2
        else:
            i += 1
    return out


def stats(tr):
    n = len(tr)
    if not n:
        return {"trades": 0, "win_rate": None, "avg_R": None, "profit_factor": None}
    rs = [t[1] for t in tr]
    w, ls = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    return {"trades": n, "win_rate": round(sum(r > 0 for r in rs) / n, 3), "avg_R": round(sum(rs) / n, 3),
            "profit_factor": round(w / ls, 2) if ls else None}


def research(iv):
    per_hour = 12 if iv == "5m" else 4
    cs = hl.candles("BTC", iv, 5000)
    d = prep(cs, per_hour)
    n = len(cs)
    cut = int(n * 0.6)
    days = round(n * (5 if iv == "5m" else 15) / 1440, 1)
    results = []
    for name, fn in STRATS.items():
        for side, sname in ((1, "long"), (-1, "short")):
            best = None
            for sa, tr in GRID:
                trn = simulate(d, fn, 60, cut, sa, tr, side)
                st = stats(trn)
                if st["trades"] >= 20 and (best is None or st["avg_R"] > best[0]["avg_R"]):
                    best = (st, sa, tr)
            if not best:
                results.append({"strategy": name, "side": sname, "valid": False, "why": "too few trades in training"})
                continue
            test = stats(simulate(d, fn, cut, n, best[1], best[2], side))
            ok = test["trades"] >= MIN_TEST_TRADES and test["avg_R"] is not None and test["avg_R"] > 0.05 and best[0]["avg_R"] > 0
            results.append({"strategy": name, "side": sname, "stop_atr": best[1], "target_r": best[2], "train": best[0], "test": test,
                            "valid": bool(ok), "why": "" if ok else ("test negative or flat" if test["trades"] >= MIN_TEST_TRADES else "too few test trades")})
    return {"interval": iv, "candles": n, "days": days, "split": "first 60% to pick parameters, last 40% untouched to judge",
            "fee_assumed": FEE, "results": results}


_cache = {}


def get_research():
    global FEE
    if _cache.get("v") and time.time() - _cache["t"] < 1800:
        return _cache["v"]
    base = FEE
    v = {"as_of": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "tf": {iv: research(iv) for iv in ("5m", "15m")}, "fees": []}
    for label, fee in (("taker both ways (+slippage)", base), ("maker-ish, 0.03% round trip", 0.0003), ("zero fees (unrealistic)", 0.0)):
        FEE = fee
        row = {"label": label, "fee": fee}
        for iv in ("5m", "15m"):
            res = [q for q in research(iv)["results"] if "test" in q]
            row[iv] = {"valid": sum(q["valid"] for q in res),
                       "best_test_avg_R": max((q["test"]["avg_R"] for q in res if q["test"]["avg_R"] is not None), default=None)}
        v["fees"].append(row)
    FEE = base
    _cache.update(v=v, t=time.time())
    return v


def plans(d, i, iv, size=100.0, stop_atr=2.0, tgt_r=1.5):
    """Conditional entry/stop/target for both sides: enter when a candle closes beyond the 20-candle range."""
    a, px = d["atr"][i], d["c"][i]
    risk = stop_atr * a
    out = {}
    for side, sg, trig in (("long", 1, d["hi20"][i]), ("short", -1, d["lo20"][i])):
        entry = max(trig, px) if sg == 1 else min(trig, px)
        qty = size / entry
        out[side] = {"trigger": trig, "entry": entry, "stop": entry - sg * risk, "target": entry + sg * tgt_r * risk, "risk_pct": risk / entry,
                     "away_pct": abs(trig - px) / px, "notional": size, "loss_at_stop": qty * risk + size * FEE,
                     "gain_at_target": qty * tgt_r * risk - size * FEE, "fees": size * FEE, "time_exit_candles": HOLD,
                     "cancel_if": "a candle closes back %s %.2f before you are in" % ("below" if sg == 1 else "above", d["lo20"][i] if sg == 1 else d["hi20"][i])}
    return out


def live():
    out = {}
    for iv, ph in (("5m", 12), ("15m", 4)):
        d = prep(hl.candles("BTC", iv, 900), ph)
        i = len(d["cs"]) - 1
        c = d["c"]
        parts = {"price above 20 EMA": c[i] > d["e20"][i], "20 EMA above 50 EMA": d["e20"][i] > d["e50"][i],
                 "1h trend up": d["htf"][i] == 1, "last 12 candles up": c[i] > c[i - 12]}
        down = {"price below 20 EMA": c[i] < d["e20"][i], "20 EMA below 50 EMA": d["e20"][i] < d["e50"][i],
                "1h trend down": d["htf"][i] == -1, "last 12 candles down": c[i] < c[i - 12]}
        u, dn = sum(parts.values()), sum(down.values())
        out[iv] = {"price": c[i], "bias": "LONG" if u >= 3 and dn == 0 else "SHORT" if dn >= 3 and u == 0 else "NEUTRAL",
                   "up_points": u, "down_points": dn, "up": [k for k, v in parts.items() if v], "down": [k for k, v in down.items() if v],
                   "rsi": d["rsi"][i], "high20": d["hi20"][i], "low20": d["lo20"][i], "atr_pct": d["atr"][i] / c[i],
                   "rules_firing": [n for n, fn in STRATS.items() if fn(d, i)], "plans": plans(d, i, iv)}
    return out


if __name__ == "__main__":
    import json
    t = time.time()
    r = get_research()
    print("%.0fs" % (time.time() - t))
    for iv, x in r["tf"].items():
        print(iv, x["candles"], "candles", x["days"], "days")
        for q in x["results"]:
            if "test" in q:
                print("  %-24s %-5s sl %.1f tp %.1f | train %s | test %s | %s" % (q["strategy"], q["side"], q["stop_atr"], q["target_r"], q["train"], q["test"], "VALID" if q["valid"] else q["why"]))
            else:
                print("  ", q["strategy"], q["side"], q["why"])
