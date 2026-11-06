# -*- coding: utf-8 -*-
"""全局配置模块。

设计原则：
1. 所有可变参数集中在此，业务代码里不出现魔法值；
2. 全部支持 .env / 环境变量覆盖，换供应商只改 .env，不改代码；
3. 与阶段 1 的 PROVIDER_PRESETS 保持一致，保证三个阶段配置方式统一。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/ 的上一级），所有相对路径都从这里出发
BASE_DIR = Path(__file__).resolve().parent.parent

# 若存在 .env 则加载（不存在则全部走默认值 + 降级模式，保证开箱可跑）
load_dotenv(BASE_DIR / ".env")

# ----------------------------------------------------------------------------
# 大模型 / Embedding 供应商预设（统一 OpenAI 兼容协议）
# 说明：DeepSeek 未提供 embeddings 接口，因此 Embedding 供应商可单独配置。
# ----------------------------------------------------------------------------
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek":  {"llm_base_url": "https://api.deepseek.com/v1",        "llm_model": "deepseek-chat"},
    "moonshot":  {"llm_base_url": "https://api.moonshot.cn/v1",          "llm_model": "moonshot-v1-8k"},
    "dashscope": {"llm_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "llm_model": "qwen-plus"},
    "zhipu":     {"llm_base_url": "https://open.bigmodel.cn/api/paas/v4", "llm_model": "glm-4-flash"},
    "openai":    {"llm_base_url": "https://api.openai.com/v1",           "llm_model": "gpt-4o-mini"},
}

EMBED_PRESETS: dict[str, dict[str, str]] = {
    # dashscope 的 text-embedding-v3 是目前国产里性价比较高的选择
    "dashscope": {"embed_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                  "embed_model": "text-embedding-v3", "embed_dim": "1024"},
    "zhipu":     {"embed_base_url": "https://open.bigmodel.cn/api/paas/v4",
                  "embed_model": "embedding-2", "embed_dim": "1024"},
    "openai":    {"embed_base_url": "https://api.openai.com/v1",
                  "embed_model": "text-embedding-3-small", "embed_dim": "1536"},
}


class Config:
    """集中配置。读取顺序：环境变量 / .env > 供应商预设 > 默认值。"""

    # ---------------- 路径 ----------------
    DATA_DIR = BASE_DIR / "data"
    DB_PATH = DATA_DIR / "chat.db"          # SQLite 数据库（单文件）
    SEED_DOC = DATA_DIR / "报销制度.md"       # 首次启动自动入库的种子语料
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- 鉴权（JWT）----------------
    JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production-请务必修改")
    JWT_ALGORITHM = "HS256"
    TOKEN_EXPIRE_HOURS = int(os.getenv("TOKEN_EXPIRE_HOURS", "12"))   # 登录有效期
    COOKIE_NAME = "bx_token"                                             # JWT 存 HttpOnly Cookie

    # ---------------- 大模型（LLM）----------------
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL") or PROVIDER_PRESETS[LLM_PROVIDER]["llm_base_url"]
    LLM_MODEL = os.getenv("LLM_MODEL") or PROVIDER_PRESETS[LLM_PROVIDER]["llm_model"]
    LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))   # 制度问答要求稳定，温度调低
    LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1024"))
    LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "60"))                # 单次生成超时（秒）

    # ---------------- Embedding ----------------
    EMBED_PROVIDER = os.getenv("EMBED_PROVIDER", "")   # 留空表示不配置，自动降级为本地哈希向量
    EMBED_API_KEY = os.getenv("EMBED_API_KEY", "")
    EMBED_BASE_URL = os.getenv("EMBED_BASE_URL") or EMBED_PRESETS.get(EMBED_PROVIDER, {}).get("embed_base_url", "")
    EMBED_MODEL = os.getenv("EMBED_MODEL") or EMBED_PRESETS.get(EMBED_PROVIDER, {}).get("embed_model", "")
    EMBED_DIM = int(os.getenv("EMBED_DIM") or EMBED_PRESETS.get(EMBED_PROVIDER, {}).get("embed_dim", "512"))

    # ---------------- 检索与切片 ----------------
    CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "500"))     # 切片目标字数（沿用阶段 1 的 500）
    CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "80"))
    TOP_K = int(os.getenv("TOP_K", "3"))                 # 融合后取 Top3 拼入提示词
    RRF_K = 60                                           # RRF 平滑系数，经验值
    VECTOR_WEIGHT = 1.0
    BM25_WEIGHT = 1.0

    # ---------------- 会话 ----------------
    HISTORY_ROUNDS = 6        # 每次请求携带最近 6 轮（12 条）历史作为上下文
    HISTORY_CHAR_LIMIT = 800  # 单条历史超长时截断，防止撑爆上下文窗口
    MAX_QUESTION_LEN = 500    # 员工单次提问的最大长度

    # ---------------- 上线加固（阶段 7 新增）----------------
    # 限流：每个用户每分钟最多发起的问答次数（滑动窗口，进程内实现）
    CHAT_RATE_LIMIT_PER_MIN = int(os.getenv("CHAT_RATE_LIMIT_PER_MIN", "60"))
    # 内容安全：检测到提示词注入时直接拒绝，问题不进 RAG / 不进 LLM
    INJECTION_REFUSAL = (
        "⚠️ 您的消息中包含疑似指令注入的内容，已按安全策略拦截。"
        "本助手只回答公司报销制度相关问题，请调整措辞后重新提问。"
    )
    # 日志保留：超过该天数的 tool_calls / messages 日志由 scripts/purge_logs.py 清理
    LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "90"))
    # 数据备份：scripts/backup_db.py 输出目录与保留份数
    BACKUP_DIR = BASE_DIR / "backups"
    BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "7"))

    # ---------------- 上传限制 ----------------
    UPLOAD_MAX_BYTES = 10 * 1024 * 1024                   # 单文件最大 10MB
    UPLOAD_SUFFIXES = {".md", ".markdown", ".pdf"}        # 只支持 markdown 与 pdf

    # ---------------- 默认系统提示词（首次启动写入数据库，之后以库里的为准）----------------
    DEFAULT_PROMPT = (
        "你是公司报销制度助手，只根据提供的制度条文回答，不确定时明确说'制度中未找到'，"
        "回答末尾附引用来源，不得编造报销标准。"
    )

    # ---------------- 便捷判断 ----------------
    @classmethod
    def llm_ready(cls) -> bool:
        """是否配置了大模型 Key（没配则问答走降级模式：直接展示检索原文）。"""
        return bool(cls.LLM_API_KEY)

    @classmethod
    def embed_ready(cls) -> bool:
        """是否配置了 Embedding Key（没配则用本地哈希向量，只保字面相似）。"""
        return bool(cls.EMBED_PROVIDER and cls.EMBED_API_KEY and cls.EMBED_BASE_URL)
