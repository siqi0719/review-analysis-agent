"""项目路径与环境变量配置。"""
import ctypes
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
DB_PATH = DATA_DIR / "agent.db"

CONFIG_DIR = ROOT / "config"
TAXONOMY_DRAFT = CONFIG_DIR / "taxonomy_draft.json"  # 大模型归纳的草稿，等待人工确认
TAXONOMY_PATH = CONFIG_DIR / "taxonomy.json"         # 确认后的正式标签体系

REPORT_DIR = ROOT / "reports"


def desktop_dir() -> Path:
    # 通过系统接口取桌面路径，兼容被 OneDrive 接管的桌面
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)  # CSIDL_DESKTOPDIRECTORY
    return Path(buf.value)


WATCH_DIR = Path(os.getenv("WATCH_DIR") or desktop_dir() / "评论数据")  # 轮询监控的评论文件目录
WATCH_PATTERN = "评论_*.xlsx"
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "60"))

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL_EXTRACT = os.getenv("LLM_MODEL_EXTRACT", "deepseek-flash")
MODEL_CHAT = os.getenv("LLM_MODEL_CHAT", "deepseek-v4-pro")
