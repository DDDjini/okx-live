"""
OKX 实盘自动交易（GitHub Actions 私有库版）
- 每轮：查持仓 → 拉行情 → 检测信号 → 动态仓位 → 下单
- 全部通过 GitHub Secrets 注入 API Key
- 策略参数与模拟盘完全一致
"""

import ccxt
import pandas as pd
import numpy as np
import os
import sys
import time
import requests
import traceback
import hashlib
import math
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════════
# 密钥（优先读环境变量，其次读 .env 文件）
# ═══════════════════════════════════════════════════════════════

# 尝试从 .env 文件加载
env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_file):
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    print(f"📄 已加载 .env 文件 ({env_file})")
else:
    print("⚠️ 未找到 .env 文件，将仅使用系统环境变量")
    print(f"   .env 路径: {env_file}")

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
RR = 1.5
SL_BUFFER = 0.0005
LEVERAGE = 25              # 25x 杠杆（逐仓）
MARGIN_PCT = 0.05          # conservative cap; review before activation
RISK_PCT = 0.005           # planned loss cap on available USDT, before funding/gaps
COST_BUFFER = 0.0015       # assumed fee + slippage buffer; validate before activation
ORDER_PREFIX = "F30V2"
ADD_ENABLED = False        # 关闭加仓（浮盈加仓风险；回测结论：加仓放大亏损、降低盈亏比）
ADD_MARGIN_PCT = 0.03      # 加仓 3%（ADD_ENABLED=False 后不生效）
ADD_FRAC = 0.60            # 浮亏 60% 处加仓（ADD_ENABLED=False 后不生效）
ORDER_TTL_MS = 6 * 30 * 60 * 1000   # 限价挂单存活期：6根30mK线 = 3小时，超时未成交则撤单

# ── 回测定稿新增参数 ──
OFFSET_RANGE = 1           # 分型扫描根数（只扫最新一根）
RISK_FILTER_PCT = 0.6      # 风险过滤：止损距离占价格比例 >= 0.6% 则过滤该信号
H1_ENABLED = False         # 关闭 1h 分型过滤（回测定稿：1h 关闭）
H4_ENABLED = False         # 关闭 4h 分型过滤（多方对比确认无用）
H1_STRICT = False          # 1h 宽松判定（H1_ENABLED=False 后此参数无效）
H4_CONFIRMED = False       # 4h 右肩收盘确认（H4_ENABLED=False 后此参数无效）


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
    low_shifts = [df["low"].shift(k) for k in range(-right, left + 1)]
    high_shifts = [df["high"].shift(k) for k in range(-right, left + 1)]
    lm = pd.concat(low_shifts, axis=1)
    hm = pd.concat(high_shifts, axis=1)
    min_low = lm.min(axis=1)
    max_high = hm.max(axis=1)
    count_low = (lm.values == df["low"].values[:, None]).sum(axis=1)
    count_high = (hm.values == df["high"].values[:, None]).sum(axis=1)
    df["fractal_low"] = lm.notna().all(axis=1) & (df["low"] == min_low) & (count_low == 1)
    df["fractal_high"] = hm.notna().all(axis=1) & (df["high"] == max_high) & (count_high == 1)
    return df


# ═══════════════════════════════════════════════════════════════
# OKX API 封装 — 实盘，不连 Sandbox
# ═══════════════════════════════════════════════════════════════

