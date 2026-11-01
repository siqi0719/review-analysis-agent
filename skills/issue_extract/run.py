"""
Skill 1：问题提取与分类 —— 指定时间范围内，用户在哪些方面夸、在哪些方面抱怨，哪些问题在变多。

  python skills/issue_extract/run.py --days 30
  python skills/issue_extract/run.py --start 2026-09-01 --end 2026-09-30
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 允许从任意目录直接运行

import pandas as pd  # noqa: E402
from pyecharts import options as opts  # noqa: E402
from pyecharts.charts import Bar, Sunburst  # noqa: E402

from skills.common import (OTHER, ensure_analyzed, load_aspects, load_comments, out_path,  # noqa: E402
                           previous_range, render_page, resolve_range, table_html, tag_groups)

SKILL = "issue_extract"

# 给 DeepSeek Agent 注册工具时使用的参数说明（OpenAI function calling 格式）
TOOL_SPEC = {
    "name": SKILL,
    "description": "问题提取与分类：统计时间范围内每个方面标签的正面/负面评论数、与上一周期相比增长最快的问题、"
                   "各问题的典型原话，并生成可视化 HTML 报告。",
    "parameters": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
            "end": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
            "days": {"type": "integer", "description": "未给 start 时，取截至 end 的最近 N 天，默认 30"},
            "top": {"type": "integer", "description": "典型原话展示前几个负面问题，默认 6"},
        },
    },
}


def _tag_table(asp: pd.DataFrame, n_comments: int) -> pd.DataFrame:
    t = (asp.pivot_table(index="tag", columns="polarity", values="comment_id", aggfunc="nunique", fill_value=0)
         .reindex(columns=["负面", "正面"], fill_value=0))
    t["负面率"] = (t["负面"] / n_comments * 100).round(1)  # 每百条评论中提到该负面问题的条数
    t["负面占比"] = (t["负面"] / (t["负面"] + t["正面"]).clip(lower=1) * 100).round(1)
    return t.sort_values("负面", ascending=False)


def run(start: str = None, end: str = None, days: int = 30, top: int = 6, analyze: bool = True) -> dict:
    start, end = resolve_range(start, end, days)
    unanalyzed = ensure_analyzed(start, end, analyze)
    comments = load_comments(start, end)
    n = int(comments["analyzed"].sum())
    if n == 0:
        raise ValueError(f"{start} ~ {end} 没有已分析的评论")

    cur = _tag_table(load_aspects(start, end), n)
    p_start, p_end = previous_range(start, end)
    prev_comments = load_comments(p_start, p_end)
    n_prev = int(prev_comments["analyzed"].sum())
    prev = _tag_table(load_aspects(p_start, p_end), n_prev) if n_prev else None

    # 环比：负面率（每百条评论）的变化
    if prev is not None:
        cur["上期负面率"] = prev["负面率"].reindex(cur.index).fillna(0)
        cur["负面率变化"] = (cur["负面率"] - cur["上期负面率"]).round(1)

    # 每个负面问题取几条典型原话（按片段长度挑信息量较大的）
    neg = load_aspects(start, end, "负面")
    neg = neg[neg["snippet"].str.len() > 0]
    quotes = {}
    for tag in [t for t in cur.index if t != OTHER][:top]:
        s = neg.loc[neg["tag"] == tag, "snippet"].drop_duplicates()
        quotes[tag] = s.loc[s.str.len().sort_values(ascending=False).index].head(4).tolist()

    tags = [t for t in cur.index]
    order = list(reversed(tags))  # 横向条形图从下往上画，倒序让最多的在最上面

    # 图 1：正负面对比条形图
    bar = (Bar(init_opts=opts.InitOpts(height="560px"))
           .add_xaxis(order)
           .add_yaxis("负面", [-int(cur.loc[t, "负面"]) for t in order], stack="s", color="#d94e5d",
                      label_opts=opts.LabelOpts(is_show=False))
           .add_yaxis("正面", [int(cur.loc[t, "正面"]) for t in order], stack="s", color="#50a3ba",
                      label_opts=opts.LabelOpts(is_show=False))
           .reversal_axis()
           .set_global_opts(
               tooltip_opts=opts.TooltipOpts(trigger="axis", axis_pointer_type="shadow"),
               xaxis_opts=opts.AxisOpts(name="评论数", axislabel_opts=opts.LabelOpts(formatter="{value}")),
               legend_opts=opts.LegendOpts(pos_top="0")))

    # 图 2：负面问题按分组的旭日图
    groups = tag_groups()
    sun_data = []
    for g in dict.fromkeys(groups.values()):
        children = [opts.SunburstItem(name=t, value=int(cur.loc[t, "负面"]))
                    for t, gg in groups.items() if gg == g and t in cur.index and cur.loc[t, "负面"] > 0]
        if children:
            sun_data.append(opts.SunburstItem(name=g, children=children))
    sun = (Sunburst()
           .add("", sun_data, radius=[0, "90%"], highlight_policy="ancestor",
                label_opts=opts.LabelOpts(rotate="radial"))
           .set_global_opts(tooltip_opts=opts.TooltipOpts(formatter="{b}：{c} 条")))

    sections = [
        {"title": "各方面正负面评论数", "chart": bar, "height": 560,
         "note": "按「提到该方面的评论条数」计；同一条评论多次提到同一方面只算一次"},
        {"title": "负面问题构成", "chart": sun, "height": 480, "note": "内圈为问题大类，外圈为具体问题"},
    ]

    # 图 3：环比变化
    if prev is not None:
        chg = cur.drop(index=OTHER, errors="ignore").sort_values("负面率变化")
        chg_bar = (Bar()
                   .add_xaxis(chg.index.tolist())
                   .add_yaxis("负面率变化（每百条评论）",
                              [opts.BarItem(name=t, value=float(v),
                                            itemstyle_opts=opts.ItemStyleOpts(color="#d94e5d" if v > 0 else "#50a3ba"))
                               for t, v in chg["负面率变化"].items()],
                              label_opts=opts.LabelOpts(position="top"))
                   .set_global_opts(xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=40)),
                                    legend_opts=opts.LegendOpts(is_show=False)))
        sections.append({"title": f"与上一周期相比（{p_start} ~ {p_end}）", "chart": chg_bar, "height": 380,
                         "note": "红色表示该问题在本期更常被抱怨，蓝色表示有所改善"})

    quote_df = pd.DataFrame([{"问题": t, "负面评论数": int(cur.loc[t, "负面"]), "典型原话": " ／ ".join(q)}
                             for t, q in quotes.items()])
    sections.append({"title": "典型原话", "html": table_html(quote_df)})

    # 要点（规则生成，不额外调用大模型）
    real = cur.drop(index=OTHER, errors="ignore")
    insights = [f"{start} ~ {end} 共 {len(comments)} 条评论，已分析 {n} 条"
                + (f"（{unanalyzed} 条未分析）" if unanalyzed else "")]
    top3 = real.head(3)
    insights.append("被抱怨最多的问题：" + "、".join(f"{t}（{int(r['负面'])} 条，每百条 {r['负面率']}）"
                                           for t, r in top3.iterrows()))
    worst = real[real["负面"] + real["正面"] >= 20].sort_values("负面占比", ascending=False).head(3)
    insights.append("口碑最差的方面（负面占比最高）：" + "、".join(f"{t} {r['负面占比']}%" for t, r in worst.iterrows()))
    if prev is not None:
        up = real[real["负面率变化"] > 0].sort_values("负面率变化", ascending=False).head(3)
        if len(up):
            insights.append("比上一周期增多的问题：" + "、".join(f"{t} +{r['负面率变化']}" for t, r in up.iterrows()))
    if OTHER in cur.index:
        insights.append(f"「其他」类负面 {int(cur.loc[OTHER, '负面'])} 条，未归入任何具体问题")

    path = render_page(out_path(SKILL, start, end), "问题提取与分类", f"{start} ~ {end}", insights, sections)
    return {
        "skill": SKILL,
        "range": [start, end],
        "insights": insights,
        "data": {
            "analyzed_comments": n,
            "tags": cur.reset_index().rename(columns={"tag": "标签"}).to_dict("records"),
            "quotes": quotes,
        },
        "html": str(path),
    }


def main():
    p = argparse.ArgumentParser(description=TOOL_SPEC["description"])
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--top", type=int, default=6)
    p.add_argument("--no-analyze", action="store_true", help="不调用大模型补分析未处理的评论")
    a = p.parse_args()
    result = run(a.start, a.end, a.days, a.top, analyze=not a.no_analyze)
    print("\n".join(result["insights"]))
    print(f"\n报告：{result['html']}")


if __name__ == "__main__":
    main()
