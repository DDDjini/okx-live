"""
本地实盘持续运行 — 每10分钟扫描一次
用法: python live_local.py
Ctrl+C 停止
"""
import time
from live_trader import run_once

print("=" * 50)
print("🚀 本地实盘交易 — 每10分钟扫描")
print("按 Ctrl+C 停止")
print("=" * 50)

INTERVAL = 600  # 10分钟 = 600秒

while True:
    try:
        run_once()
    except Exception as e:
        print(f"[主循环异常] {e}")
    print(f"
⏳ 等待 {INTERVAL//60} 分钟后下一轮...
")
    time.sleep(INTERVAL)
