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
                os.environ[k.strip()] = v.strip().strip('"').strip("'")
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
LEVERAGE = 100
MARGIN_PCT = 0.05          # 头仓 5%
ADD_MARGIN_PCT = 0.03      # 加仓 3%
ADD_FRAC = 0.60            # 浮亏 60% 处加仓（入场到止损走完 3/5）
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
        # 诊断：检查环境变量
        missing = []
        if not API_KEY: missing.append("OKX_LIVE_API_KEY")
        if not API_SECRET: missing.append("OKX_LIVE_API_SECRET")
        if not PASSPHRASE: missing.append("OKX_LIVE_PASSPHRASE")
        if missing:
            raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")
        if not FEISHU_WEBHOOK:
            print("⚠️ FEISHU_LIVE_WEBHOOK 未设置，不会推送飞书")
        print(f"🔑 密钥已加载: API_KEY={API_KEY[:8]}***")

        cfg = {
            "apiKey": API_KEY, "secret": API_SECRET, "password": PASSPHRASE,
            "enableRateLimit": True, "timeout": 30000,
            "options": {"defaultType": "swap"},
        }
        # 本地需要代理才能连 OKX（有代理就用，没有就不加）
        if os.getenv("OKX_PROXY"):
            cfg["proxies"] = {"http": os.getenv("OKX_PROXY"), "https": os.getenv("OKX_PROXY")}
            print("🌐 使用代理:", os.getenv("OKX_PROXY"))
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

        # 限价单价格：严格用信号入场价（避免市价滑点）
        px = self.exchange.price_to_precision(sym, entry_price)

        body = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": order_side,
            "posSide": pos_side,
            "ordType": "limit",
            "px": px,
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
        print(f"  [{name}] 挂限价单: {order_side} {contracts}张 @{px} posSide={pos_side}")
        try:
            order = self.exchange.private_post_trade_order(body)
            print(f"  [{name}] ✅ 限价挂单成功 {order}")
            return contracts, margin
        except Exception as e:
            print(f"  [{name}] 第1次下单失败: {e}")
            body2 = dict(body)
            del body2["posSide"]
            for ao in body2["attachAlgoOrds"]:
                ao.pop("posSide", None)
            print(f"  [{name}] 重试(无posSide): {order_side} {contracts}张 @{px}")
            try:
                order = self.exchange.private_post_trade_order(body2)
                print(f"  [{name}] ✅ 限价挂单成功 {order}")
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
        """查询该币种未成交的普通限价挂单，返回 [{id, cTime(ms), price, side}]"""
        market = self.exchange.market(ASSETS[name]["symbol"])
        inst_id = market["id"]
        result = []
        try:
            orders = self.exchange.fetch_open_orders(ASSETS[name]["symbol"])
            for o in orders:
                info = o.get("info") or {}
                if info.get("instId") != inst_id:
                    continue
                result.append({
                    "id": o.get("id"),
                    "cTime": int(info.get("cTime", 0) or 0),  # 创建时间(毫秒)
                    "price": o.get("price"),
                    "side": o.get("side"),
                })
        except Exception as e:
            print(f"  查询挂单失败 [{name}]: {e}")
        return result

    def cancel_open_orders(self, name):
        """撤销该币种所有未成交的普通限价挂单"""
        sym = ASSETS[name]["symbol"]
        market = self.exchange.market(sym)
        inst_id = market["id"]
        cancelled = 0
        try:
            orders = self.exchange.fetch_open_orders(sym)
            for o in orders:
                if (o.get("info") or {}).get("instId") != inst_id:
                    continue
                oid = o.get("id")
                if not oid:
                    continue
                try:
                    self.exchange.cancel_order(oid, sym)
                    cancelled += 1
                except Exception as e:
                    print(f"    撤单失败 {oid}: {e}")
        except Exception as e:
            print(f"  撤销挂单失败 [{name}]: {e}")
        return cancelled

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
            "instId": inst_id, "tdMode": "cross", "side": order_side,
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
            "instId": inst_id, "tdMode": "cross", "side": close_side,
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
            # ── 持仓中：判断是否需要加仓（浮亏60%处）──
            side = pos["side"]
            entry = pos["entry"]
            contracts = pos["contracts"]
            sl, tp, algo_sz = trader.get_algo_prices(name)

            if sl <= 0:
                print(f"  [{name}] 持仓中但无止损单，跳过加仓")
                continue

            # 头仓应有张数（5%保证金×100x）
            ct_val = trader.contracts[name]["ct_val"]
            expected_head = equity * MARGIN_PCT * LEVERAGE / (entry * ct_val) if entry > 0 else 0

            # 判断是否已加仓：挂单张数 > 1.25倍头仓（加仓3%后总张数≈1.6倍头仓）
            ref_sz = algo_sz if algo_sz > 0 else contracts
            if ref_sz > expected_head * 1.25:
                print(f"  [{name}] 已加仓({contracts}张)，跳过")
                continue

            # 未加仓，计算加仓触发价（浮亏60%）
            if side == "long":
                add_price = entry - ADD_FRAC * (entry - sl)
            else:
                add_price = entry + ADD_FRAC * (sl - entry)

            price = trader.fetch_price(name)
            trigger = (side == "long" and price > 0 and price <= add_price) or \
                      (side == "short" and price > 0 and price >= add_price)

            if not trigger:
                print(f"  [{name}] 持仓中，价格{price:.2f}未到加仓区(触发价{add_price:.2f})")
                continue

            # 触发加仓
            print(f"  🔔 [{name}] 触及加仓区{add_price:.2f}，执行加仓!")
            avg_entry = (entry + add_price) / 2
            if side == "long":
                new_risk = avg_entry - sl
                new_tp = avg_entry + RR * new_risk
            else:
                new_risk = sl - avg_entry
                new_tp = avg_entry - RR * new_risk

            try:
                add_contracts, add_margin, total = trader.add_to_position(
                    name, side, add_price, sl, new_tp, contracts, equity)
                signals_found.append(
                    f"  📈 {side.upper()}加仓 @{add_price:.2f} | +{add_contracts}张 | 总{total}张"
                )
            except Exception as e:
                feishu(f"⚠️ [{name}] 加仓失败", f"**错误**: `{str(e)[:300]}`", color="red")
            continue

        # 无持仓：先查挂单，判断是否超时（6根K线未成交才撤）
        open_orders = trader.fetch_open_orders(name)
        now_ms = int(time.time() * 1000)
        if open_orders:
            expired = [o for o in open_orders if now_ms - o["cTime"] > ORDER_TTL_MS]
            if expired:
                n_cancel = trader.cancel_open_orders(name)
                print(f"  [{name}] 挂单超时(6根K线未成交)，撤销 {n_cancel} 个")
            else:
                age_min = (now_ms - open_orders[0]["cTime"]) / 60000
                print(f"  [{name}] 已有挂单未成交(已挂{age_min:.0f}分钟)，保留等待成交")
                continue

        # 无挂单（或已撤销超时单）：拉数据 + 扫信号
        try:
            m30 = trader.fetch_ohlcv(name, "30m", 100)
            h1 = trader.fetch_ohlcv(name, "1h", 50)
            h4 = trader.fetch_ohlcv(name, "4h", 100)
        except Exception as e:
            print(f"  [{name}] 数据拉取失败: {e}")
            continue

        price = m30["close"].iloc[-1]
        print(f"  [{name}] 价格: {price:.2f}")

        sig = detect_signal(name, m30, h1, h4, cfg)
        if not sig:
            print(f"  [{name}] 无信号")
            continue

        print(f"  🔔 [{name}] {sig['signal'].upper()} 限价挂单 @{sig['entry']}")
        trader.set_leverage(name)
        try:
            contracts, margin = trader.open(name, sig["signal"], sig["entry"],
                                             sig["sl"], sig["tp"], equity)
            signals_found.append(
                f"  {sig['signal'].upper()} 限价挂单@{sig['entry']} | {contracts}张 | 保证金{margin:.2f}"
            )
        except Exception as e:
            feishu(f"⚠️ [{name}] 挂单失败", f"**信号**: {sig['signal'].upper()} @{sig['entry']}\n**错误**: `{str(e)[:300]}`", color="red")

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
