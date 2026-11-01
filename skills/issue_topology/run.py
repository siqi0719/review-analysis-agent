"""
Skill 2：问题关系拓扑 —— 哪些负面问题经常在同一条评论里一起出现，以及每个问题对满意度的影响。

关联强度用提升度 lift = P(A∩B) / (P(A)·P(B))，统计口径为「至少有一个负面问题的评论」：
lift > 1 表示两个问题一起出现的次数比随机情况更多；同时要求共现评论数 ≥ min_count，过滤偶然结果。

  python skills/issue_topology/run.py --days 90
  python skills/issue_topology/run.py --start 2026-07-01 --end 2026-09-30 --min-count 15
"""
import argparse
import itertools
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 允许从任意目录直接运行

import networkx as nx  # noqa: E402
import pandas as pd  # noqa: E402
from pyecharts import options as opts  # noqa: E402
from pyecharts.charts import Bar, Graph, HeatMap  # noqa: E402

from skills.common import (OTHER, SAT_COLORS, ensure_analyzed, load_aspects, load_comments,  # noqa: E402
                           out_path, render_page, resolve_range, table_html)

SKILL = "issue_topology"

TOOL_SPEC = {
    "name": SKILL,
    "description": "问题关系拓扑：分析负面问题之间的共现关联（提升度 lift）、问题聚类，以及每个问题出现时满意度下降多少，"
                   "生成力导向网络图、关联热力图等可视化 HTML 报告。",
    "parameters": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
            "end": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
            "days": {"type": "integer", "description": "未给 start 时，取截至 end 的最近 N 天，默认 90"},
            "min_count": {"type": "integer", "description": "连线所需的最少共现评论数，默认 10"},
        },
    },
}


def _color(score: float, lo: float = 2.5, hi: float = 4.0) -> str:
    """满意度映射为颜色：越低越红，越高越蓝。"""
    x = min(1.0, max(0.0, (score - lo) / (hi - lo)))
    a, b = (SAT_COLORS[0], SAT_COLORS[1]) if x < 0.5 else (SAT_COLORS[1], SAT_COLORS[2])
    t = x * 2 if x < 0.5 else (x - 0.5) * 2
    rgb = [round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t) for i in (1, 3, 5)]
    return "#" + "".join(f"{v:02x}" for v in rgb)


