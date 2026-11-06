# -*- coding: utf-8 -*-
"""SQLite 数据库备份脚本（阶段 7 上线加固新增）。

用途（上线检查清单"回滚方案"维度的落地工具）：
- 备份：用 SQLite 官方 backup API 做在线一致性备份（不怕 SSE 长连接正在写入），
  输出带时间戳的快照到 backups/ 目录，自动清理超出保留份数的旧备份。
- 恢复：停服务 → 用某个快照覆盖 data/chat.db → 重启服务（见 --restore 说明）。

用法：
    python scripts/backup_db.py              # 立即备份一次
    python scripts/backup_db.py --keep 14    # 备份并保留最近 14 份
    python scripts/backup_db.py --list       # 查看现有备份

生产建议：Windows 任务计划程序 / Linux crontab 每日执行一次。
"""

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# 兼容脚本直跑与模块导入两种方式：把项目根目录塞进 sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.config import Config   # noqa: E402  （依赖上面的 sys.path 注入）


def backup(keep: int) -> Path:
    """执行一次在线备份，返回快照文件路径。"""
    backup_dir = Path(Config.BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"chat_{stamp}.db"

    # SQLite backup API：在加锁的瞬间拷贝一致的页面快照，
    # 比直接 cp 数据库文件安全（后者可能拷到 WAL 半写状态）
    src = sqlite3.connect(Config.DB_PATH)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    print(f"[备份] 已生成快照：{target}")

    # 清理超出保留份数的旧备份（按文件名时间戳排序，删最旧的）
    snapshots = sorted(backup_dir.glob("chat_*.db"))
    for old in snapshots[:-keep] if keep > 0 else snapshots:
        old.unlink()
        print(f"[清理] 删除过期备份：{old.name}")
    return target


def list_backups() -> None:
    """列出全部现有备份。"""
    backup_dir = Path(Config.BACKUP_DIR)
    if not backup_dir.exists():
        print("尚无备份目录。")
        return
    for f in sorted(backup_dir.glob("chat_*.db")):
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"{f.name}  {size_mb:.2f} MB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="报销助手 SQLite 备份工具")
    parser.add_argument("--keep", type=int, default=Config.BACKUP_KEEP,
                        help=f"保留最近几份备份（默认 {Config.BACKUP_KEEP}）")
    parser.add_argument("--list", action="store_true", help="仅列出现有备份")
    args = parser.parse_args()

    if args.list:
        list_backups()
    else:
        backup(args.keep)
