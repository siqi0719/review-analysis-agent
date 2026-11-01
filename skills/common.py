"""三个 Skill 共用的工具：日期范围、数据读取、HTML 报告渲染。"""
import html
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from core.config import ROOT
from core.db import connect

OUT_DIR = ROOT / "reports" / "skills"
OTHER = "其他"
ECHARTS_JS = "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"

# 满意度配色：低分红、中间黄、高分绿
SAT_COLORS = ["#d94e5d", "#eac736", "#50a3ba"]


def resolve_range(start: str = None, end: str = None, days: int = None) -> tuple:
    """返回 (start, end)。未指定时以数据中最新日期为结束日，向前取 days 天。"""
    conn = connect()
    lo, hi = conn.execute("SELECT min(date), max(date) FROM comments").fetchone()
    conn.close()
    end = end or hi
    if not start:
        start = (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat() if days else lo
    return max(start, lo), min(end, hi)


def previous_range(start: str, end: str) -> tuple:
    """紧挨在前面、长度相同的对比区间。"""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    span = (e - s).days + 1
    return (s - timedelta(days=span)).isoformat(), (s - timedelta(days=1)).isoformat()


def load_comments(start: str, end: str) -> pd.DataFrame:
    conn = connect()
    df = pd.read_sql(
        """SELECT comment_id, rest_id, rest_name, date, rating, rating_flavor, rating_env,
                  rating_service, llm_score, satisfaction, content, summary,
                  comment_id IN (SELECT comment_id FROM analysis) AS analyzed
           FROM v_comments WHERE date BETWEEN ? AND ?""",
        conn, params=(start, end))
    conn.close()
    return df


def load_aspects(start: str, end: str, polarity: str = None) -> pd.DataFrame:
    """每条评论每个标签只保留一行（同一评论多次提到同一标签只算一次），附带一个原文片段。"""
    sql = """SELECT a.comment_id, a.tag, a.polarity, MIN(a.snippet) AS snippet, c.date
             FROM aspects a JOIN comments c USING (comment_id)
             WHERE c.date BETWEEN ? AND ?"""
    params = [start, end]
    if polarity:
        sql += " AND a.polarity = ?"
        params.append(polarity)
    conn = connect()
    df = pd.read_sql(sql + " GROUP BY a.comment_id, a.tag, a.polarity", conn, params=params)
    conn.close()
    return df


def tag_groups() -> dict:
    from core.taxonomy import load
    return {t["name"]: t.get("group", OTHER) for t in load()}


def out_path(skill: str, start: str, end: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"{skill}_{start}_{end}.html"


def table_html(df: pd.DataFrame) -> str:
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in r) + "</tr>"
                   for r in df.itertuples(index=False))
    return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"


def render_page(path: Path, title: str, subtitle: str, insights: list, sections: list) -> Path:
    """sections: [{"title": str, "chart": pyecharts 图表 或 None, "html": str, "height": int}]"""
    blocks, scripts = [], []
    for i, s in enumerate(sections):
        body = ""
        if s.get("chart") is not None:
            body += f'<div id="c{i}" class="chart" style="height:{s.get("height", 460)}px"></div>'
            # 每个图单独 try，一个图出错不影响其他图
            scripts.append(f"try{{var c=echarts.init(document.getElementById('c{i}'));"
                           f"c.setOption({s['chart'].dump_options()});charts.push(c);}}"
                           f"catch(e){{console.error('图表 c{i} 渲染失败',e);"
                           f"document.getElementById('c{i}').innerText='图表渲染失败：'+e.message;}}")
        body += s.get("html", "")
        note = f'<p class="note">{html.escape(s["note"])}</p>' if s.get("note") else ""
        blocks.append(f'<section><h2>{html.escape(s["title"])}</h2>{note}{body}</section>')

    items = "".join(f"<li>{html.escape(t)}</li>" for t in insights)
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<script src="{ECHARTS_JS}"></script>
<style>
body{{margin:0;background:#f6f7f9;color:#222;font-family:"Microsoft YaHei","PingFang SC",sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:24px 16px 48px}}
h1{{font-size:24px;margin:0 0 4px}} .sub{{color:#666;margin:0 0 20px}}
section,.insights{{background:#fff;border-radius:10px;padding:16px 20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
h2{{font-size:17px;margin:0 0 10px}} .note{{color:#777;font-size:13px;margin:-4px 0 10px}}
.insights ul{{margin:6px 0 0;padding-left:20px;line-height:1.8}}
.chart{{width:100%}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#fafafa}}
</style></head><body><main>
<h1>{html.escape(title)}</h1><p class="sub">{html.escape(subtitle)}</p>
<div class="insights"><h2>要点</h2><ul>{items}</ul></div>
{''.join(blocks)}
</main>
<script>var charts=[];{''.join(scripts)}
window.addEventListener('resize',function(){{charts.forEach(function(c){{c.resize();}});}});</script>
</body></html>"""
    path.write_text(page, encoding="utf-8")
    return path


def ensure_analyzed(start: str, end: str, analyze: bool) -> int:
    """范围内还有未分析的评论时，按需调用大模型补齐；返回仍未分析的条数。"""
    conn = connect()
    n = conn.execute(
        """SELECT COUNT(*) FROM comments WHERE date BETWEEN ? AND ?
           AND comment_id NOT IN (SELECT comment_id FROM analysis)""", (start, end)).fetchone()[0]
    conn.close()
    if n and analyze:
        from core.extract import run
        run(start, end)
        return ensure_analyzed(start, end, analyze=False)
    return n