class OKXTrader:
    def __init__(self):
        # 诊断：检查环境变量
        missing = []
        if not API_KEY: missing.append("OKX_LIVE_API_KEY")
        if not API_SECRET: missing.append("OKX_LIVE_API_SECRET")
        if not PASSPHRASE: missing.append("OKX_LIVE_PASSPHRASE")
        if missing:
            raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")
        if not FEISHU_WEBHOOK:
            print("⚠️ FEISHU_LIVE_WEBHOOK 未设置，不会推送飞书")
        print("OKX credentials loaded")

        cfg = {
            "apiKey": API_KEY, "secret": API_SECRET, "password": PASSPHRASE,
            "enableRateLimit": True, "timeout": 30000,
            "options": {"defaultType": "swap"},
        }
        # 本地需要代理才能连 OKX（有代理就用，没有就不加）
        if os.getenv("OKX_PROXY"):
            cfg["proxies"] = {"http": os.getenv("OKX_PROXY"), "https": os.getenv("OKX_PROXY")}
            print("OKX proxy configured")
        self.exchange = ccxt.okx(cfg)
        # 实盘不连 Sandbox

        for attempt in range(3):
            try:
                self.exchange.load_markets()
                print("✅ load_markets 成功")
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
        bal = self.exchange.fetch_balance()
        val = float(bal.get("USDT", {}).get("free", 0))
        if not math.isfinite(val) or val <= 0:
            raise RuntimeError("Available USDT unknown or zero")
        return val

    def position(self, name):
        sym = ASSETS[name]["symbol"]
        positions = self.exchange.fetch_positions([sym])
        active = []
        for p in positions:
            if p.get("symbol") != sym:
                continue
            qty = abs(float(p.get("contracts", 0)))
            if qty <= 0:
                continue
            mmode = ((p.get("marginMode") or "").lower()
                     or (p.get("info") or {}).get("mgnMode", "").lower())
            if mmode != "isolated":
                raise RuntimeError(f"{name}: non-isolated or unknown position; manual review")
            side = p.get("side") or (p.get("info") or {}).get("posSide")
            if side not in ("long", "short"):
                raise RuntimeError(f"{name}: unknown position side")
            active.append({"side": side, "contracts": qty, "entry": float(p.get("entryPrice", 0))})
        if len(active) > 1:
            raise RuntimeError(f"{name}: multiple positions; manual review")
        return active[0] if active else None

    def set_leverage(self, name, pos_side=None):
        sym = ASSETS[name]["symbol"]
        inst_id = self.exchange.market(sym)["id"]
        params = {"instId": inst_id, "lever": str(LEVERAGE), "mgnMode": "isolated"}
        if pos_side in ("long", "short"):
            params["posSide"] = pos_side
        result = self.exchange.set_leverage(LEVERAGE, sym, params=params)
        data = result.get("data", []) if isinstance(result, dict) else []
        if not data or not all(float(x.get("lever", 0)) == LEVERAGE for x in data):
            raise RuntimeError("Leverage not positively confirmed")
        return result

    def open(self, name, signal, entry_price, sl, tp, equity, signal_time):
        if os.getenv("OKX_LIVE_ENABLE") != "1":
            raise RuntimeError("Live execution disabled")
        if signal not in ("long", "short") or not all(math.isfinite(x) and x > 0 for x in (entry_price, sl, tp, equity)):
            raise ValueError("Invalid order inputs")
        if not (sl < entry_price < tp if signal == "long" else tp < entry_price < sl):
            raise ValueError("Invalid stop/target geometry")
        sym = ASSETS[name]["symbol"]
        ct_val = self.contracts[name]["ct_val"]
        min_qty = self.contracts[name]["min_qty"]
        risk = abs(entry_price - sl) / entry_price
        notional = min(equity * MARGIN_PCT * LEVERAGE, equity * RISK_PCT / (risk + COST_BUFFER))
        qty = notional / (entry_price * ct_val)
        contracts = float(self.exchange.amount_to_precision(sym, qty))
        if not math.isfinite(contracts) or contracts < min_qty or contracts <= 0 or contracts > qty * (1 + 1e-10):
            raise ValueError("Quantity cannot be sized safely")
        inst_id = self.exchange.market(sym)["id"]
        cid = ORDER_PREFIX + hashlib.sha256(f"{name}|{signal}|{signal_time}".encode()).hexdigest()[:24]
        # Query by client ID first; only an explicit order-not-found code permits a new request.
        try:
            existing = self.exchange.private_get_trade_order({"instId": inst_id, "clOrdId": cid})
        except ccxt.OrderNotFound:
            existing = None
        if existing is not None:
            if str(existing.get("code")) != "51603":
                raise RuntimeError("Prior signal order exists or lookup ambiguous; no submission")
        body = {
            "instId": inst_id, "tdMode": "isolated",
            "side": "buy" if signal == "long" else "sell", "posSide": signal,
            "ordType": "limit", "clOrdId": cid,
            "px": self.exchange.price_to_precision(sym, entry_price),
            "sz": str(contracts),
            "attachAlgoOrds": [{
                "tpTriggerPx": self.exchange.price_to_precision(sym, tp), "tpOrdPx": "-1",
                "slTriggerPx": self.exchange.price_to_precision(sym, sl), "slOrdPx": "-1",
                "sz": str(contracts), "posSide": signal,
            }],
        }
        # Never retry an ambiguous submission; reconcile by clOrdId on subsequent scans.
        result = self.exchange.private_post_trade_order(body)
        data = result.get("data", []) if isinstance(result, dict) else []
        if str(result.get("code")) != "0" or len(data) != 1 or str(data[0].get("sCode")) != "0" or not data[0].get("ordId"):
            raise RuntimeError("Order acceptance not confirmed")
        return contracts, contracts * entry_price * ct_val / LEVERAGE

    def fetch_ohlcv(self, name, tf, limit=100):
        sym = ASSETS[name]["symbol"]
        rows = self.exchange.fetch_ohlcv(sym, tf, limit=limit)
        df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        if len(df) > 1:
            df = df.iloc[:-1].reset_index(drop=True)
        return df

    def fetch_price(self, name):
        """获取最新实时价"""
        sym = ASSETS[name]["symbol"]
        try:
            t = self.exchange.fetch_ticker(sym)
            return float(t.get("last", 0) or 0)
        except Exception as e:
            print(f"  [{name}] 获取实时价失败: {e}")
            return 0.0

    def fetch_open_orders(self, name):
        sym = ASSETS[name]["symbol"]
        orders = self.exchange.fetch_open_orders(sym)
        result = []
        for o in orders:
            info = o.get("info") or {}
            if o.get("symbol") != sym and info.get("instId") != self.exchange.market(sym)["id"]:
                raise RuntimeError("Unexpected order symbol")
            result.append({"id": o.get("id"), "cTime": int(info.get("cTime", 0) or 0),
                           "client_id": info.get("clOrdId") or o.get("clientOrderId") or "",
                           "price": o.get("price"), "side": o.get("side")})
        return result

    def cancel_open_orders(self, name):
        raise RuntimeError("Bulk cancel disabled; reconcile owned order IDs manually")

    def get_algo_prices(self, name):
        """读取该币种挂单的止损/止盈价，返回 (sl, tp, algo_sz)"""
        market = self.exchange.market(ASSETS[name]["symbol"])
        inst_id = market["id"]
        sl = tp = 0.0
        algo_sz = 0.0
        for ord_type in ["oco", "conditional", "move_order_stop"]:
            try:
                pending = self.exchange.private_get_trade_orders_algo_pending({
                    "instId": inst_id, "ordType": ord_type,
                })
                data = pending.get("data", []) if isinstance(pending, dict) else []
                for item in data:
                    if (item.get("tdMode") or "").lower() != "isolated":
                        continue  # 只认逐仓算法单
                    sl_v = float(item.get("slTriggerPx", 0) or 0)
                    tp_v = float(item.get("tpTriggerPx", 0) or 0)
                    sz_v = float(item.get("sz", 0) or 0)
                    if sl_v > 0:
                        sl = sl_v
                    if tp_v > 0:
                        tp = tp_v
                    if sz_v > 0:
                        algo_sz = max(algo_sz, sz_v)
            except Exception:
                pass
        return sl, tp, algo_sz

    def cancel_all_algos(self, name):
        """撤销该币种所有止盈止损挂单"""
        market = self.exchange.market(ASSETS[name]["symbol"])
        inst_id = market["id"]
        cancelled = 0
        for ord_type in ["oco", "conditional", "move_order_stop"]:
            try:
                pending = self.exchange.private_get_trade_orders_algo_pending({
                    "instId": inst_id, "ordType": ord_type,
                })
                data = pending.get("data", []) if isinstance(pending, dict) else []
                for item in data:
                    if (item.get("tdMode") or "").lower() != "isolated":
                        continue  # 只撤逐仓算法单
                    algo_id = item.get("algoId")
                    if not algo_id:
                        continue
                    try:
                        self.exchange.private_post_trade_cancel_algo({
                            "instId": inst_id, "algoId": algo_id,
                        })
                        cancelled += 1
                    except Exception as e:
                        print(f"    撤销 algo {algo_id} 失败: {e}")
            except Exception as e:
                print(f"  查询 {ord_type} 挂单失败: {e}")
        return cancelled

    def add_to_position(self, name, signal, add_price, sl, new_tp, original_contracts, equity):
        """加仓 3% + 重挂统一止盈止损（止损不变，止盈按平均成本重算）"""
        sym = ASSETS[name]["symbol"]
        ct_val = self.contracts[name]["ct_val"]
        min_qty = self.contracts[name]["min_qty"]
        pos_side = signal
        order_side = "buy" if signal == "long" else "sell"

        margin = equity * ADD_MARGIN_PCT
        notional = margin * LEVERAGE
        contracts = max(round(notional / (add_price * ct_val), 2), min_qty)

        market = self.exchange.market(sym)
        inst_id = market["id"]

        body = {
            "instId": inst_id, "tdMode": "isolated", "side": order_side,
            "posSide": pos_side, "ordType": "market", "sz": str(contracts),
        }
        print(f"  [{name}] 加仓: {order_side.upper()} {contracts}张 @{add_price:.2f}")
        order = None
        try:
            order = self.exchange.private_post_trade_order(body)
        except Exception as e:
            print(f"  [{name}] 加仓失败(尝试无posSide): {e}")
            body2 = dict(body)
            del body2["posSide"]
            order = self.exchange.private_post_trade_order(body2)

        # 检查下单返回的 sCode（ccxt 不检查 code=0 但 sCode!=0 的情况）
        if isinstance(order, dict):
            data = order.get("data", [])
            if data:
                s_code = str(data[0].get("sCode", "0"))
                if s_code != "0":
                    raise RuntimeError(f"加仓下单被拒绝: sCode={s_code} {data[0].get('sMsg','')}")

        # 验证加仓是否真的成交
        import time
        time.sleep(2)
        pos_after = self.position(name)
        after_contracts = float(pos_after["contracts"]) if pos_after else 0.0
        if after_contracts <= original_contracts:
            raise RuntimeError(f"加仓后仓位未增加: {original_contracts} -> {after_contracts} 张")
        print(f"  [{name}] ✅ 加仓成功，持仓 {original_contracts} -> {after_contracts} 张")

        # 撤销旧单，重挂统一止盈止损
        self.cancel_all_algos(name)
        close_side = "sell" if signal == "long" else "buy"
        algo_body = {
            "instId": inst_id, "tdMode": "isolated", "side": close_side,
            "posSide": pos_side, "ordType": "oco", "sz": str(after_contracts),
            "tpTriggerPx": str(round(new_tp, 2)), "tpOrdPx": "-1",
            "slTriggerPx": str(round(sl, 2)), "slOrdPx": "-1",
        }
        try:
            self.exchange.private_post_trade_order_algo(algo_body)
            print(f"  [{name}] ✅ 重挂统一止盈止损 SL={sl:.2f} TP={new_tp:.2f} ({after_contracts}张)")
        except Exception as e:
            print(f"  [{name}] ⚠️ 重挂止盈止损失败(需手动检查): {e}")

        return contracts, margin, after_contracts


