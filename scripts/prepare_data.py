"""
从大众点评 yf_dianping 数据（HuggingFace 镜像的 parquet 分片）中抽样，并按天拆分成 Excel 评论文件。

流程：
  1. 读取 data/raw 下所有 .parquet，统计每家餐厅的有效评论数，选出评论最多的 N 家
     （如果有 restaurants.csv，也可以用 --keyword 按店名筛选）
  2. 只取这些餐厅的评论，清洗、去重
  3. 找到评论最密集的连续 D 天作为时间窗口
  4. 把窗口整体平移到最近（结束日 = 昨天），保留原始时间方便追溯
  5. 每天最多保留 M 条（控制大模型调用成本），输出 评论_YYYYMMDD.xlsx

用法：
  python scripts/prepare_data.py
  python scripts/prepare_data.py --top-n 30 --days 180 --max-per-day 80
"""
import argparse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

ROOT = Path(__file__).resolve().parent.parent

# 原始字段 -> 输出到 Excel 的中文列名
COLUMNS = {
    "comment_id": "评论ID",
    "userId": "用户ID",
    "restId": "门店ID",
    "name": "门店名称",
    "time": "评论时间",
    "rating": "总评分",
    "rating_flavor": "口味评分",
    "rating_env": "环境评分",
    "rating_service": "服务评分",
    "comment": "评论内容",
    "orig_time": "原始时间",
}


def parse_args():
    p = argparse.ArgumentParser(description="yf_dianping 抽样与按天拆分")
    p.add_argument("--raw-dir", default=ROOT / "data" / "raw", type=Path)
    p.add_argument("--out-dir", default=ROOT / "data" / "daily", type=Path)
    p.add_argument("--top-n", type=int, default=100, help="选取评论数最多的 N 家餐厅")
    p.add_argument("--keyword", help="按店名关键字筛选餐厅（需要 restaurants.csv）")
    p.add_argument("--days", type=int, default=365, help="时间窗口长度（天）")
    p.add_argument("--max-per-day", type=int, default=60, help="每天最多保留的评论数")
    p.add_argument("--min-len", type=int, default=8, help="评论最短字数")
    p.add_argument("--end-date", type=date.fromisoformat, default=date.today() - timedelta(days=1),
                   help="平移后的窗口结束日，默认昨天")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def parquet_files(raw_dir: Path) -> list:
    files = sorted(raw_dir.rglob("*.parquet"))
    if not files:
        raise SystemExit(f"在 {raw_dir} 下找不到 .parquet 文件，请先下载 yf_dianping 分片")
    return files


def load_restaurants(raw_dir: Path) -> pd.DataFrame:
    # HuggingFace 镜像没有餐厅名称表；如果手动放了 restaurants.csv 就用上
    hits = list(raw_dir.rglob("restaurants.csv"))
    if hits:
        return pd.read_csv(hits[0])[["restId", "name"]]
    return pd.DataFrame(columns=["restId", "name"])


def pick_restaurants(files: list, restaurants: pd.DataFrame, args) -> list:
    df = pd.concat(pd.read_parquet(f, columns=["restId", "comment"]) for f in files)
    df = df[df["comment"].str.len() >= args.min_len]
    counts = df["restId"].value_counts()

    if args.keyword:
        if restaurants.empty:
            raise SystemExit("--keyword 需要 data/raw 下有 restaurants.csv")
        ids = restaurants.loc[restaurants["name"].str.contains(args.keyword, na=False), "restId"]
        counts = counts[counts.index.isin(ids)]
        if counts.empty:
            raise SystemExit(f"没有店名包含「{args.keyword}」的餐厅")

    top = counts.head(args.top_n)
    print(f"选中 {len(top)} 家餐厅，共 {int(top.sum())} 条有效评论")
    return top.index.astype("int64").tolist()


def load_comments(files: list, rest_ids: list, args) -> pd.DataFrame:
    df = pd.concat(pd.read_parquet(f, filters=[("restId", "in", rest_ids)]) for f in files)

    df["comment"] = (df["comment"].astype(str)
                     .map(lambda s: ILLEGAL_CHARACTERS_RE.sub("", s))
                     .str.replace(r"\s+", " ", regex=True).str.strip())
    df = df[df["comment"].str.len() >= args.min_len]
    df = df.dropna(subset=["rating", "timestamp"])
    df = df.drop_duplicates(subset=["userId", "restId", "comment"])

    # 原始时间戳是毫秒级 Unix 时间，转成北京时间
    df["orig_time"] = (pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                       .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None))
    return df


def densest_window(df: pd.DataFrame, days: int) -> tuple:
    daily = df["orig_time"].dt.normalize().value_counts().sort_index()
    daily = daily.asfreq("D", fill_value=0)
    if len(daily) <= days:
        return daily.index[0], daily.index[-1]
    end = daily.rolling(days).sum().idxmax()
    return end - pd.Timedelta(days=days - 1), end


def main():
    args = parse_args()
    files = parquet_files(args.raw_dir)
    restaurants = load_restaurants(args.raw_dir)

    rest_ids = pick_restaurants(files, restaurants, args)
    df = load_comments(files, rest_ids, args)

    start, end = densest_window(df, args.days)
    df = df[(df["orig_time"] >= start) & (df["orig_time"] < end + pd.Timedelta(days=1))]
    print(f"原始时间窗口：{start.date()} ~ {end.date()}，{len(df)} 条")

    shift = pd.Timestamp(args.end_date) - end
    df["time"] = df["orig_time"] + shift

    # 每天随机抽 max-per-day 条
    df = (df.groupby(df["time"].dt.date, group_keys=False)
            .apply(lambda g: g.sample(min(len(g), args.max_per_day), random_state=args.seed))
            .sort_values("time").reset_index(drop=True))

    df = df.merge(restaurants, on="restId", how="left")
    df["name"] = df["name"].fillna("门店_" + df["restId"].astype(str))
    df["comment_id"] = [f"C{i:07d}" for i in range(1, len(df) + 1)]
    df = df[list(COLUMNS)].rename(columns=COLUMNS)

    # 清掉上一次生成的每日文件，避免不同参数的结果混在一起
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for old in args.out_dir.glob("评论_*.xlsx"):
        old.unlink()
    processed = ROOT / "data" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    df.to_csv(processed / "sample_all.csv", index=False, encoding="utf-8-sig")

    for day, g in df.groupby(df["评论时间"].dt.date):
        g.to_excel(args.out_dir / f"评论_{day:%Y%m%d}.xlsx", index=False)

    n_days = df["评论时间"].dt.date.nunique()
    print(f"完成：{len(df)} 条评论，{n_days} 个每日文件 -> {args.out_dir}")
    print(f"平移后时间范围：{df['评论时间'].min().date()} ~ {df['评论时间'].max().date()}，"
          f"日均 {len(df) / n_days:.1f} 条")


if __name__ == "__main__":
    main()
