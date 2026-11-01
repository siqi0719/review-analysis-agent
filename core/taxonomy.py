"""问题标签体系：先让大模型从样本中归纳草稿，人工确认后才作为正式标签使用。"""
import json
import random

from core.config import MODEL_CHAT, TAXONOMY_DRAFT, TAXONOMY_PATH
from core.db import connect
from core.llm import chat_json

OTHER = "其他"

SYSTEM = """你是餐饮行业的用户体验分析专家。请阅读一批大众点评用户评论，归纳出一套「评论方面标签体系」，
用于后续给每条评论打标签（同一个标签既可以是正面评价，也可以是负面评价）。

要求：
1. 输出 15–20 个标签，标签名 2–6 个汉字，例如「口味」「上菜速度」「服务态度」
2. 标签之间不要重叠，粒度要一致；不要出现「其他」，系统会自动补上
3. 每个标签给出一句话定义，以及 2 条从评论里摘出的原文片段作为例子
4. 按这批评论中出现的频率从高到低排列

只输出 json，格式如下：
{"tags": [{"name": "口味", "definition": "菜品的味道、口感是否好吃", "examples": ["味道很正宗", "太咸了"]}]}"""


def induce(sample_size: int = 300, seed: int = 42) -> dict:
    conn = connect()
    contents = [r["content"] for r in conn.execute("SELECT content FROM comments ORDER BY comment_id")]
    conn.close()
    if not contents:
        raise SystemExit("数据库里还没有评论，请先运行 python cli.py ingest")

    sample = random.Random(seed).sample(contents, min(sample_size, len(contents)))
    text = "\n".join(f"{i + 1}. {c[:300]}" for i, c in enumerate(sample))
    result = chat_json(SYSTEM, f"以下是 {len(sample)} 条评论：\n{text}", model=MODEL_CHAT)

    TAXONOMY_DRAFT.parent.mkdir(parents=True, exist_ok=True)
    TAXONOMY_DRAFT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def load() -> list:
    """返回已确认的标签列表（末尾自动补「其他」）。"""
    if not TAXONOMY_PATH.exists():
        raise SystemExit(f"标签体系还没有确认：请检查 {TAXONOMY_DRAFT}，确认后另存为 {TAXONOMY_PATH}")
    tags = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))["tags"]
    return tags + [{"name": OTHER, "definition": "不属于以上任何一类"}]
