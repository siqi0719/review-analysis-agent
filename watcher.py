"""
后台轮询进程：每隔 POLL_MINUTES 分钟（默认 60）检查一次 桌面/评论数据 下的新评论文件，并自动完成
入库 → 大模型提取 → 日报 → Skill 报告。启动时立即执行一次。

  python watcher.py               # 按 .env 里的 POLL_MINUTES，默认每小时
  python watcher.py --minutes 2   # 演示时缩短间隔
  python watcher.py --once        # 只跑一次就退出（可配合 Windows 任务计划程序）
"""
import argparse
import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from core.config import POLL_MINUTES, WATCH_DIR
from core.pipeline import poll_once


def main():
    p = argparse.ArgumentParser(description="评论文件自动轮询")
    p.add_argument("--minutes", type=int, default=POLL_MINUTES, help="轮询间隔（分钟）")
    p.add_argument("--once", action="store_true", help="只执行一次")
    args = p.parse_args()

    if args.once:
        poll_once("schedule")
        return

    logging.basicConfig(level=logging.WARNING)
    # 使用本机时区；若写死其他时区，datetime.now() 会被错当成该时区的时间，首次运行被推迟数小时
    sched = BlockingScheduler()
    # max_instances=1 + coalesce：上一次还没跑完时不会叠加，错过的多次只补跑一次
    sched.add_job(poll_once, "interval", minutes=args.minutes, next_run_time=datetime.now(),
                  max_instances=1, coalesce=True, kwargs={"trigger": "schedule"})
    print(f"开始轮询 {WATCH_DIR}，每 {args.minutes} 分钟一次（Ctrl+C 停止）", flush=True)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("已停止")


if __name__ == "__main__":
    main()
