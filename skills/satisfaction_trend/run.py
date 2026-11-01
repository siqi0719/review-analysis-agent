"""
Skill 3：满意度变化 —— 满意度怎么变、哪几天异常下跌、下跌时哪些问题变多了、哪些门店在变差。

满意度口径：用户总评分为主，缺失时用大模型判断的分数补上（见 v_comments.satisfaction）。
异常下跌：当天评论数 ≥ min_n，且当天均值显著低于前 28 天基线
          （z = (当天均值 − 基线均值) / (基线单条标准差 / √当天条数) ≤ −2，且至少低 0.3 分）。

  python skills/satisfaction_trend/run.py              # 全部数据
  python skills/satisfaction_trend/run.py --days 180
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 允许从任意目录直接运行

import pandas as pd  # noqa: E402
from pyecharts import options as opts  # noqa: E402
from pyecharts.charts import Bar, HeatMap, Line  # noqa: E402

from skills.common import (OTHER, ensure_analyzed, load_aspects, load_comments, out_path,  # noqa: E402
                           render_page, resolve_range, table_html)

SKILL = "satisfaction_trend"
BASELINE_DAYS = 28
Z_THRESHOLD = -2.0
MIN_DROP = 0.3
MIN_WEEK_N = 30

TOOL_SPEC = {
    "name": SKILL,
    "description": "满意度变化：每日满意度及 7 日移动平均、异常下跌日期及当天增多的问题、口味/环境/服务分项趋势、"
                   "各问题按周的负面率热力图、满意度变化最大的门店，生成可视化 HTML 报告。",
    "parameters": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
            "end": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
            "days": {"type": "integer", "description": "未给 start 时，取截至 end 的最近 N 天；不填则用全部数据"},
            "min_n": {"type": "integer", "description": "判断异常时当天最少评论数，默认 15"},
        },
    },
}


def _daily(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    d = df.dropna(subset=["satisfaction"]).groupby("date")["satisfaction"].agg(n="count", total="sum", mean="mean")
    d.index = pd.to_datetime(d.index)
    d = d.reindex(pd.date_range(start, end), fill_value=0)
    d.loc[d["n"] == 0, "mean"] = None
    # 7 日移动平均按评论数加权，避免评论很少的日子把曲线拉得忽上忽下
    d["ma7"] = d["total"].rolling(7, min_periods=1).sum() / d["n"].rolling(7, min_periods=1).sum()
    return d


def _anomalies(df: pd.DataFrame, daily: pd.DataFrame, min_n: int) -> list:
    sat = df.dropna(subset=["satisfaction"]).assign(date=lambda x: pd.to_datetime(x["date"]))
    out = []
    for day, row in daily[daily["n"] >= min_n].iterrows():
        base = sat[(sat["date"] < day) & (sat["date"] >= day - pd.Timedelta(days=BASELINE_DAYS))]["satisfaction"]
        if len(base) < 100:
            continue
        z = (row["mean"] - base.mean()) / (base.std() / row["n"] ** 0.5)
        if z <= Z_THRESHOLD and base.mean() - row["mean"] >= MIN_DROP:
            out.append({"date": day.strftime("%Y-%m-%d"), "n": int(row["n"]), "mean": round(row["mean"], 2),
                        "baseline": round(base.mean(), 2), "z": round(z, 1)})
    return out


def _drivers(day: str, neg: pd.DataFrame, n_by_date: pd.Series) -> list:
    """异常当天，各负面问题的出现率（每百条评论）相对前 28 天基线增加了多少。"""
    d = pd.Timestamp(day)
    in_base = (neg["date"] < d) & (neg["date"] >= d - pd.Timedelta(days=BASELINE_DAYS))
    n_day = n_by_date.get(d, 0)
    n_base = n_by_date[(n_by_date.index < d) & (n_by_date.index >= d - pd.Timedelta(days=BASELINE_DAYS))].sum()
    if not n_day or not n_base:
        return []
    today = neg[neg["date"] == d].groupby("tag")["comment_id"].nunique() / n_day * 100
    base = neg[in_base].groupby("tag")["comment_id"].nunique() / n_base * 100
    diff = today.sub(base, fill_value=0).drop(index=OTHER, errors="ignore").sort_values(ascending=False)
    return [{"tag": t, "today": round(today.get(t, 0), 1), "baseline": round(base.get(t, 0), 1), "diff": round(v, 1)}
            for t, v in diff.head(3).items() if v > 0]


def _store_changes(df: pd.DataFrame, end: str, window: int = 30, min_comments: int = 15) -> pd.DataFrame:
    e = pd.Timestamp(end)
    d = df.dropna(subset=["satisfaction"]).assign(date=lambda x: pd.to_datetime(x["date"]))
    recent = d[d["date"] > e - pd.Timedelta(days=window)]
    before = d[(d["date"] <= e - pd.Timedelta(days=window)) & (d["date"] > e - pd.Timedelta(days=2 * window))]
    a = recent.groupby("rest_name")["satisfaction"].agg(["mean", "count"])
    b = before.groupby("rest_name")["satisfaction"].agg(["mean", "count"])
    m = a.join(b, lsuffix="_近期", rsuffix="_之前", how="inner")
    m = m[(m["count_近期"] >= min_comments) & (m["count_之前"] >= min_comments)]
    m["变化"] = m["mean_近期"] - m["mean_之前"]
    return m.sort_values("变化").round(2)


def run(start: str = None, end: str = None, days: int = None, min_n: int = 15, analyze: bool = True) -> dict:
    start, end = resolve_range(start, end, days)
    unanalyzed = ensure_analyzed(start, end, analyze)
    df = load_comments(start, end)
    daily = _daily(df, start, end)
    anomalies = _anomalies(df, daily, min_n)

    neg = load_aspects(start, end, "负面").assign(date=lambda x: pd.to_datetime(x["date"]))
    analyzed = df[df["analyzed"] == 1]
    n_by_date = analyzed.groupby(pd.to_datetime(analyzed["date"]))["comment_id"].count()
    for a in anomalies:
        a["drivers"] = _drivers(a["date"], neg, n_by_date)

    # 图 1：每日满意度 + 7 日均线 + 评论数 + 异常点
    x = [d.strftime("%Y-%m-%d") for d in daily.index]
    count_bar = (Bar()
                 .add_xaxis(x)
                 .add_yaxis("评论数", daily["n"].astype(int).tolist(), yaxis_index=1, color="#d9dee7",
                            label_opts=opts.LabelOpts(is_show=False)))
    # 折线图作为底图（带第二根 y 轴），再叠加评论数柱子
    line = (Line()
            .add_xaxis(x)
            .add_yaxis("每日满意度", [None if pd.isna(v) else round(v, 2) for v in daily["mean"]],
                       is_symbol_show=False, color="#9fb3c8", linestyle_opts=opts.LineStyleOpts(width=1),
                       label_opts=opts.LabelOpts(is_show=False), is_connect_nones=False)
            .add_yaxis("7 日移动平均", daily["ma7"].round(2).tolist(), is_symbol_show=False, is_smooth=True,
                       color="#3a6ea5", linestyle_opts=opts.LineStyleOpts(width=3),
                       label_opts=opts.LabelOpts(is_show=False),
                       markpoint_opts=opts.MarkPointOpts(
                           data=[opts.MarkPointItem(name="异常下跌", coord=[a["date"], a["mean"]], value=a["mean"],
                                                    itemstyle_opts=opts.ItemStyleOpts(color="#d94e5d"))
                                 for a in anomalies], symbol_size=40))
            .extend_axis(yaxis=opts.AxisOpts(name="评论数", position="right",
                                             splitline_opts=opts.SplitLineOpts(is_show=False)))
            .set_global_opts(
                tooltip_opts=opts.TooltipOpts(trigger="axis"),
                yaxis_opts=opts.AxisOpts(name="满意度", min_=1, max_=5),
                datazoom_opts=[opts.DataZoomOpts(type_="slider"), opts.DataZoomOpts(type_="inside")],
                legend_opts=opts.LegendOpts(pos_top="0")))
    trend = line.overlap(count_bar)

    # 图 2：按周的总分与分项评分
    d2 = df.assign(week=pd.to_datetime(df["date"]).dt.to_period("W").dt.start_time)
    weekly = d2.groupby("week")[["satisfaction", "rating_flavor", "rating_env", "rating_service"]].mean()
    week_n = d2.groupby("week")["satisfaction"].count()
    names = {"satisfaction": "满意度（总）", "rating_flavor": "口味评分", "rating_env": "环境评分",
             "rating_service": "服务评分"}
    sub_line = Line().add_xaxis([w.strftime("%Y-%m-%d") for w in weekly.index])
    for col, name in names.items():
        sub_line.add_yaxis(name, weekly[col].round(2).tolist(), is_smooth=True, is_symbol_show=False,
                           linestyle_opts=opts.LineStyleOpts(width=3 if col == "satisfaction" else 2),
                           label_opts=opts.LabelOpts(is_show=False))
    sub_line.set_global_opts(tooltip_opts=opts.TooltipOpts(trigger="axis"),
                             yaxis_opts=opts.AxisOpts(min_=2, max_=4.5),
                             legend_opts=opts.LegendOpts(pos_top="0"))

    # 图 3：各问题按周的负面率热力图（每百条评论）
    neg_w = neg[neg["tag"] != OTHER].assign(week=lambda x: x["date"].dt.to_period("W").dt.start_time)
    n_week = analyzed.assign(week=pd.to_datetime(analyzed["date"]).dt.to_period("W").dt.start_time) \
        .groupby("week")["comment_id"].count()
    rate = (neg_w.groupby(["tag", "week"])["comment_id"].nunique().unstack(fill_value=0)
            .reindex(columns=n_week.index, fill_value=0).div(n_week, axis=1) * 100)
    rate = rate.loc[rate.mean(axis=1).sort_values(ascending=False).index]
    weeks = [w.strftime("%Y-%m-%d") for w in rate.columns]
    heat = [[j, i, round(rate.iat[i, j], 1)] for i in range(len(rate)) for j in range(len(weeks))]
    heatmap = (HeatMap()
               .add_xaxis(weeks)
               .add_yaxis("负面率", rate.index.tolist(), heat, label_opts=opts.LabelOpts(is_show=False))
               .set_global_opts(visualmap_opts=opts.VisualMapOpts(min_=0, max_=float(rate.max().max()), is_calculable=True,
                                                                  range_color=["#f7f7f7", "#f4b183", "#d94e5d"],
                                                                  orient="horizontal", pos_left="center", pos_bottom="0"),
                                tooltip_opts=opts.TooltipOpts(formatter="{c}"),
                                legend_opts=opts.LegendOpts(is_show=False)))

    stores = _store_changes(df, end)

    # 要点
    last30 = daily.iloc[-30:]
    prev30 = daily.iloc[-60:-30]
    avg = lambda d: d["total"].sum() / max(d["n"].sum(), 1)  # noqa: E731
    insights = [f"{start} ~ {end} 共 {int(daily['n'].sum())} 条有满意度的评论，整体平均 {avg(daily):.2f} 分"
                + (f"（{unanalyzed} 条未分析）" if unanalyzed else "")]
    if len(prev30) == 30:
        insights.append(f"最近 30 天平均 {avg(last30):.2f} 分，之前 30 天 {avg(prev30):.2f} 分，"
                        f"变化 {avg(last30) - avg(prev30):+.2f}")
    full = weekly.loc[week_n[week_n >= MIN_WEEK_N].index, "satisfaction"]  # 评论太少的周不参与排名
    if len(full):
        best, worst = full.idxmax(), full.idxmin()
        insights.append(f"满意度最高的一周 {best:%Y-%m-%d} 起（{full[best]:.2f}），"
                        f"最低的一周 {worst:%Y-%m-%d} 起（{full[worst]:.2f}）；不含评论少于 {MIN_WEEK_N} 条的周")
    insights.append(f"检测到 {len(anomalies)} 个异常下跌日" + (
        "：" + "；".join(f"{a['date']} {a['mean']}（基线 {a['baseline']}）"
                        + ("，增多：" + "、".join(d["tag"] for d in a["drivers"]) if a["drivers"] else "")
                        for a in anomalies[-3:]) if anomalies else ""))
    if len(stores):
        s = stores.iloc[0]
        insights.append(f"满意度下滑最多的门店：{stores.index[0]}（{s['mean_之前']} → {s['mean_近期']}）")

    anomaly_rows = pd.DataFrame([{"日期": a["date"], "评论数": a["n"], "当天满意度": a["mean"], "前 28 天基线": a["baseline"],
                                  "z": a["z"], "当天增多的问题（每百条：当天 / 基线）":
                                      "、".join(f"{d['tag']} {d['today']}/{d['baseline']}" for d in a["drivers"])}
                                 for a in anomalies])
    store_rows = stores.head(8).reset_index().rename(columns={
        "rest_name": "门店", "mean_近期": "近 30 天满意度", "count_近期": "近 30 天评论数",
        "mean_之前": "之前 30 天满意度", "count_之前": "之前 30 天评论数"})

    sections = [
        {"title": "每日满意度", "chart": trend, "height": 460,
         "note": "灰线为每日均值，蓝线为按评论数加权的 7 日移动平均，红色标记为异常下跌日；底部可拖动缩放"},
        {"title": "异常下跌日", "html": table_html(anomaly_rows) if len(anomaly_rows) else "<p>没有检测到异常下跌</p>",
         "note": f"当天评论数 ≥ {min_n}，且显著低于前 {BASELINE_DAYS} 天基线（z ≤ {Z_THRESHOLD}，且至少低 {MIN_DROP} 分）"},
        {"title": "满意度与分项评分（按周）", "chart": sub_line, "height": 400,
         "note": "分项评分只有约 72% 的评论有，仅供参考"},
        {"title": "各问题负面率（按周，每百条评论）", "chart": heatmap, "height": 560},
        {"title": "满意度下滑最多的门店（近 30 天 vs 之前 30 天）",
         "html": table_html(store_rows) if len(store_rows) else "<p>门店评论数不足，无法比较</p>",
         "note": "两个时段都至少有 15 条评论的门店"},
    ]
    path = render_page(out_path(SKILL, start, end), "满意度变化", f"{start} ~ {end}", insights, sections)
    return {
        "skill": SKILL,
        "range": [start, end],
        "insights": insights,
        "data": {
            "weekly": [{"week": w.strftime("%Y-%m-%d"), **{k: (None if pd.isna(v) else round(v, 2)) for k, v in r.items()}}
                       for w, r in weekly.iterrows()],
            "anomalies": anomalies,
            "store_changes": store_rows.head(5).to_dict("records"),
        },
        "html": str(path),
    }


def main():
    p = argparse.ArgumentParser(description=TOOL_SPEC["description"])
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--days", type=int)
    p.add_argument("--min-n", type=int, default=15)
    p.add_argument("--no-analyze", action="store_true", help="不调用大模型补分析未处理的评论")
    a = p.parse_args()
    result = run(a.start, a.end, a.days, a.min_n, analyze=not a.no_analyze)
    print("\n".join(result["insights"]))
    print(f"\n报告：{result['html']}")


if __name__ == "__main__":
    main()
