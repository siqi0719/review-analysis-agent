"""
对话 Agent：DeepSeek（v4-pro）+ 工具调用。工具包括 3 个分析 Skill、只读 SQL 查询、日报、立即拉取新文件。
"""
import json
import re
import sqlite3
from datetime import datetime

from core.config import DB_PATH, MODEL_CHAT, REPORT_DIR
from core.db import connect
from core.llm import client
from skills.issue_extract.run import TOOL_SPEC as EXTRACT_SPEC, run as issue_extract
from skills.issue_topology.run import TOOL_SPEC as TOPOLOGY_SPEC, run as issue_topology
from skills.satisfaction_trend.run import TOOL_SPEC as TREND_SPEC, run as satisfaction_trend

MAX_ROUNDS = 8
MAX_TOOL_CHARS = 12000  # 单次工具结果回传给模型的最大字符数
MAX_ROWS = 200

SYSTEM = """你是「大众点评评论分析助手」，帮助用户分析门店评论中的问题和满意度。今天是 {today}。

## 数据
SQLite 数据库，评论日期范围 {date_min} ~ {date_max}，共 {n} 条评论，{n_rest} 家门店（门店名称为「门店_ID」）。
- comments(comment_id, user_id, rest_id, rest_name, time, date, rating 总评分 1-5, rating_flavor 口味评分,
  rating_env 环境评分, rating_service 服务评分, content 评论原文, source_file)
- analysis(comment_id, llm_score 大模型判断的满意度 1-5, summary 一句话摘要)
- aspects(comment_id, tag 方面标签, polarity '正面'/'负面', snippet 原文片段)，一条评论可有多行
- v_comments：comments 关联 analysis 的视图，satisfaction = COALESCE(rating, llm_score) 是统一的满意度口径
- poll_log：自动轮询记录

方面标签（共 16 个 + 其他）：{tags}

## 工作方式
- 问「主要问题/问题排行/哪些问题变多」→ 调用 issue_extract
- 问「问题之间的关联/哪个问题最伤满意度/问题聚类」→ 调用 issue_topology
- 问「满意度变化/趋势/哪天下滑/哪家店变差」→ 调用 satisfaction_trend
- 某一天的情况 → 调用 daily_report
- 具体数字、某家门店、某条评论、自定义统计 → 用 query_db 写 SQL
  （统计问题时按 COUNT(DISTINCT comment_id) 计数，同一评论多次提到同一标签只算一次）
- 用户要求获取最新数据 → 调用 refresh_now
- 「最近 N 天/本周/上个月」都以数据最新日期 {date_max} 为准；SQL 里不要用 date('now')，直接写具体日期
- Skill 会生成可视化报告并自动展示给用户，你不需要描述图表长什么样，重点解读结论

## 回答要求
- 用中文，先给结论，再给 2–4 个支撑数据点
- 所有数字必须来自工具返回的结果，不要编造；工具没有返回的数据就说明无法确定
- 样本量小（例如某天少于 15 条评论）时要提醒结论仅供参考"""


def _run_sql(sql: str) -> dict:
    if not re.match(r"^\s*(select|with)\b", sql, re.I) or ";" in sql.strip().rstrip(";"):
        return {"error": "只允许单条 SELECT / WITH 查询"}
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)  # 只读连接，防止误改数据
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        conn.close()
    return {"columns": cols, "rows": [list(r) for r in rows[:MAX_ROWS]], "truncated": len(rows) > MAX_ROWS}


def _daily_report(date: str) -> dict:
    cached = REPORT_DIR / f"daily_{date}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    from core.report import generate
    r = generate(date)
    return {"stats": r["stats"], "report": r["report"]}


def _refresh_now() -> dict:
    from core.pipeline import poll_once
    r = poll_once("agent", log=lambda *_: None)
    return {k: r[k] for k in ("status", "new_files", "new_comments", "analyzed", "message")}


def _skill(fn):
    def call(**kw):
        r = fn(**kw)
        return {"insights": r["insights"], "data": r["data"], "html": r["html"]}
    return call


TOOLS = {
    "issue_extract": (_skill(issue_extract), EXTRACT_SPEC),
    "issue_topology": (_skill(issue_topology), TOPOLOGY_SPEC),
    "satisfaction_trend": (_skill(satisfaction_trend), TREND_SPEC),
    "query_db": (lambda sql: _run_sql(sql), {
        "name": "query_db",
        "description": f"在评论数据库上执行只读 SQL（SQLite 语法），最多返回 {MAX_ROWS} 行。",
        "parameters": {"type": "object", "properties": {"sql": {"type": "string", "description": "单条 SELECT 语句"}},
                       "required": ["sql"]},
    }),
    "daily_report": (lambda date: _daily_report(date), {
        "name": "daily_report",
        "description": "获取某一天的评论日报（核心指标、重点问题、典型原话、改进建议）；没有现成日报时会自动生成。",
        "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
                       "required": ["date"]},
    }),
    "refresh_now": (lambda: _refresh_now(), {
        "name": "refresh_now",
        "description": "立即检查桌面「评论数据」文件夹，把新的评论文件入库并完成分析。",
        "parameters": {"type": "object", "properties": {}},
    }),
}


def _system_prompt() -> str:
    from core.taxonomy import load
    conn = connect()
    lo, hi, n, n_rest = conn.execute(
        "SELECT min(date), max(date), count(*), count(DISTINCT rest_id) FROM comments").fetchone()
    conn.close()
    tags = "、".join(t["name"] for t in load())
    return SYSTEM.format(today=datetime.now().strftime("%Y-%m-%d"), date_min=lo, date_max=hi, n=n,
                         n_rest=n_rest, tags=tags)


def chat(history: list, user_msg: str, on_step=None) -> dict:
    """
    history: 之前的 [{"role": "user"/"assistant", "content": str}]（只保留文字，不含工具细节）
    返回 {"answer": str, "steps": [{"tool", "args", "result"}], "html": [报告路径]}
    """
    messages = [{"role": "system", "content": _system_prompt()}] + history + [{"role": "user", "content": user_msg}]
    tools = [{"type": "function", "function": spec} for _, spec in TOOLS.values()]
    steps, html = [], []

    for _ in range(MAX_ROUNDS):
        resp = client.chat.completions.create(
            model=MODEL_CHAT, messages=messages, tools=tools, temperature=0.3,
            extra_body={"thinking": {"type": "disabled"}})
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return {"answer": msg.content or "", "steps": steps, "html": html}

        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
                fn = TOOLS[name][0]
                result = fn(**args)
            except Exception as e:  # 工具出错时把错误告诉模型，让它调整参数或换个方式
                args = locals().get("args", {})
                result = {"error": f"{type(e).__name__}: {e}"}
            if isinstance(result, dict) and result.get("html"):
                html.append(result["html"])
            step = {"tool": name, "args": args, "result": result}
            steps.append(step)
            if on_step:
                on_step(step)
            content = json.dumps(result, ensure_ascii=False, default=str)
            if len(content) > MAX_TOOL_CHARS:
                content = content[:MAX_TOOL_CHARS] + "…（结果过长已截断）"
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

    return {"answer": "这个问题需要的步骤太多了，请把问题拆小一点再问我。", "steps": steps, "html": html}
