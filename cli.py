"""
命令行入口。

  python cli.py ingest                  # 把 data/daily 下的评论 Excel 增量写入数据库
  python cli.py taxonomy                # 让大模型归纳标签体系草稿，等待人工确认
  python cli.py extract --days 7        # 分析最近 7 天还没分析过的评论
  python cli.py extract --start 2026-10-01 --end 2026-10-27
  python cli.py report                  # 生成最新一天的日报（--date 指定日期）
"""
import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from core.config import DAILY_DIR, TAXONOMY_DRAFT, TAXONOMY_PATH


def cmd_ingest(args):
    from core.ingest import ingest_dir
    n = ingest_dir(Path(args.dir))
    print(f"新增 {n} 条评论")


def cmd_taxonomy(args):
    from core.taxonomy import induce
    result = induce(args.sample)
    for i, t in enumerate(result["tags"], 1):
        print(f"{i:2}. {t['name']}：{t['definition']}  例：{' / '.join(t.get('examples', []))}")
    print(f"\n草稿已保存到 {TAXONOMY_DRAFT}\n确认或修改后，另存为 {TAXONOMY_PATH} 才会正式生效")


def cmd_extract(args):
    from core.db import connect
    from core.extract import run
    start, end = args.start, args.end
    if args.days:
        latest = connect().execute("SELECT max(date) FROM comments").fetchone()[0]
        end = latest
        start = (date.fromisoformat(latest) - timedelta(days=args.days - 1)).isoformat()
    print(f"分析范围：{start or '最早'} ~ {end or '最新'}")
    print(json.dumps(run(start, end, args.batch, args.workers), ensure_ascii=False))


def cmd_report(args):
    from core.db import connect
    from core.report import generate
    day = args.date or connect().execute("SELECT max(date) FROM comments").fetchone()[0]
    result = generate(day)
    print(f"日报已生成：{result['markdown']}")


def main():
    p = argparse.ArgumentParser(description="评论分析 Agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest")
    s.add_argument("--dir", default=str(DAILY_DIR))
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("taxonomy")
    s.add_argument("--sample", type=int, default=300)
    s.set_defaults(func=cmd_taxonomy)

    s = sub.add_parser("extract")
    s.add_argument("--days", type=int, help="分析数据中最近 N 天")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--batch", type=int, default=20)
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("report")
    s.add_argument("--date", help="报告日期，默认数据中最新的一天")
    s.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
