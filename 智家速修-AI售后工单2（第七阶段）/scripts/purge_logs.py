# -*- coding: utf-8 -*-
"""对话与工具日志保留策略执行脚本（阶段 7 上线加固新增）。

用途（上线检查清单"数据安全"维度的落地工具）：
报销对话记录属业务数据，长期堆积既占空间又有合规风险。
本脚本按 Config.LOG_RETENTION_DAYS（默认 90 天）清理：

- tool_calls 工具调用日志：超过保留期全删（分析价值随时间衰减最快）；
- messages 对话记录：**只删除未标记（flagged=0）**的过期记录——
  被管理员标记为疑似违规的问答属于审计证据，永久保留，需人工处理后才可删。

用法：
    python scripts/purge_logs.py             # 预演：只打印将要删除的行数，不动数据
    python scripts/purge_logs.py --apply     # 真正执行删除

生产建议：crontab / 任务计划程序每日执行 `--apply`。
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app import db               # noqa: E402
from app.config import Config    # noqa: E402


def cutoff() -> str:
    """保留期截止时间（早于该时间戳的记录过期）。"""
    return (datetime.now() - timedelta(days=Config.LOG_RETENTION_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S")


def preview() -> dict:
    """统计将被清理的行数（预演用，不写库）。"""
    ts = cutoff()
    stale_calls = db.query_one(
        "SELECT COUNT(*) AS n FROM tool_calls WHERE created_at < ?", (ts,))
    stale_msgs = db.query_one(
        "SELECT COUNT(*) AS n FROM messages WHERE created_at < ? AND flagged = 0", (ts,))
    kept_flagged = db.query_one(
        "SELECT COUNT(*) AS n FROM messages WHERE created_at < ? AND flagged = 1", (ts,))
    return {
        "cutoff": ts,
        "tool_calls": stale_calls["n"],
        "messages": stale_msgs["n"],
        "messages_保留_被标记": kept_flagged["n"],
    }


def purge() -> None:
    """执行清理。注意 messages 只删未标记的，标记记录留给管理员处理。"""
    ts = cutoff()
    db.execute("DELETE FROM tool_calls WHERE created_at < ?", (ts,))
    db.execute("DELETE FROM messages WHERE created_at < ? AND flagged = 0", (ts,))
    db.execute("VACUUM")   # 回收 SQLite 文件空间
    print(f"[清理] 截止 {ts} 之前的工具日志与未标记对话已删除，VACUUM 完成。")


if __name__ == "__main__":
    db.init_db()   # 幂等：库不存在则建表（保证脚本在任何一台新机器上都能直接跑）
    parser = argparse.ArgumentParser(description="日志保留策略执行工具")
    parser.add_argument("--apply", action="store_true",
                        help="真正执行删除（缺省只做预演统计）")
    args = parser.parse_args()

    stat = preview()
    print(f"保留策略：{Config.LOG_RETENTION_DAYS} 天")
    print(f"截止时间：{stat['cutoff']}")
    print(f"将清理工具日志 {stat['tool_calls']} 条、未标记对话 {stat['messages']} 条"
          f"（另有 {stat['messages_保留_被标记']} 条被标记记录永久保留）")
    if args.apply:
        purge()
    else:
        print("（预演模式：如确认无误请加 --apply）")
