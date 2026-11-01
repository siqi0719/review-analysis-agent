"""读取每日评论 Excel，增量写入 SQLite。重复读取同一文件不会产生重复数据。"""
import hashlib
from pathlib import Path

import pandas as pd

from core.db import connect

# Excel 中文列名 -> 数据库字段
COLUMNS = {
    "评论ID": "comment_id",
    "用户ID": "user_id",
    "门店ID": "rest_id",
    "门店名称": "rest_name",
    "评论时间": "time",
    "总评分": "rating",
    "口味评分": "rating_flavor",
    "环境评分": "rating_env",
    "服务评分": "rating_service",
    "评论内容": "content",
}
REQUIRED = ["评论ID", "评论时间", "评论内容"]


def ingest_file(path: Path, conn) -> int:
    df = pd.read_excel(path)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} 缺少必需列：{missing}")

    df = df[[c for c in COLUMNS if c in df.columns]].rename(columns=COLUMNS)
    df["time"] = pd.to_datetime(df["time"])
    df["date"] = df["time"].dt.strftime("%Y-%m-%d")
    df["time"] = df["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
    for col in ["rating", "rating_flavor", "rating_env", "rating_service"]:
        if col in df:
            df[col] = df[col].where(df[col] > 0)
    df["hash"] = [hashlib.sha1(f"{r.get('user_id')}|{r.get('rest_id')}|{r['content']}".encode()).hexdigest()
                  for _, r in df.iterrows()]
    df["source_file"] = path.name

    df = df.astype(object).where(df.notna(), None)
    cols = list(df.columns)
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO comments ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        df.itertuples(index=False, name=None),
    )
    conn.commit()
    return conn.total_changes - before


def ingest_dir(folder: Path, pattern: str = "评论_*.xlsx") -> int:
    conn = connect()
    total = 0
    for f in sorted(folder.glob(pattern)):
        total += ingest_file(f, conn)
    conn.close()
    return total
