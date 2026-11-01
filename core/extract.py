"""批量调用大模型，为每条评论提取「方面标签 + 正负面 + 原文片段」、满意度和摘要。"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import taxonomy
from core.config import MODEL_EXTRACT
from core.db import connect
from core.llm import chat_json

SYSTEM_TEMPLATE = """你是餐饮评论分析助手。请逐条分析用户评论，输出 json。

可用的方面标签（只能从中选择，都不合适时用「其他」）：
{tags}

对每条评论输出：
- aspects：评论提到的所有方面，每项包含 tag（标签名）、polarity（只能是「正面」或「负面」）、snippet（原文片段，不超过 30 字）。
  同一条评论可以有多个方面；同一个标签既有夸又有贬时，拆成两项。没有提到任何具体方面时输出空列表。
- score：整体满意度，1–5 的整数（1 非常不满意，3 一般，5 非常满意）
- summary：一句话摘要，不超过 25 字

格式示例：
{{"results": [{{"id": "C0000001", "aspects": [{{"tag": "口味", "polarity": "正面", "snippet": "虾面超鲜"}},
{{"tag": "服务态度", "polarity": "负面", "snippet": "服务员爱理不理"}}], "score": 4, "summary": "面很鲜但服务冷淡"}}]}}

必须为输入的每一条评论都输出一项，id 与输入保持一致。"""


def _system_prompt() -> tuple:
    tags = taxonomy.load()
    lines = "\n".join(f"- {t['name']}：{t['definition']}" for t in tags)
    return SYSTEM_TEMPLATE.format(tags=lines), {t["name"] for t in tags}


def _analyze_batch(system: str, batch: list) -> list:
    user = "\n".join(f"[{r['comment_id']}] {r['content'][:800]}" for r in batch)
    return chat_json(system, user).get("results", [])


def _clean(item: dict, valid_tags: set) -> tuple:
    aspects = []
    for a in item.get("aspects") or []:
        tag = a.get("tag") if a.get("tag") in valid_tags else taxonomy.OTHER
        if a.get("polarity") in ("正面", "负面"):
            aspects.append((tag, a["polarity"], (a.get("snippet") or "")[:60]))
    try:
        score = min(5, max(1, int(item.get("score"))))
    except (TypeError, ValueError):
        score = None
    return aspects, score, (item.get("summary") or "")[:60]


def pending(conn, start: str = None, end: str = None) -> list:
    sql = ("SELECT comment_id, content FROM comments WHERE comment_id NOT IN "
           "(SELECT comment_id FROM analysis)")
    params = []
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    return [dict(r) for r in conn.execute(sql + " ORDER BY time", params)]


def run(start: str = None, end: str = None, batch_size: int = 20, workers: int = 8) -> dict:
    system, valid_tags = _system_prompt()
    conn = connect()
    rows = pending(conn, start, end)
    if not rows:
        conn.close()
        return {"pending": 0, "done": 0, "failed": 0}

    batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    done = failed = 0
    print(f"待分析 {len(rows)} 条，分 {len(batches)} 批，并发 {workers}")

    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(_analyze_batch, system, b): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                results = {r.get("id"): r for r in fut.result()}
            except Exception as e:
                failed += len(batch)
                print(f"  一批失败（{len(batch)} 条），下次运行会重试：{e}")
                continue
            # 模型漏掉的评论不写入 analysis，下次运行会重新分析
            for row in batch:
                item = results.get(row["comment_id"])
                if item is None:
                    failed += 1
                    continue
                aspects, score, summary = _clean(item, valid_tags)
                conn.execute("INSERT INTO analysis (comment_id, llm_score, summary, model) VALUES (?,?,?,?)",
                             (row["comment_id"], score, summary, MODEL_EXTRACT))
                conn.executemany("INSERT INTO aspects (comment_id, tag, polarity, snippet) VALUES (?,?,?,?)",
                                 [(row["comment_id"], *a) for a in aspects])
                done += 1
            conn.commit()
            print(f"  进度 {done + failed}/{len(rows)}")

    conn.close()
    return {"pending": len(rows), "done": done, "failed": failed}