# ═══════════════════════════════════════════════════════════════
# 信号检测
# ═══════════════════════════════════════════════════════════════

def detect_signal(name, m30_df, h1_df, h4_df, cfg):
    n = len(m30_df)
    if n < LEFT + RIGHT + 2:
        return None

    m30 = add_fractals(m30_df.copy(), LEFT, RIGHT)
    h1 = add_fractals(h1_df.copy(), 2, 2)
    h4 = add_fractals(h4_df.copy(), 2, 2)

    # 1h 分型事件（宽松：存在同向分型即可；严格：最近分型方向必须匹配）
    if H1_ENABLED:
        if H1_STRICT:
            h1_mask = h1['fractal_low'].values | h1['fractal_high'].values
            h1_ts = h1['timestamp'].values[h1_mask]
            h1_typ = np.where(h1['fractal_low'].values[h1_mask], 'low', 'high')
            if len(h1_ts) > 1:
                order = np.argsort(h1_ts)
                h1_ts = h1_ts[order]
                h1_typ = h1_typ[order]

    # 4h 分型事件（严格共振：最近一个"已确认"分型方向必须匹配）
    if H4_ENABLED:
        h4_mask = h4['fractal_low'].values | h4['fractal_high'].values
        if H4_CONFIRMED:
            # 仅保留右肩收盘确认的分型（右 2 根必须已收盘，末尾 right=2 根分型视为未成型）
            n4 = len(h4_df)
            confirmed_ok = np.arange(n4) <= (n4 - 1 - 2)
            h4_mask = h4_mask & confirmed_ok
        h4_ts = h4['timestamp'].values[h4_mask]
        h4_typ = np.where(h4['fractal_low'].values[h4_mask], 'low', 'high')
        if len(h4_ts) > 1:
            order = np.argsort(h4_ts)
            h4_ts = h4_ts[order]
            h4_typ = h4_typ[order]

    for offset in range(0, OFFSET_RANGE):
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

        if H1_ENABLED:
            sub = h1[h1["timestamp"] <= ts]
            if len(sub) < 5:
                continue
            if H1_STRICT:
                idx1 = int(np.searchsorted(h1_ts, ts, side='right') - 1)
                if idx1 < 0:
                    continue
                if dir_ == "long" and h1_typ[idx1] != "low":
                    continue
                if dir_ == "short" and h1_typ[idx1] != "high":
                    continue
            else:
                if dir_ == "long" and not sub["fractal_low"].any():
                    continue
                if dir_ == "short" and not sub["fractal_high"].any():
                    continue

        # 4h 严格共振（最近 4h 已确认分型方向必须匹配）
        if H4_ENABLED:
            idx4 = int(np.searchsorted(h4_ts, ts, side='right') - 1)
            if idx4 < 0:
                continue
            if dir_ == "long" and h4_typ[idx4] != "low":
                continue
            if dir_ == "short" and h4_typ[idx4] != "high":
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

        # 风险过滤：止损距离占价格比例 >= RISK_FILTER_PCT% 则跳过该信号
        risk_pct = abs(entry - sl) / entry * 100
        if risk_pct >= RISK_FILTER_PCT:
            continue

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
    if os.getenv("OKX_LIVE_ENABLE") != "1":
        print("Live execution disabled; no exchange client created")
        return
    ts = datetime.now(timezone.utc).strftime('%m-%d %H:%M')
    print(f"[{ts}] 🚀 实盘交易扫描...")

    try:
        return _run_inner(ts)
    except Exception as e:
        err = traceback.format_exc()
        print(f"[严重错误]\n{err}")
        feishu("Execution blocked", "Scan failed; inspect local logs before resuming.", color="red")
        raise


