"""
评论分析 Agent 网页：自动看板 + 对话分析。

  streamlit run app.py

自动轮询由单独的 watcher.py 进程负责；这里显示轮询状态，并提供「立即拉取」按钮。
"""
import json
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from core.config import REPORT_DIR, WATCH_DIR
from core.db import connect

st.set_page_config(page_title="评论分析 Agent", page_icon="📊", layout="wide")


def show_html(path: str, height: int = 900):
    # 报告是独立 HTML，用 iframe 嵌入；iframe 尺寸变化时报告内的图表会随 resize 事件重新适配
    p = Path(path)
    if p.exists():
        components.html(p.read_text(encoding="utf-8"), height=height, scrolling=True)
    else:
        st.warning(f"找不到报告：{path}")


def latest(pattern: str):
    files = sorted((REPORT_DIR / "skills").glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


@st.cache_data(ttl=60)
def overview() -> dict:
    conn = connect()
    o = dict(conn.execute(
        """SELECT count(*) n, min(date) lo, max(date) hi, round(avg(satisfaction), 2) sat,
                  sum(comment_id NOT IN (SELECT comment_id FROM analysis)) pending
           FROM v_comments""").fetchone())
    o["recent"] = pd.read_sql(
        """SELECT date AS 日期, count(*) AS 评论数, round(avg(satisfaction), 2) AS 满意度
           FROM v_comments GROUP BY date ORDER BY date DESC LIMIT 14""", conn)
    conn.close()
    return o


# ---------------- 侧边栏：轮询状态 ----------------
with st.sidebar:
    st.header("自动轮询")
    st.caption(f"监控目录：{WATCH_DIR}")
    from core.pipeline import last_polls, poll_once
    polls = last_polls(8)
    if polls:
        p = polls[0]
        st.metric("上次轮询", p["finished_at"][5:16], help=p["message"])
        st.caption(f"状态：{p['status']} · 触发：{p['trigger']} · {p['message']}")
    else:
        st.info("还没有轮询记录。请在终端运行 python watcher.py")
    if st.button("立即拉取", use_container_width=True):
        with st.status("正在检查新文件…", expanded=True) as s:
            r = poll_once("manual", log=st.write)
            s.update(label=f"完成：{r['message']}", state="complete" if r["status"] != "error" else "error")
        st.cache_data.clear()
    with st.expander("轮询记录"):
        if polls:
            st.dataframe(pd.DataFrame(polls)[["finished_at", "trigger", "status", "new_comments", "message"]],
                         hide_index=True, use_container_width=True)

tab_board, tab_chat = st.tabs(["📊 自动看板", "💬 对话分析"])

# ---------------- 看板 ----------------
with tab_board:
    o = overview()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("评论总数", f"{o['n']:,}")
    c2.metric("最新数据日期", o["hi"], help=f"数据范围 {o['lo']} ~ {o['hi']}")
    c3.metric("整体满意度", o["sat"])
    c4.metric("待分析", o["pending"])

    left, right = st.columns([1, 2])
    with left:
        st.subheader("最近 14 天")
        st.dataframe(o["recent"], hide_index=True, use_container_width=True)
    with right:
        st.subheader("最新日报")
        reports = sorted(REPORT_DIR.glob("daily_*.json"), reverse=True)
        if reports:
            day = st.selectbox("日期", [f.stem.replace("daily_", "") for f in reports], label_visibility="collapsed")
            md = REPORT_DIR / f"daily_{day}.md"
            if md.exists():
                st.markdown(md.read_text(encoding="utf-8"))
            else:
                st.json(json.loads((REPORT_DIR / f"daily_{day}.json").read_text(encoding="utf-8")))
        else:
            st.info("还没有日报")

    st.divider()
    st.subheader("分析报告")
    sub1, sub2, sub3 = st.tabs(["问题提取", "问题拓扑", "满意度变化"])
    for sub, pattern in [(sub1, "issue_extract_*.html"), (sub2, "issue_topology_*.html"),
                         (sub3, "satisfaction_trend_*.html")]:
        with sub:
            f = latest(pattern)
            if f:
                st.caption(f"{f.name} · 更新于 {pd.Timestamp(f.stat().st_mtime, unit='s', tz='Asia/Shanghai'):%m-%d %H:%M}")
                show_html(str(f), 1500)
            else:
                st.info("还没有生成，等下一次轮询，或在对话里让我分析")

# ---------------- 对话 ----------------
with tab_chat:
    if "history" not in st.session_state:
        st.session_state.history = []  # [{"role", "content", "html": [...], "steps": [...]}]

    st.caption("例如：最近 30 天用户主要抱怨什么？/ 排队和服务问题有关联吗？/ 7 月 2 日满意度为什么下降？")
    for m in st.session_state.history:
        with st.chat_message(m["role"]):
            if m.get("steps"):
                with st.expander(f"调用了 {len(m['steps'])} 个工具"):
                    for s in m["steps"]:
                        st.code(f"{s['tool']}({json.dumps(s['args'], ensure_ascii=False)})", language="python")
            st.markdown(m["content"])
            for h in m.get("html", []):
                with st.expander("查看可视化报告", expanded=True):
                    show_html(h, 1100)

    if prompt := st.chat_input("问我关于评论数据的问题"):
        st.session_state.history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            from core.agent import chat
            with st.status("思考中…", expanded=False) as status:
                prior = [{"role": m["role"], "content": m["content"]} for m in st.session_state.history[:-1]]
                result = chat(prior, prompt, on_step=lambda s: status.write(f"调用 {s['tool']}"))
                status.update(label=f"完成（调用了 {len(result['steps'])} 个工具）", state="complete")
            st.markdown(result["answer"])
            for h in result["html"]:
                with st.expander("查看可视化报告", expanded=True):
                    show_html(h, 1100)
        st.session_state.history.append({"role": "assistant", "content": result["answer"],
                                         "html": result["html"], "steps": result["steps"]})
