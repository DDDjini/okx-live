"""
OKX 实盘自动交易（GitHub Actions 私有库版）
- 每轮：查持仓 → 拉行情 → 检测信号 → 动态仓位 → 下单
- 全部通过 GitHub Secrets 注入 API Key
- 策略参数与模拟盘完全一致
"""

import ccxt
import pandas as pd
import os
import sys
import requests
import traceback
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════════
# 密钥（从环境变量读取 — 实盘专用）
# ═══════════════════════════════════════════════════════════════

API_KEY = os.getenv("OKX_LIVE_API_KEY", "")
API_SECRET = os.getenv("OKX_LIVE_API_SECRET", "")
PASSPHRASE = os.getenv("OKX_LIVE_PASSPHRASE", "")
FEISHU_WEBHOOK = os.getenv("FEISHU_LIVE_WEBHOOK", "")

# ═══════════════════════════════════════════════════════════════
# 策略参数（与模拟盘完全一致）
# ═══════════════════════════════════════════════════════════════

ASSETS = {
    "BTC": {"symbol": "BTC/USDT:USDT", "max_stop_pct": 0.017},
    "ETH": {"symbol": "ETH/USDT:USDT", "max_stop_pts": 50.0},
}

LEFT, RIGHT = 5, 2
RR = 1.0
SL_BUFFER = 0.0005
LEVERAGE = 100
MARGIN_PCT = 0.05          # 5%


# ═══════════════════════════════════════════════════════════════
# 飞书
# ═══════════════════════════════════════════════════════════════

def feishu(title: str, content: str, color: str = "blue"):
    if not FEISHU_WEBHOOK:
        print(f"[飞书] 跳过: {title}")
        return
    try:
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
                "elements": [
                    {"tag": "markdown", "content": content},
                    {"tag": "note", "elements": [
                        {"tag": "plain_text",
                         "content": f"🕐 {datetime.now().strftime('%m-%d %H:%M')} UTC"}
                    ]},
                ],
            },
        }
        r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        print(f"  [飞书] {r.status_code} | {title}")
    except Exception as e:
        print(f"  [飞书] 异常: {e}")


# ═══════════════════════════════════════════════════════════════
# 分型
# ═══════════════════════════════════════════════════════════════

def add_fractals(df, left, right):
    df = df.copy()
    low_shifts = [df["low"].shift(k) for k in range(-left, right + 1)]
    high_shifts = [df["high"].shift(k) for k in range(-left, right + 1)]
    lm = pd.concat(low_shifts, axis=1)
    hm = pd.concat(high_shifts, axis=1)
    min_low = lm.min(axis=1)
    max_high = hm.max(axis=1)
    count_low = (lm.values == df["low"].values[:, None]).sum(axis=1)
    count_high = (hm.values == df["high"].values[:, None]).sum(axis=1)
    df["fractal_low"] = (df["low"] == min_low) & (count_low == 1)
    df["fractal_high"] = (df["high"] == max_high) & (count_high == 1)
    return df


# ═══════════════════════════════════════════════════════════════
# OKX API 封装 — 实盘，不连 Sandbox
# ═══════════════════════════════════════════════════════════════

