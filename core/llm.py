"""DeepSeek 调用封装（OpenAI 兼容接口），统一要求返回 JSON。"""
import json
import time

from openai import AuthenticationError, OpenAI

from core.config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, MODEL_EXTRACT

if not DEEPSEEK_API_KEY:
    raise SystemExit("没有读到 DEEPSEEK_API_KEY，请检查 D:\\Agent\\.env")

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


def chat_json(system: str, user: str, model: str = MODEL_EXTRACT,
              max_tokens: int = 8000, retries: int = 3) -> dict:
    """调用模型并解析 JSON。DeepSeek 偶尔返回空内容或截断的 JSON，失败时重试。"""
    last_err = None
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                temperature=0.2,
                extra_body={"thinking": {"type": "disabled"}},  # 批量任务关闭思考模式，更快更省
            )
            content = resp.choices[0].message.content
            if not content or not content.strip():
                raise ValueError("模型返回了空内容")
            return json.loads(content)
        except AuthenticationError:
            raise SystemExit("DeepSeek 拒绝了 API Key（401），请检查 .env 里的 DEEPSEEK_API_KEY")
        except Exception as e:  # 网络错误、限流、JSON 解析失败都重试
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"调用 {model} 失败（重试 {retries} 次）：{last_err}")
