"""
模拟「每天有新的评论文件出现在桌面」：按日期顺序，每隔一段时间把 data/daily 里的
下一个文件复制到监控目录（默认 桌面/评论数据）。已经复制过的文件会跳过，中断后可以接着运行。

用法：
  python scripts/simulate_feed.py --since 20261021              # 从 10-21 起每小时放一个
  python scripts/simulate_feed.py --since 20261021 --interval 60 # 每 60 秒放一个（演示用）
  python scripts/simulate_feed.py --since 20261021 --preload 2   # 先一次性放入 2 个
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import DAILY_DIR, WATCH_DIR, WATCH_PATTERN  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="模拟评论文件按时投放到监控目录")
    p.add_argument("--src", default=DAILY_DIR, type=Path)
    p.add_argument("--dst", default=WATCH_DIR, type=Path)
    p.add_argument("--since", help="只投放该日期（YYYYMMDD）及之后的文件")
    p.add_argument("--interval", type=int, default=3600, help="投放间隔（秒）")
    p.add_argument("--preload", type=int, default=0, help="启动时先一次性投放的文件数")
    return p.parse_args()


def main():
    args = parse_args()
    args.dst.mkdir(parents=True, exist_ok=True)

    files = sorted(args.src.glob(WATCH_PATTERN))
    if args.since:
        files = [f for f in files if f.stem.split("_")[-1] >= args.since]
    pending = [f for f in files if not (args.dst / f.name).exists()]
    if not pending:
        print("没有待投放的文件")
        return
    print(f"待投放 {len(pending)} 个文件 -> {args.dst}")

    for i, f in enumerate(pending):
        shutil.copy2(f, args.dst / f.name)
        print(f"[{time.strftime('%H:%M:%S')}] 投放 {f.name}（剩余 {len(pending) - i - 1}）", flush=True)
        if i + 1 >= args.preload and i + 1 < len(pending):
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