def _run_inner(ts):
    trader = OKXTrader()
    equity = trader.balance()
    # Snapshot both symbols first. A failed private query aborts the entire round.
    positions = {name: trader.position(name) for name in ASSETS}
    orders = {name: trader.fetch_open_orders(name) for name in ASSETS}
    # Existing exposure needs manual review: do not add or alter protection orders.
    # Protection reconciliation and a durable order journal are mandatory before activation.
    if any(positions.values()) or any(orders.values()):
        feishu("Existing exposure: manual review", "New entries blocked while positions or orders exist.", "red")
        return
    for name, cfg in ASSETS.items():
        m30 = trader.fetch_ohlcv(name, "30m", 100)
        h1 = trader.fetch_ohlcv(name, "1h", 50)
        h4 = trader.fetch_ohlcv(name, "4h", 100)
        if m30.empty or h1.empty or h4.empty:
            raise RuntimeError("Empty candles")
        # Refuse stale, duplicate or unordered 30m bars.
        stamps = m30["timestamp"]
        if not stamps.is_monotonic_increasing or stamps.duplicated().any():
            raise RuntimeError("Invalid candle sequence")
        age = int(time.time() * 1000) - int(stamps.iloc[-1]) - 30 * 60 * 1000
        if age < 0 or age >= 30 * 60 * 1000:
            raise RuntimeError("Latest closed candle is missing or stale")
        sig = detect_signal(name, m30, h1, h4, cfg)
        if not sig:
            continue
        # Refresh before submitting; any unexpected exposure blocks the entry.
        if any(trader.position(n) for n in ASSETS) or any(trader.fetch_open_orders(n) for n in ASSETS):
            raise RuntimeError("Exposure changed; abort")
        trader.set_leverage(name, sig["signal"])
        contracts, margin = trader.open(name, sig["signal"], sig["entry"], sig["sl"], sig["tp"], equity, sig["time"])
        feishu("Order accepted", f"{name} {sig['signal']} {contracts} contracts; verify fill and protection.", "blue")
        return  # one entry maximum per round
    print("No eligible signal")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    if args.once:
        run_once()
    else:
        print("用法: python live_trader.py --once")