class OKXTrader:
    def __init__(self):
        if not all([API_KEY, API_SECRET, PASSPHRASE]):
            raise RuntimeError("OKX API Key 未配置")

        cfg = {
            "apiKey": API_KEY, "secret": API_SECRET, "password": PASSPHRASE,
            "enableRateLimit": True, "timeout": 30000,
            "options": {"defaultType": "swap"},
        }
        self.exchange = ccxt.okx(cfg)
        # 注意：实盘不加 set_sandbox_mode

        for attempt in range(3):
            try:
                self.exchange.load_markets()
                break
            except Exception as e:
                print(f"  load_markets 第{attempt+1}次失败: {e}")
                if attempt == 2:
                    raise
                import time; time.sleep(3)

        self.contracts = {}
        for name, cfg_asset in ASSETS.items():
            sym = cfg_asset["symbol"]
            mkt = self.exchange.market(sym)
            self.contracts[name] = {
                "ct_val": float(mkt.get("contractSize", 1)),
                "min_qty": float(mkt.get("limits", {}).get("amount", {}).get("min", 0.01)),
            }

    def balance(self):
        for attempt in range(2):
            try:
                bal = self.exchange.fetch_balance()
                usdt = bal.get("USDT", {})
                val = float(usdt.get("free", 0)) or float(usdt.get("total", 0))
                if val > 0:
                    return val
            except Exception as e:
                print(f"  余额查询第{attempt+1}次失败: {e}")
                if attempt < 1:
                    import time; time.sleep(2)
        return 0

    def position(self, name):
        sym = ASSETS[name]["symbol"]
        try:
            positions = self.exchange.fetch_positions([sym])
            for p in positions:
                if p.get("symbol") == sym and abs(float(p.get("contracts", 0))) > 0:
                    side = p.get("posSide", p.get("side", "long"))
                    return {
                        "side": side, "contracts": float(p["contracts"]),
                        "entry": float(p.get("entryPrice", 0)),
                    }
            return None
        except Exception as e:
            print(f"  持仓查询失败 [{name}]: {e}")
            return None

    def set_leverage(self, name):
        sym = ASSETS[name]["symbol"]
        market_data = self.exchange.market(sym)
        inst_id = market_data["id"]
        try:
            self.exchange.set_leverage(LEVERAGE, sym,
                                       params={"instId": inst_id, "lever": str(LEVERAGE), "mgnMode": "cross"})
            print(f"  [{name}] 杠杆: {LEVERAGE}x")
        except Exception as e:
            print(f"  [{name}] 杠杆设置失败: {e}")

    def open(self, name: str, signal: str, entry_price: float,
             sl: float, tp: float, equity: float):
        sym = ASSETS[name]["symbol"]
        ct_val = self.contracts[name]["ct_val"]
        min_qty = self.contracts[name]["min_qty"]
        pos_side = signal
        order_side = "buy" if signal == "long" else "sell"

        margin = equity * MARGIN_PCT
        notional = margin * LEVERAGE
        contracts = max(round(notional / (entry_price * ct_val), 2), min_qty)

        market = self.exchange.market(sym)
        inst_id = market["id"]

        body = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": order_side,
            "posSide": pos_side,
            "ordType": "market",
            "sz": str(contracts),
            "attachAlgoOrds": [{
                "tpTriggerPx": str(tp),
                "tpOrdPx": "-1",
                "slTriggerPx": str(sl),
                "slOrdPx": "-1",
                "sz": str(contracts),
                "posSide": pos_side,
            }],
        }
        print(f"  [{name}] 实盘下单: {order_side} {contracts}张 posSide={pos_side}")
        try:
            order = self.exchange.private_post_trade_order(body)
            print(f"  [{name}] ✅ 开仓成功 {order}")
            return contracts, margin
        except Exception as e:
            print(f"  [{name}] 第1次下单失败: {e}")
            body2 = dict(body)
            del body2["posSide"]
            for ao in body2["attachAlgoOrds"]:
                ao.pop("posSide", None)
            print(f"  [{name}] 重试(无posSide): {order_side} {contracts}张")
            try:
                order = self.exchange.private_post_trade_order(body2)
                print(f"  [{name}] ✅ 开仓成功 {order}")
                return contracts, margin
            except Exception as e2:
                print(f"  [{name}] ❌ 重试也失败: {e2}")
                raise e2

    def fetch_ohlcv(self, name, tf, limit=100):
        sym = ASSETS[name]["symbol"]
        rows = self.exchange.fetch_ohlcv(sym, tf, limit=limit)
        df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        if len(df) > 1:
            df = df.iloc[:-1].reset_index(drop=True)
        return df


# ═══════════════════════════════════════════════════════════════
# 信号检测
# ═══════════════════════════════════════════════════════════════

