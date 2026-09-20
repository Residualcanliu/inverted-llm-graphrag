"""配置。所有可调项走环境变量，默认值对齐 .env.example。"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _get(key: str, default: str) -> str:
    return os.getenv(key, default)


# ---------- Neo4j ----------
NEO4J_URI = _get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = _get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = _get("NEO4J_PASSWORD", "please_change_me")
# 注意：没有独立的只读账号。社区版不支持 RBAC，只读靠 driver 的
# READ_ACCESS 模式（服务端强制），见 app/graph/client.py。

# ---------- 模型 ----------


def _normalize_url(v: str) -> str:
    """补全 http:// 前缀，并把 0.0.0.0 换成 127.0.0.1。

    踩过的坑：环境变量名原本想用 OLLAMA_HOST，但它和 Ollama 服务端自己的
    变量重名 —— Ollama 把它设成监听地址 `0.0.0.0:11434`，而 load_dotenv
    默认不覆盖已存在的环境变量，于是我们拿到的是监听地址：既缺 http://
    前缀，0.0.0.0 也不是一个合法的客户端目标。
    改用 OLLAMA_BASE_URL 避开重名，这里再做一层防御性归一。
    """
    v = v.strip().rstrip("/")
    if not v:
        return "http://127.0.0.1:11434"
    if not v.startswith(("http://", "https://")):
        v = "http://" + v
    return v.replace("//0.0.0.0:", "//127.0.0.1:")


OLLAMA_BASE_URL = _normalize_url(_get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
GEN_MODEL = _get("GEN_MODEL", "qwen2.5-coder:14b")
JUDGE_MODEL = _get("JUDGE_MODEL", "deepseek-r1:14b")
EMBED_MODEL = _get("EMBED_MODEL", "bge-m3")

# ---------- 生成参数 ----------
GEN_TEMPERATURE = float(_get("GEN_TEMPERATURE", "0.1"))
GEN_NUM_PREDICT = int(_get("GEN_NUM_PREDICT", "512"))
# 调研结论：自修复前 2 次拿走绝大部分收益，第 3 次后基本不收敛
MAX_REPAIR_ROUNDS = int(_get("MAX_REPAIR_ROUNDS", "2"))

# ---------- 传统 RAG 基线 ----------
CHUNK_SIZE = int(_get("CHUNK_SIZE", "400"))
CHUNK_OVERLAP = int(_get("CHUNK_OVERLAP", "50"))
TOP_K = int(_get("TOP_K", "5"))

# ---------- 路径 ----------
DATA_DIR = ROOT / _get("DATA_DIR", "data")
REPORT_DIR = ROOT / _get("REPORT_DIR", "reports")

# 阈值：低于这个分数的 chunk 视为不相关
MIN_CHUNK_SCORE = 0.25
