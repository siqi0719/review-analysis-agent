"""每日报告：先用 SQL 算好当天的统计数字，再让大模型基于这些数字写结论和建议。"""
import json
from datetime import date, timedelta

import pandas as pd

from core.config import MODEL_CHAT, ROOT
from core.db import connect
from core.llm import chat_json

REPORT_DIR = ROOT / "reports"

SYSTEM = """你是餐饮门店的用户体验分析师。下面给你某一天的大众点评评论统计数据（json），
请写一份简洁的每日分析报告。

要求：
1. 所有数字只能引用输入里给出的，不要编造或自行推算新的数字；
   标签的 comments 字段是「提到该问题的评论条数」，请表述为「X 条评论提到」，不要说成「提及 X 次」
2. 当天评论数少于 15 条时，在 headline 里提醒样本量小、结论仅供参考
3. key_issues 选 2–4 个最值得关注的负面问题，quotes 必须从输入的原文片段里原样摘取
4. suggestions 给 2–4 条可以落地的改进建议，对应具体问题

只输出 json，格式如下：
{"headline": "一句话总结当天情况",
 "satisfaction": "满意度变化的解读，1-2 句",
 "key_issues": [{"tag": "排队等位", "analysis": "问题说明，1-2 句", "quotes": ["原文片段1", "原文片段2"]}],
 "highlights": ["用户认可的方面，1 句话一条"],
 "suggestions": ["改进建议"]}"""


def _tag_stats(conn, day: str, polarity: str) -> pd.DataFrame:
    # 按「提到该标签的评论数」计数，同一条评论多次提到同一标签只算一次
    return pd.read_sql(
        """SELECT tag, COUNT(DISTINCT comment_id) AS comments
           FROM aspects JOIN comments USING (comment_id)
           WHERE date = ? AND polarity = ? GROUP BY tag ORDER BY comments DESC""",
        conn, params=(day, polarity))


def _snippets(conn, day: str, tag: str, polarity: str, limit: int = 5) -> list:
    rows = conn.execute(
        """SELECT DISTINCT snippet FROM aspects JOIN comments USING (comment_id)
           WHERE date = ? AND tag = ? AND polarity = ? AND snippet != '' LIMIT ?""",
        (day, tag, polarity, limit))
    return [r[0] for r in rows]


def collect_stats(day: str) -> dict:
    conn = connect()
    today = conn.execute(
        "SELECT COUNT(*) n, AVG(satisfaction) sat, SUM(satisfaction <= 2) low FROM v_comments WHERE date = ?",
        (day,)).fetchone()
    if not today["n"]:
        conn.close()
        raise SystemExit(f"{day} 没有评论数据")

    analyzed = conn.execute(
        "SELECT COUNT(*) FROM analysis JOIN comments USING (comment_id) WHERE date = ?", (day,)).fetchone()[0]
    week_start = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    prev = conn.execute(
        "SELECT COUNT(*) n, AVG(satisfaction) sat FROM v_comments WHERE date >= ? AND date < ?",
        (week_start, day)).fetchone()

    neg, pos = _tag_stats(conn, day, "负面"), _tag_stats(conn, day, "正面")
    stats = {
        "date": day,
        "comments": today["n"],
        "analyzed": analyzed,
        "avg_satisfaction": round(today["sat"], 2) if today["sat"] else None,
        "low_satisfaction_comments": today["low"],
        "prev_7d_comments": prev["n"],
        "prev_7d_avg_satisfaction": round(prev["sat"], 2) if prev["sat"] else None,
        "negative_tags": [{"tag": r.tag, "comments": r.comments,
                           "snippets": _snippets(conn, day, r.tag, "负面")} for r in neg.head(6).itertuples()],
        "positive_tags": [{"tag": r.tag, "comments": r.comments,
                           "snippets": _snippets(conn, day, r.tag, "正面", 3)} for r in pos.head(4).itertuples()],
    }
    conn.close()
    return stats


def _render_markdown(stats: dict, llm: dict) -> str:
    sat, prev = stats["avg_satisfaction"], stats["prev_7d_avg_satisfaction"]
    delta = f"{sat - prev:+.2f}" if sat is not None and prev is not None else "—"
    lines = [
        f"# 评论日报 {stats['date']}",
        "",
        f"> {llm.get('headline', '')}",
        "",
        "## 核心指标",
        "| 评论数 | 已分析 | 平均满意度 | 较前 7 天 | 低满意度（≤2 分） |",
        "|---|---|---|---|---|",
        f"| {stats['comments']} | {stats['analyzed']} | {sat} | {delta} | {stats['low_satisfaction_comments']} |",
        "",
        llm.get("satisfaction", ""),
        "",
        "## 重点问题",
    ]
    for issue in llm.get("key_issues", []):
        lines.append(f"### {issue.get('tag')}")
        lines.append(issue.get("analysis", ""))
        lines += [f"- 「{q}」" for q in issue.get("quotes", [])]
        lines.append("")
    lines.append("## 负面问题分布（提到该问题的评论数）")
    lines += [f"- {t['tag']}：{t['comments']}" for t in stats["negative_tags"]]
    lines += ["", "## 用户认可"]
    lines += [f"- {h}" for h in llm.get("highlights", [])]
    lines += ["", "## 改进建议"]
    lines += [f"{i}. {s}" for i, s in enumerate(llm.get("suggestions", []), 1)]
    return "\n".join(lines) + "\n"


def generate(day: str) -> dict:
    stats = collect_stats(day)
    if stats["analyzed"] < stats["comments"]:
        print(f"注意：{day} 有 {stats['comments'] - stats['analyzed']} 条评论还没分析，报告只基于已分析部分")
    llm = chat_json(SYSTEM, json.dumps(stats, ensure_ascii=False), model=MODEL_CHAT)

    REPORT_DIR.mkdir(exist_ok=True)
    md_path = REPORT_DIR / f"daily_{day}.md"
    md_path.write_text(_render_markdown(stats, llm), encoding="utf-8")
    (REPORT_DIR / f"daily_{day}.json").write_text(
        json.dumps({"stats": stats, "report": llm}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"markdown": md_path, "stats": stats, "report": llm}
