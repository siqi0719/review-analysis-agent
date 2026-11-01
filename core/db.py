"""SQLite 存储：评论原文、大模型分析结果、方面标签。"""
import sqlite3

from core.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    comment_id     TEXT PRIMARY KEY,
    hash           TEXT UNIQUE NOT NULL,   -- 用户 + 门店 + 内容，用于去重
    user_id        INTEGER,
    rest_id        INTEGER,
    rest_name      TEXT,
    time           TEXT NOT NULL,
    date           TEXT NOT NULL,
    rating         REAL,                   -- 0 分视为无效，存为 NULL
    rating_flavor  REAL,
    rating_env     REAL,
    rating_service REAL,
    content        TEXT NOT NULL,
    source_file    TEXT,
    ingested_at    TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_comments_date ON comments(date);

CREATE TABLE IF NOT EXISTS analysis (
    comment_id     TEXT PRIMARY KEY REFERENCES comments(comment_id),
    llm_score      INTEGER,                -- 大模型判断的满意度 1-5
    summary        TEXT,
    model          TEXT,
    analyzed_at    TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS aspects (
    comment_id     TEXT REFERENCES comments(comment_id),
    tag            TEXT NOT NULL,
    polarity       TEXT NOT NULL CHECK (polarity IN ('正面', '负面')),
    snippet        TEXT
);
CREATE INDEX IF NOT EXISTS idx_aspects_tag ON aspects(tag, polarity);

-- 轮询时已处理过的文件；文件大小或修改时间变化时会重新入库（入库本身按 hash 去重）
CREATE TABLE IF NOT EXISTS ingested_files (
    name           TEXT PRIMARY KEY,
    size           INTEGER,
    mtime          REAL,
    new_comments   INTEGER,
    ingested_at    TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS poll_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT,
    finished_at    TEXT,
    trigger        TEXT,                   -- schedule / manual / agent
    new_files      INTEGER,
    new_comments   INTEGER,
    analyzed       INTEGER,
    reports        TEXT,                   -- 生成的报告路径，json 数组
    status         TEXT,                   -- ok / no_change / error / busy
    message        TEXT
);

-- 满意度口径：以用户总评分为主，缺失时用大模型判断补上
CREATE VIEW IF NOT EXISTS v_comments AS
SELECT c.*, a.llm_score, a.summary,
       COALESCE(c.rating, a.llm_score) AS satisfaction
FROM comments c LEFT JOIN analysis a USING (comment_id);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 轮询进程、网页和 Agent 会同时读写，开启 WAL 并设置等待时间，避免 database is locked
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