def run(start: str = None, end: str = None, days: int = 90, min_count: int = 10, analyze: bool = True) -> dict:
    start, end = resolve_range(start, end, days)
    unanalyzed = ensure_analyzed(start, end, analyze)
    comments = load_comments(start, end)
    comments = comments[comments["analyzed"] == 1].set_index("comment_id")

    neg = load_aspects(start, end, "负面")
    neg = neg[neg["tag"] != OTHER]
    sets = neg.groupby("comment_id")["tag"].apply(frozenset)
    N = len(sets)
    if N < 30:
        raise ValueError(f"{start} ~ {end} 只有 {N} 条带负面问题的评论，样本太少，请扩大时间范围")

    single = Counter(t for s in sets for t in s)
    pair = Counter(p for s in sets if len(s) > 1 for p in itertools.combinations(sorted(s), 2))

    edges = []
    for (a, b), c in pair.items():
        lift = c * N / (single[a] * single[b])
        if c >= min_count and lift > 1:
            edges.append({"a": a, "b": b, "count": c, "lift": round(lift, 2)})
    edges.sort(key=lambda e: e["lift"], reverse=True)

    # 每个问题出现时的平均满意度，以及相对没有该问题时的落差
    sat = comments["satisfaction"]
    impact = []
    for tag in single:
        ids = neg.loc[neg["tag"] == tag, "comment_id"].unique()
        with_tag = sat[sat.index.isin(ids)].mean()
        without = sat[~sat.index.isin(ids)].mean()
        impact.append({"tag": tag, "comments": single[tag], "satisfaction": round(with_tag, 2),
                       "drop": round(with_tag - without, 2)})
    impact = pd.DataFrame(impact).set_index("tag").sort_values("drop")

    # 问题聚类：在关联网络上做社区发现
    G = nx.Graph()
    G.add_nodes_from(single)
    G.add_weighted_edges_from((e["a"], e["b"], e["lift"]) for e in edges)
    clusters = [sorted(c, key=lambda t: -single[t])
                for c in nx.community.louvain_communities(G, weight="weight", seed=42) if len(c) > 1]
    clusters.sort(key=lambda c: -sum(single[t] for t in c))

    # 图 1：关系网络图。坐标用 networkx 的力导向算法算好，ECharts 会把坐标缩放到容器大小，
    # 任何宽度下都不会出界（在浏览器里实时做力导向时，容器宽度为 0 会让节点挤成一条线）
    # 没有任何连线的孤立问题会被力导向推到很远，把其余节点挤到角落；所以单独排在网络下方一行
    linked = G.subgraph([t for t in G if G.degree(t) > 0])
    pos = nx.spring_layout(linked, weight="weight", k=1.6 / max(len(linked), 1) ** 0.5,
                           iterations=200, seed=42) if len(linked) else {}
    isolated = [t for t in G if t not in pos]
    for i, t in enumerate(isolated):
        pos[t] = ((i + 1) / (len(isolated) + 1) * 2 - 1, 1.25)  # ECharts 的 y 轴向下，正值在底部
    max_n = max(single.values())
    nodes = [opts.GraphNode(name=t, value=single[t], symbol_size=18 + 52 * (single[t] / max_n) ** 0.5,
                            x=float(pos[t][0] * 1000), y=float(pos[t][1] * 600),
                            itemstyle_opts=opts.ItemStyleOpts(color=_color(impact.loc[t, "satisfaction"])),
                            label_opts=opts.LabelOpts(is_show=True, font_size=13))
             for t in single]
    max_lift = max((e["lift"] for e in edges), default=1)
    links = [opts.GraphLink(source=e["a"], target=e["b"], value=e["lift"],
                            linestyle_opts=opts.LineStyleOpts(width=1 + 7 * (e["lift"] - 1) / max(max_lift - 1, 0.01),
                                                              opacity=0.6, curve=0.1))
             for e in edges]
    graph = (Graph()
             .add("", nodes, links, layout="none", is_draggable=True, is_roam=True, is_focusnode=True,
                  tooltip_opts=opts.TooltipOpts(formatter="{b}：{c}")))

    # 图 2：问题对满意度的影响
    imp_bar = (Bar()
               .add_xaxis(impact.index.tolist())
               .add_yaxis("满意度落差", [opts.BarItem(name=t, value=float(v),
                                                  itemstyle_opts=opts.ItemStyleOpts(color=_color(impact.loc[t, "satisfaction"])))
                                      for t, v in impact["drop"].items()],
                          label_opts=opts.LabelOpts(position="bottom"))
               .set_global_opts(xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=40)),
                                yaxis_opts=opts.AxisOpts(name="分"),
                                legend_opts=opts.LegendOpts(is_show=False)))

    # 图 3：关联热力图（只显示满足最少共现数的格子）
    order = [t for t, _ in single.most_common()]
    heat = []
    for (a, b), c in pair.items():
        if c >= min_count:
            lift = round(c * N / (single[a] * single[b]), 2)
            heat += [[order.index(a), order.index(b), lift], [order.index(b), order.index(a), lift]]
    heatmap = (HeatMap()
               .add_xaxis(order)
               .add_yaxis("lift", order, heat, label_opts=opts.LabelOpts(is_show=True, font_size=10))
               .set_global_opts(
                   xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=40)),
                   visualmap_opts=opts.VisualMapOpts(min_=0.5, max_=max(2.0, max_lift), is_calculable=True,
                                                     range_color=["#50a3ba", "#f7f7f7", "#d94e5d"],
                                                     orient="horizontal", pos_left="center", pos_bottom="0"),
                   legend_opts=opts.LegendOpts(is_show=False)))

    # 关联最强的问题对 + 同时提到两者的评论摘要
    rows = []
    for e in edges[:10]:
        both = [cid for cid, s in sets.items() if e["a"] in s and e["b"] in s]
        example = comments.loc[comments.index.isin(both), "summary"].dropna().head(2).tolist()
        rows.append({"问题 A": e["a"], "问题 B": e["b"], "共现评论数": e["count"], "lift": e["lift"],
                     "评论摘要示例": " ／ ".join(example)})

    insights = [f"{start} ~ {end}：{len(comments)} 条已分析评论中，{N} 条带有负面问题，"
                f"其中 {sum(1 for s in sets if len(s) > 1)} 条同时提到多个问题"
                + (f"（{unanalyzed} 条未分析）" if unanalyzed else "")]
    if edges:
        insights.append("关联最强的问题对：" + "、".join(f"{e['a']}＋{e['b']}（lift {e['lift']}，{e['count']} 条）"
                                              for e in edges[:3]))
    insights.append("对满意度伤害最大的问题：" + "、".join(f"{t}（{r['drop']:+.2f} 分）"
                                              for t, r in impact.head(3).iterrows()))
    for i, c in enumerate(clusters[:3], 1):
        insights.append(f"问题群 {i}：" + "、".join(c))

    sections = [
        {"title": "负面问题关系网络", "chart": graph, "height": 620,
         "note": f"节点大小 = 带该负面问题的评论数；节点颜色 = 这些评论的平均满意度（红低蓝高）；"
                 f"连线粗细 = lift，只显示共现 ≥ {min_count} 条且 lift > 1 的关联。可拖拽、缩放，悬停高亮相邻问题"},
        {"title": "各问题对满意度的影响", "chart": imp_bar, "height": 380,
         "note": "带有该负面问题的评论平均满意度，减去没有该问题的评论平均满意度"},
        {"title": "问题关联热力图（lift）", "chart": heatmap, "height": 620,
         "note": f"红色表示两个问题经常一起出现，蓝色表示很少一起出现；空白格为共现不足 {min_count} 条"},
        {"title": "关联最强的问题对", "html": table_html(pd.DataFrame(rows)) if rows else "<p>没有满足条件的关联</p>"},
    ]
    path = render_page(out_path(SKILL, start, end), "问题关系拓扑", f"{start} ~ {end}", insights, sections)
    return {
        "skill": SKILL,
        "range": [start, end],
        "insights": insights,
        "data": {
            "negative_comments": N,
            "edges": edges,
            "impact": impact.reset_index().to_dict("records"),
            "clusters": clusters,
        },
        "html": str(path),
    }


def main():
    p = argparse.ArgumentParser(description=TOOL_SPEC["description"])
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--min-count", type=int, default=10)
    p.add_argument("--no-analyze", action="store_true", help="不调用大模型补分析未处理的评论")
    a = p.parse_args()
    result = run(a.start, a.end, a.days, a.min_count, analyze=not a.no_analyze)
    print("\n".join(result["insights"]))
    print(f"\n报告：{result['html']}")


if __name__ == "__main__":
    main()
