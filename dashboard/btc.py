"""Live BTC multi-timeframe analysis for the dashboard. Read-only, Hyperliquid public API."""
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader-hl"))
import hl  # noqa: E402
import signals as S  # noqa: E402

TFS = ["5m", "15m", "1h", "4h", "1d"]


def ema(vals, n):
    k, e, out = 2 / (n + 1), vals[0], []
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def tf_view(iv):
    cs = hl.candles("BTC", iv, 300 if iv != "1d" else 200)
    c = [x[3] for x in cs]
    e20, e50 = ema(c, 20), ema(c, 50)
    i = len(cs) - 1
    a = S.atr(cs, i)
    vavg = sum(x[4] for x in cs[-21:-1]) / 20
    win = cs[-21:-1]
    score = (c[-1] > e20[-1]) + (c[-1] > e50[-1]) + (e20[-1] > e50[-1])
    trend = "up" if score == 3 else "down" if score == 0 else "mixed"
    sig = S.signal_at(cs, i)
    return {"interval": iv, "close": c[-1], "ema20": e20[-1], "ema50": e50[-1], "trend": trend,
            "rsi": hl.rsi(c), "atr": a, "atr_pct": a / c[-1], "vol_x": cs[-1][4] / vavg if vavg else None,
            "high20": max(x[1] for x in win), "low20": min(x[2] for x in win), "signal": sig,
            "hist_long": S.history(cs, "long"), "hist_short": S.history(cs, "short"),
            "chg_20": c[-1] / c[-21] - 1, "cs": cs, "e20": e20}


def analyse():
    meta, ctxs = hl.post({"type": "metaAndAssetCtxs"})
    idx = next(k for k, m in enumerate(meta["universe"]) if m["name"] == "BTC")
    x, m = ctxs[idx], meta["universe"][idx]
    px, prev = float(x["markPx"]), float(x["prevDayPx"])
    views = [tf_view(iv) for iv in TFS]
    chart = views[1]["cs"][-96:]
    ce20 = views[1]["e20"][-96:]
    for v in views:
        del v["cs"], v["e20"]
    ups = sum(v["trend"] == "up" for v in views)
    downs = sum(v["trend"] == "down" for v in views)
    bias = "bullish" if ups >= 4 else "bearish" if downs >= 4 else "leaning up" if ups > downs else "leaning down" if downs > ups else "mixed / range"
    args = types.SimpleNamespace(size=100, max_loss=30, leverage=1)
    plans = {}
    v15 = views[1]
    cs15 = hl.candles("BTC", "15m", 60)
    for side in ("long", "short"):
        p = S.plan(None, cs15, side, v15["atr"], args, m["maxLeverage"])
        plans[side] = p
    fund = float(x["funding"])
    return {"as_of": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "price": px, "chg_24h": px / prev - 1,
            "volume_24h": float(x["dayNtlVlm"]), "open_interest_usd": float(x["openInterest"]) * px,
            "funding_hr": fund, "funding_apr": fund * 24 * 365, "premium": float(x["premium"]),
            "max_leverage": m["maxLeverage"], "bias": bias, "ups": ups, "downs": downs, "views": views,
            "chart": [list(c) for c in chart], "chart_ema20": ce20, "plans": plans}