def detect_signal(name, m30_df, h1_df, cfg):
    n = len(m30_df)
    if n < LEFT + RIGHT + 2:
        return None

    m30 = add_fractals(m30_df.copy(), LEFT, RIGHT)
    h1 = add_fractals(h1_df.copy(), 2, 2)

    for offset in range(0, 3):
        i = n - 1 - offset
        pivot = i - RIGHT
        if pivot < 0:
            continue

        dir_ = None
        if m30.loc[pivot, "fractal_low"]:
            dir_ = "long"
        elif m30.loc[pivot, "fractal_high"]:
            dir_ = "short"
        if dir_ is None:
            continue

        ts = m30.loc[pivot, "timestamp"]
        sub = h1[h1["timestamp"] <= ts]
        if len(sub) < 5:
            continue
        if dir_ == "long" and not sub["fractal_low"].any():
            continue
        if dir_ == "short" and not sub["fractal_high"].any():
            continue

        entry = m30.loc[i, "close"]
        if dir_ == "long":
            sl = m30.loc[pivot, "low"] * (1 - SL_BUFFER)
            risk = entry - sl
            if risk <= 0: continue
            max_stop = cfg.get("max_stop_pct")
            if max_stop and risk > entry * max_stop:
                risk = entry * max_stop; sl = entry - risk
            max_pts = cfg.get("max_stop_pts")
            if max_pts and risk > max_pts:
                risk = max_pts; sl = entry - risk
            tp = entry + RR * risk
        else:
            sl = m30.loc[pivot, "high"] * (1 + SL_BUFFER)
            risk = sl - entry
            if risk <= 0: continue
            max_stop = cfg.get("max_stop_pct")
            if max_stop and risk > entry * max_stop:
                risk = entry * max_stop; sl = entry + risk
            max_pts = cfg.get("max_stop_pts")
            if max_pts and risk > max_pts:
                risk = max_pts; sl = entry + risk
            tp = entry - RR * risk

        return {
            "asset": name, "signal": dir_,
            "entry": round(entry, 2), "sl": round(sl, 2), "tp": round(tp, 2),
            "time": str(m30.loc[i, "datetime"]),
        }

    return None


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def run_once():
    ts = datetime.now(timezone.utc).strftime('%m-%d %H:%M')
    print(f"[{ts}] 🚀 实盘交易扫描...")

    try:
        return _run_inner(ts)
    except Exception as e:
        err = traceback.format_exc()
        print(f"[严重错误]\n{err}")
        feishu(f"❌ 脚本崩溃 {ts}", f"```{err[:800]}```", color="red")


def _run_inner(ts):
    try:
        trader = OKXTrader()
    except Exception as e:
        feishu("❌ OKX 初始化失败", f"```{traceback.format_exc()[:400]}```", "red")
        return

    equity = trader.balance()
    print(f"账户余额: {equity:.2f} USDT")

    btc_pos = trader.position("BTC")
    eth_pos = trader.position("ETH")
    btc_line = f"持仓 {btc_pos['side']} {btc_pos['contracts']}张 @{btc_pos['entry']}" if btc_pos else "无持仓"
    eth_line = f"持仓 {eth_pos['side']} {eth_pos['contracts']}张 @{eth_pos['entry']}" if eth_pos else "无持仓"

    signals_found = []

    for name, cfg in ASSETS.items():
        pos = trader.position(name)
        if pos:
            print(f"  [{name}] 持仓中，跳过")
            continue

        try:
            m30 = trader.fetch_ohlcv(name, "30m", 100)
            h1 = trader.fetch_ohlcv(name, "1h", 50)
        except Exception as e:
            print(f"  [{name}] 数据拉取失败: {e}")
            continue

        price = m30["close"].iloc[-1]
        print(f"  [{name}] 价格: {price:.2f}")

        sig = detect_signal(name, m30, h1, cfg)
        if not sig:
            print(f"  [{name}] 无信号")
            continue

        print(f"  🔔 [{name}] {sig['signal'].upper()} @ {sig['entry']}")
        trader.set_leverage(name)
        try:
            contracts, margin = trader.open(name, sig["signal"], sig["entry"],
                                             sig["sl"], sig["tp"], equity)
            signals_found.append(
                f"  {sig['signal'].upper()} @{sig['entry']} | {contracts}张 | 保证金{margin:.2f}"
            )
        except Exception as e:
            feishu(f"⚠️ [{name}] 下单失败", f"**信号**: {sig['signal'].upper()} @{sig['entry']}\n**错误**: `{str(e)[:300]}`", color="red")

    sig_text = "\n".join(signals_found) if signals_found else "本轮无信号"
    action = "🎯 已下单！" if signals_found else "👀 等待信号"

    feishu(
        f"{action} {ts}",
        f"**✅ 已连接 OKX 实盘**\n"
        f"**账户余额**: {equity:.2f} USDT\n\n"
        f"**BTC**: {btc_line} | **ETH**: {eth_line}\n\n"
        f"**本轮信号**:\n{sig_text}",
        color="red" if signals_found else "blue",
    )

    print(f"\n{'='*50}")
    print(f"余额={equity:.2f} BTC={btc_line} ETH={eth_line} 信号={len(signals_found)}")
    print("[Done]")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    if args.once:
        run_once()
    else:
        print("用法: python live_trader.py --once")
