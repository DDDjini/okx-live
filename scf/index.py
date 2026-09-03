# -*- coding: utf-8 -*-
"""
腾讯云函数 (SCF) 入口文件
- 函数名: main_handler  (SCF Python 标准入口)
- 触发方式: 定时触发器 (类似 cron)
- 环境变量 (在 SCF 控制台配置):
    OKX_LIVE_API_KEY       OKX API Key
    OKX_LIVE_API_SECRET    OKX Secret
    OKX_LIVE_PASSPHRASE    OKX 资金密码 / Passphrase
    FEISHU_LIVE_WEBHOOK    (可选) 飞书机器人 Webhook
"""
import os
import sys
import traceback

# 确保能 import 同目录的 live_trader
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main_handler(event, context):
    """SCF 入口。每次定时触发时调用一次。"""
    print("=" * 50)
    print("[SCF] 触发开始")
    print(f"  request_id = {getattr(context, 'request_id', 'N/A')}")

    required = ["OKX_LIVE_API_KEY", "OKX_LIVE_API_SECRET", "OKX_LIVE_PASSPHRASE"]
    missing = [k for k in required if not os.environ.get(k, "")]

    if missing:
        msg = f"[ERROR] 缺少环境变量: {', '.join(missing)}。请在 SCF 控制台 → 函数配置 → 环境变量 中添加。"
        print(msg)
        return {"status": "error", "message": msg}

    try:
        # 延迟导入，避免 import 阶段就报错
        from live_trader import run_once
        run_once()

        print("[SCF] 执行完成")
        print("=" * 50)
        return {"status": "success", "message": "scan completed"}

    except Exception as e:
        print(f"[SCF] 执行异常: {e}")
        print(traceback.format_exc())
        print("=" * 50)
        return {"status": "error", "message": str(e)}
