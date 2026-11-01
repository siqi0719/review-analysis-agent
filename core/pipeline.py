"""
一次完整的轮询：扫描监控目录 → 新文件入库 → 大模型提取 → 新日期生成日报 → 重新生成 3 个 Skill 报告。
后台 watcher、网页上的「立即拉取」按钮和对话 Agent 都调用 poll_once()，用锁文件保证同一时间只跑一个。
"""
import json
import os
import time
import traceback
from datetime import datetime

import pandas as pd

from core.config import DATA_DIR, WATCH_DIR, WATCH_PATTERN
from core.db import connect
from core.ingest import ingest_file

LOCK = DATA_DIR / "poll.lock"
STALE_SECONDS = 30 * 60  # 锁文件超过 30 分钟视为上次异常退出留下的


def _acquire() -> bool:
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime > STALE_SECONDS:
        LOCK.unlink(missing_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _changed_files(conn) -> list:
    seen = {r["name"]: (r["size"], r["mtime"]) for r in conn.execute("SELECT name, size, mtime FROM ingested_files")}
    out = []
    for f in sorted(WATCH_DIR.glob(WATCH_PATTERN)):
        st = f.stat()
        if seen.get(f.name) != (st.st_size, st.st_mtime):
            out.append(f)
    return out


def _log(conn, started: str, trigger: str, **kw) -> dict:
    row = {"started_at": started, "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "trigger": trigger,
           "new_files": 0, "new_comments": 0, "analyzed": 0, "reports": [], "status": "ok", "message": ""} | kw
    conn.execute(
        """INSERT INTO poll_log (started_at, finished_at, trigger, new_files, new_comments, analyzed, reports,
                                 status, message) VALUES (?,?,?,?,?,?,?,?,?)""",
        (row["started_at"], row["finished_at"], trigger, row["new_files"], row["new_comments"], row["analyzed"],
         json.dumps(row["reports"], ensure_ascii=False), row["status"], row["message"]))
    conn.commit()
    return row


def _refresh_skills() -> list:
    from skills.issue_extract.run import run as issue_extract
    from skills.issue_topology.run import run as issue_topology
    from skills.satisfaction_trend.run import run as satisfaction_trend
    paths = []
    for name, fn in [("问题提取", issue_extract), ("问题拓扑", issue_topology), ("满意度变化", satisfaction_trend)]:
        try:
            paths.append(fn(analyze=False)["html"])
        except Exception as e:  # 单个 Skill 失败不影响整体轮询
            print(f"  {name}报告生成失败：{e}")
    return paths


def poll_once(trigger: str = "schedule", log=print) -> dict:
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = connect()
    if not _acquire():
        return _log(conn, started, trigger, status="busy", message="另一次轮询正在进行，本次跳过")
    try:
        WATCH_DIR.mkdir(parents=True, exist_ok=True)
        files = _changed_files(conn)
        if not files:
            log(f"[{started}] 没有新文件（监控目录：{WATCH_DIR}）")
            return _log(conn, started, trigger, status="no_change", message="没有新文件")

        new_comments, dates = 0, set()
        for f in files:
            n = ingest_file(f, conn)
            st = f.stat()
            conn.execute("INSERT OR REPLACE INTO ingested_files (name, size, mtime, new_comments) VALUES (?,?,?,?)",
                         (f.name, st.st_size, st.st_mtime, n))
            conn.commit()
            new_comments += n
            if n:
                dates.update(pd.read_sql("SELECT DISTINCT date FROM comments WHERE source_file = ?",
                                         conn, params=(f.name,))["date"])
            log(f"  {f.name}：新增 {n} 条")

        analyzed, reports = 0, []
        if dates:
            from core.extract import run as extract
            from core.report import generate
            result = extract(min(dates), max(dates))
            analyzed = result["done"]
            log(f"  大模型提取：{result}")
            for day in sorted(dates):
                reports.append(str(generate(day)["markdown"]))
                log(f"  日报：{day}")
            reports += _refresh_skills()
            log("  Skill 报告已更新")

        msg = f"{len(files)} 个文件，新增 {new_comments} 条评论" + (f"，日期 {', '.join(sorted(dates))}" if dates else "")
        log(f"[{started}] 完成：{msg}")
        return _log(conn, started, trigger, new_files=len(files), new_comments=new_comments,
                    analyzed=analyzed, reports=reports, message=msg)
    except Exception as e:
        log(traceback.format_exc())
        return _log(conn, started, trigger, status="error", message=str(e))
    finally:
        LOCK.unlink(missing_ok=True)
        conn.close()


def last_polls(n: int = 10) -> list:
    conn = connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM poll_log ORDER BY id DESC LIMIT ?", (n,))]
    conn.close()
    return rows
