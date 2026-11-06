#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
==============================================================================
 智家速修 · AI 售后工单系统  —— 阶段 1 最小原型（RAG 检索增强问答）
 文件：ai_aftersales_rag.py
==============================================================================

【这个脚本做什么】
    1. 读取本地一份售后知识库 Markdown 文件（本项目为 data/售后知识库.md）
    2. 按 500 字切片（带重叠，避免语义被切断）
    3. 调用可配置的 Embedding 供应商，把切片向量化，存入 FAISS 向量库
    4. 命令行输入问题 → BM25 关键词检索 + 向量语义检索 混合召回 Top3
    5. 把召回片段拼进提示词 → 调用可配置的 LLM 供应商生成带引用的回答

【为什么不涉及 Web】
    阶段 1 只验证"检索 + 生成"这条链路是否走得通、召回准不准、提示词好不好。
    等这条链路稳定后，阶段 2 直接把这些函数搬进 FastAPI 服务层即可，无需重写。

------------------------------------------------------------------------------
【★ 核心模块标记说明】
    ★CORE-A  …… 本文件内会被后续阶段直接复用的核心模块
    ★CORE-B  …… 接口会被复用，但实现需要替换（如 FAISS → pgvector）
    ★ONCE    …… 阶段 1 专用的一次性代码，后续会被删除或替换
------------------------------------------------------------------------------

【模块清单与复用去向】

  编号      模块                          阶段 1 实现          阶段 2/3 去向
  -------  --------------------------    ------------------  --------------------------
  CORE-1   配置中心 Settings              dataclass + .env     → app/core/config.py
  CORE-2   文档加载与切片                 500 字 + 标题归属      → services/knowledge/splitter.py
  CORE-3   Embedding 客户端（可换供应商）  OpenAI 兼容协议        → services/llm/base.py + factory.py
  CORE-4   向量库 FAISS（含持久化）        单机文件索引           → 替换为 Postgres + pgvector（接口不变）
  CORE-5   BM25 关键词检索                rank_bm25 + jieba    → 与 pgvector 全文检索混合
  CORE-6   混合检索融合（RRF）            倒序排名融合           → services/retrieval_service.py
  CORE-7   提示词组装                     模板 + 引用编号         → agent/prompts/*.md
  CORE-8   LLM 调用函数（可换供应商）      独立函数 llm_chat()   → services/llm/*，被 AgentLoop 调用
  CORE-9   RAG 编排 Pipeline              ask()                → services/ + agent/loop.py
  ONCE-1   命令行交互主循环                while input()        → 替换为 HTTP 接口，删除

------------------------------------------------------------------------------

【运行方式】
    # 1) 安装依赖
    pip install -r requirements.txt

    # 2) 配置大模型（复制 .env.example 为 .env 并填入 Key）
    #    没有 Key 也能跑：脚本会自动进入"本地降级模式"，
    #    用哈希向量替代 Embedding、用检索片段直接拼接替代 LLM 生成，
    #    可以完整演示检索链路（作业演示建议先用这个模式）。

    # 3) 交互式提问
    python ai_aftersales_rag.py

    # 4) 单条提问 / 强制重建索引 / 强制离线
    python ai_aftersales_rag.py --question "智能门锁指纹识别无响应怎么办"
    python ai_aftersales_rag.py --rebuild
    python ai_aftersales_rag.py --offline

==============================================================================
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import pickle
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

# 第三方依赖：缺失时给出友好的中文提示，而不是抛一堆看不懂的堆栈
try:
    import faiss  # 向量检索库（Meta 开源），阶段 2 会换成 pgvector
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 faiss-cpu，请先执行：pip install -r requirements.txt")

try:
    import jieba  # 中文分词，BM25 检索前必须分词
    jieba.setLogLevel(60)  # 屏蔽 jieba 自带的 loading 日志，保持命令行输出干净
    _HAS_JIEBA = True
except ImportError:  # pragma: no cover
    _HAS_JIEBA = False  # 没有 jieba 时退化为二元组分词，效果差一些但能跑

try:
    from rank_bm25 import BM25Okapi
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 rank-bm25，请先执行：pip install -r requirements.txt")

try:
    from openai import OpenAI  # OpenAI SDK：绝大多数国产大模型都兼容它的协议
except ImportError:  # pragma: no cover
    OpenAI = None  # 离线模式下不需要它

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


# ==============================================================================
# ★CORE-1  配置中心
# ------------------------------------------------------------------------------
# 作用：把所有"会变的东西"集中到一处（模型供应商、地址、密钥、切片长度、召回数量）。
# 复用去向：阶段 2 搬进 app/core/config.py，改用 pydantic-settings 读取，字段名可保持一致。
# 设计要点：**业务代码里不允许出现任何硬编码的供应商地址或模型名**，
#           换供应商 = 改 .env，代码零改动（这是阶段 3 的验收项之一）。
# ==============================================================================

# 内置供应商预设：只填 LLM_PROVIDER=deepseek 就会自动带出 base_url 与默认模型
# 说明：所有预设都走 OpenAI 兼容协议，这是"可替换供应商"能成立的前提。
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "chat_model": "deepseek-chat",
        # DeepSeek 官方不提供 Embedding 接口，因此用本地哈希向量兜底（见 CORE-3）
        "embed_model": None,
        "embed_dim": 1024,
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "chat_model": "moonshot-v1-8k",
        "embed_model": None,
        "embed_dim": 1024,
    },
    "dashscope": {  # 阿里云百炼 / 通义千问（OpenAI 兼容模式）
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "chat_model": "qwen-plus",
        "embed_model": "text-embedding-v3",
        "embed_dim": 1024,
    },
    "zhipu": {  # 智谱 GLM
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "chat_model": "glm-4-flash",
        "embed_model": "embedding-3",
        "embed_dim": 2048,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "chat_model": "gpt-4o-mini",
        "embed_model": "text-embedding-3-small",
        "embed_dim": 1536,
    },
    "local": {  # 本地/自建：例如 Ollama、vLLM，同样暴露 OpenAI 兼容接口
        "base_url": "http://localhost:11434/v1",
        "chat_model": "qwen2.5:7b",
        "embed_model": "bge-m3",
        "embed_dim": 1024,
    },
}


@dataclass
class Settings:
    """全局配置对象。阶段 2 会换成 pydantic-settings 的 BaseSettings，字段基本不变。"""

    # ---- 大模型（对话）相关 ----
    llm_provider: str = "deepseek"       # 供应商代号，见 PROVIDER_PRESETS
    llm_api_key: str = ""                # 对话模型的 API Key
    llm_base_url: str = ""               # 接口地址（OpenAI 兼容）
    llm_model: str = ""                  # 对话模型名
    llm_temperature: float = 0.2         # 售后场景要稳定复现，温度压低
    llm_max_tokens: int = 1024

    # ---- 向量（Embedding）相关：单独配置，因为很多对话供应商不提供向量接口 ----
    embed_provider: str = ""             # 为空则跟随 llm_provider
    embed_api_key: str = ""
    embed_base_url: str = ""
    embed_model: str = ""                # 为空表示该供应商没有向量接口 → 本地降级
    embed_dim: int = 1024                # 向量维度：一旦建库就不要改，改了必须重建索引

    # ---- 检索相关 ----
    chunk_size: int = 500                # 切片长度（字符数），作业要求 500 字
    chunk_overlap: int = 80              # 相邻切片重叠长度，防止一句话被切成两半
    top_k: int = 3                       # 混合检索召回条数，作业要求 Top3
    rrf_k: int = 60                      # RRF 融合平滑系数，经典取值 60
    bm25_weight: float = 1.0             # 关键词通道权重（RRF 下一般保持 1.0）
    vector_weight: float = 1.0           # 向量通道权重

    # ---- 路径相关 ----
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent)
    doc_path: Path = None                # type: ignore[assignment]
    index_dir: Path = None               # type: ignore[assignment]

    # ---- 运行模式 ----
    offline: bool = False                # True = 强制本地降级（不联网、不花钱）

    def __post_init__(self) -> None:
        # 未显式指定路径时，使用项目内的默认位置
        self.doc_path = Path(self.doc_path) if self.doc_path else self.project_root / "data" / "售后知识库.md"
        self.index_dir = Path(self.index_dir) if self.index_dir else self.project_root / "index"
        self.index_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 派生属性：判断是否真的能联网调用 ----------
    @property
    def can_call_llm(self) -> bool:
        """是否具备调用远端大模型对话的条件。"""
        return (not self.offline) and bool(self.llm_api_key) and OpenAI is not None

    @property
    def can_call_embed(self) -> bool:
        """是否具备调用远端向量接口的条件。"""
        return (not self.offline) and bool(self.embed_api_key) and bool(self.embed_model) and OpenAI is not None


def load_env_file(project_root: Path) -> None:
    """★ONCE-1 辅助函数：加载项目根目录的 .env 文件（若存在）。

    阶段 2 会用 pydantic-settings 自动完成这一步，这里手写是为了让单文件脚本也能读配置。
    """
    if load_dotenv is None:
        return
    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


def build_settings(overrides: dict[str, Any] | None = None) -> Settings:
    """★CORE-1 从环境变量 + 命令行参数构造配置对象。

    读取优先级：命令行参数 > 环境变量(.env) > 供应商预设 > 代码默认值。
    """
    load_env_file(Path(__file__).resolve().parent)

    provider = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
    preset = PROVIDER_PRESETS.get(provider, {})

    embed_provider = os.getenv("EMBED_PROVIDER", "").strip().lower() or provider
    embed_preset = PROVIDER_PRESETS.get(embed_provider, {})

    settings = Settings(
        # 对话模型配置
        llm_provider=provider,
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "").strip() or preset.get("base_url", ""),
        llm_model=os.getenv("LLM_MODEL", "").strip() or preset.get("chat_model", ""),
        # 向量模型配置（单独一套，允许和对话不是同一家）
        embed_provider=embed_provider,
        embed_api_key=os.getenv("EMBED_API_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip(),
        embed_base_url=os.getenv("EMBED_BASE_URL", "").strip() or embed_preset.get("base_url", ""),
        embed_model=os.getenv("EMBED_MODEL", "").strip() or embed_preset.get("embed_model", "") or "",
        embed_dim=int(os.getenv("EMBED_DIM", "") or preset.get("embed_dim", 1024)),
        # 检索参数
        chunk_size=int(os.getenv("CHUNK_SIZE", "500")),
        chunk_overlap=int(os.getenv("CHUNK_OVERLAP", "80")),
        top_k=int(os.getenv("TOP_K", "3")),
    )

    # 命令行覆盖（优先级最高）
    for key, value in (overrides or {}).items():
        if value is not None and hasattr(settings, key):
            setattr(settings, key, value)

    # 离线模式的自动判定：没有 Key 就降级，保证脚本永远能跑起来
    if not settings.offline:
        if not settings.llm_api_key:
            settings.offline = True
            print("[提示] 未检测到 LLM_API_KEY，自动进入【本地降级模式】："
                  "检索链路完整可跑，最终回答由召回原文拼接生成，不调用大模型。")
    return settings


# ==============================================================================
# ★CORE-2  文档加载与切片
# ------------------------------------------------------------------------------
# 作用：把一份长 Markdown 切成若干"语义完整、长度可控"的片段（Chunk）。
# 复用去向：阶段 2 搬进 services/knowledge/loader.py + splitter.py。
# 设计要点：
#   1) 先按 Markdown 标题切分，让每个 Chunk 知道自己属于哪一节（便于回答时标注出处）；
#   2) 再按 500 字二次切分，并在句子边界（。！？）处断开，避免把一句话劈成两半；
#   3) 相邻切片保留 80 字重叠，防止跨切片的答案丢失上下文。
# ==============================================================================

@dataclass
class Chunk:
    """一个知识切片。阶段 2 就是 policy_chunks 表的一行（+ embedding 列）。"""
    id: int                 # 切片序号，同时作为 FAISS 向量的行号（两者严格对齐！）
    heading: str            # 所属章节标题，用于回答时标注出处
    text: str               # 切片正文
    char_len: int = 0       # 字符长度，便于排查切片是否合理

    def __post_init__(self) -> None:
        self.char_len = len(self.text)

    def as_citation(self) -> str:
        """生成给人看的出处标签，例如：『三、智能门锁常见故障与处理』"""
        return f"《{self.heading}》"


def load_markdown(path: Path) -> str:
    """★CORE-2 读取本地 Markdown 原文。

    阶段 2 这里会扩展为：md / pdf / docx 三种解析器（pypdf、python-docx）。
    """
    if not path.exists():
        sys.exit(f"[错误] 找不到知识库文件：{path}\n请确认路径，或用 --doc 参数指定。")
    return path.read_text(encoding="utf-8")


def split_markdown_by_heading(md_text: str) -> list[tuple[str, str]]:
    """★CORE-2 第一步：按 Markdown 标题把文档拆成 (标题, 正文) 列表。

    只认 # 到 ###### 开头的行。文档开头如果没有标题，归入『（开头）』。
    """
    sections: list[tuple[str, str]] = []
    current_heading = "（开头）"
    buffer: list[str] = []

    for line in md_text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:  # 遇到新标题 → 先把上一段收尾
            if buffer:
                sections.append((current_heading, "\n".join(buffer).strip()))
                buffer = []
            current_heading = match.group(2).strip()
        else:
            buffer.append(line)

    if buffer:  # 收尾最后一段
        sections.append((current_heading, "\n".join(buffer).strip()))

    # 过滤掉空段（例如只有分隔线的段落）
    return [(h, b) for h, b in sections if b]


def split_text_fixed_size(text: str, size: int, overlap: int) -> list[str]:
    """★CORE-2 第二步：把长文本按固定字数切分，优先在句子边界断开。

    参数：
        size     —— 每片目标字数（作业要求 500）
        overlap  —— 相邻两片的重叠字数（建议 size 的 10%~20%）

    实现思路：
        从 start 开始取 size 个字符得到候选窗口；
        在窗口末尾 30% 的范围内，从后往前找最近的句子结束符（。！？；\n），
        找到就在那里断，找不到就硬断在 size 处。
    """
    if len(text) <= size:
        return [text.strip()] if text.strip() else []

    pieces: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + size, text_len)
        window = text[start:end]

        if end < text_len:  # 不是最后一片，才需要找断点
            # 只在窗口后 30% 区域里找句号，尽量让切片接近 500 字
            search_from = int(len(window) * 0.7)
            cut = -1
            for i in range(len(window) - 1, search_from - 1, -1):
                if window[i] in "。！？；\n":
                    cut = i + 1
                    break
            if cut > 0:
                window = window[:cut]
                end = start + cut

        piece = window.strip()
        if piece:
            pieces.append(piece)

        # 下一片的起点回退 overlap 个字，形成重叠
        step = max(1, len(window) - overlap)
        start += step

    return pieces


def build_chunks(md_text: str, settings: Settings) -> list[Chunk]:
    """★CORE-2 主入口：Markdown 原文 → Chunk 列表。"""
    chunks: list[Chunk] = []
    for heading, body in split_markdown_by_heading(md_text):
        for piece in split_text_fixed_size(body, settings.chunk_size, settings.chunk_overlap):
            # 把标题也拼进正文：检索时标题里的关键词（如"智能门锁"）能被命中
            full_text = f"{heading}\n{piece}" if heading != "（开头）" else piece
            chunks.append(Chunk(id=len(chunks), heading=heading, text=full_text))
    return chunks


# ==============================================================================
# ★CORE-3  Embedding 客户端（供应商可配置）
# ------------------------------------------------------------------------------
# 作用：把文本变成向量。对外只暴露一个方法 embed_texts()，内部可以是任意供应商。
# 复用去向：阶段 2 抽象成 services/llm/base.py 里的 EmbeddingProvider 协议，
#          再由 factory.py 按配置返回具体实现 —— 业务代码完全不变。
# 设计要点：
#   - 远端走 OpenAI 的 /embeddings 协议（国产大模型基本都兼容）；
#   - 远端不可用时自动降级为"本地哈希向量"：把分词结果哈希到固定维度上做词频统计。
#     它只能表达字面相似，没有语义泛化能力，但足以让检索链路完整跑通并演示。
# ==============================================================================

class Embedder:
    """统一的向量化入口。调用方只关心 embed_texts()，不关心背后是谁。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dim = settings.embed_dim
        # 远端模式：创建一个 OpenAI 兼容的客户端（注意 base_url 决定打到哪一家）
        self._client = None
        if settings.can_call_embed and OpenAI is not None:
            self._client = OpenAI(api_key=settings.embed_api_key, base_url=settings.embed_base_url)

    @property
    def mode(self) -> str:
        """当前工作模式，打印出来让用户知道用的是哪家。"""
        if self._client:
            return f"远端 Embedding（{self.settings.embed_provider} / {self.settings.embed_model} / dim={self.dim}）"
        return f"本地哈希向量（降级模式 / dim={self.dim}）"

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """★CORE-3 把一批文本转成向量矩阵，形状 (n, dim)，已做 L2 归一化。

        归一化之后，向量内积 == 余弦相似度，所以 FAISS 用 IndexFlatIP 即可。
        """
        if self._client is not None:
            vectors = self._embed_remote(texts)
        else:
            vectors = self._embed_local(texts)

        # 统一做 L2 归一化，保证与 FAISS 的 IndexFlatIP（内积）配合等价于余弦相似度
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0  # 防止除零
        return (vectors / norms).astype("float32")

    # ---------------- 远端实现（OpenAI 兼容协议） ----------------
    def _embed_remote(self, texts: list[str]) -> np.ndarray:
        """调用远端 /embeddings 接口。绝大多数供应商一次请求有条数上限，这里按 64 条分批。"""
        result: list[list[float]] = []
        batch_size = 64
        for i in range(0, len(texts), batch_size):
            batch = texts[i: i + batch_size]
            resp = self._client.embeddings.create(model=self.settings.embed_model, input=batch)
            # data 的顺序与 input 顺序一致，按 index 排序后再取，防止乱序
            for item in sorted(resp.data, key=lambda x: x.index):
                result.append(item.embedding)
        arr = np.asarray(result, dtype="float32")
        # 若实际返回维度与配置不一致，以实际为准并给出提示（避免 FAISS 建库维度错乱）
        if arr.shape[1] != self.dim:
            print(f"[警告] 配置维度 {self.dim} 与接口实际返回 {arr.shape[1]} 不一致，已按实际维度处理。")
            self.dim = arr.shape[1]
        return arr

    # ---------------- 本地降级实现 ----------------
    def _embed_local(self, texts: list[str]) -> np.ndarray:
        """本地哈希向量（Hashing Trick）。

        做法：对每条文本分词 → 每个词用 MD5 哈希映射到 dim 个桶之一 → 桶内计数加一。
        效果：字面重合度高的文本向量相近（相当于一个简化的词袋模型）。
        注意：仅供无 Key 环境演示，不具备语义泛化能力，正式环境请配置真实 Embedding。
        """
        matrix = np.zeros((len(texts), self.dim), dtype="float32")
        for row, text in enumerate(texts):
            tokens = tokenize(text)
            for token in tokens:
                digest = hashlib.md5(token.encode("utf-8")).hexdigest()
                bucket = int(digest[:8], 16) % self.dim
                matrix[row, bucket] += 1.0
        return matrix


# ==============================================================================
# 分词函数（BM25 与本地向量共用）
# ------------------------------------------------------------------------------
# 复用去向：阶段 2 与 Postgres 全文检索、pgvector 检索共用同一套分词，保证线上线下一致。
# ==============================================================================

# 常见虚词与标点，分词后过滤掉，否则会稀释 BM25 的区分度
STOPWORDS = set("""的 了 是 在 和 与 或 有 也 就 都 而 及 等 个 之 为 以 并 但 很 到 说 吗 呢 吧 啊
， 。 、 ； ： ？ ！ “ ” ‘ ’ （ ） 《 》 〈 〉 【 】 — … · \t\n\r""".split())


def tokenize(text: str) -> list[str]:
    """中文分词 + 去停用词。优先 jieba，缺失时退化为二元组切分。"""
    text = text.lower()
    if _HAS_JIEBA:
        tokens = jieba.lcut(text)
    else:  # 兜底：按字符二元组切分，中文场景下勉强可用
        tokens = [text[i: i + 2] for i in range(len(text) - 1)] or list(text)
    return [t.strip() for t in tokens if t.strip() and t.strip() not in STOPWORDS]


# ==============================================================================
# ★CORE-4  向量库（FAISS）+ 持久化
# ------------------------------------------------------------------------------
# 作用：存向量并按相似度检索。
# 复用去向：★CORE-B —— 阶段 2 换成 Postgres + pgvector，但对外接口保持不变：
#           build(chunks, vectors) / search(query_vector, top_k) -> [(chunk_id, score)]
#           只要接口一致，上层 Pipeline 一行都不用改，这就是"先抽象后实现"的价值。
# ==============================================================================

@contextlib.contextmanager
def _in_dir(path: Path):
    """★ONCE-1 工具函数：临时切换工作目录，退出时自动切回。

    存在的理由（Windows 专属坑）：FAISS 底层是 C++ 的 fstream，在 Windows 上
    无法打开含非 ASCII 字符（如中文）的绝对路径，会报 "No such file or directory"。
    解决办法是先 chdir 到目标目录，再把**纯英文的文件名**交给 FAISS。
    阶段 2 换成 pgvector 后，这个兼容层可以直接删除。
    """
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


class VectorStore:
    """FAISS 向量库的薄封装：负责建库、持久化、加载、检索。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.index_path = settings.index_dir / "faiss.index"
        self.meta_path = settings.index_dir / "chunks.pkl"
        self.fingerprint_path = settings.index_dir / "fingerprint.json"
        self.index = None           # faiss.Index 实例
        self.chunks: list[Chunk] = []

    # ---------- 指纹：判断索引是否过期 ----------
    def _fingerprint(self, doc_text: str, embedder: Embedder) -> dict[str, Any]:
        """索引指纹 = 文档内容哈希 + 向量配置。任何一项变了都必须重建索引。

        这一步很关键：否则改了知识库文档却还在用旧索引，会出现"明明写了却检索不到"的诡异问题。
        """
        return {
            "doc_sha1": hashlib.sha1(doc_text.encode("utf-8")).hexdigest(),
            "embed_model": embedder.settings.embed_model or "local-hash",
            "embed_dim": embedder.dim,
            "chunk_size": self.settings.chunk_size,
            "chunk_overlap": self.settings.chunk_overlap,
        }

    def load_if_fresh(self, doc_text: str, embedder: Embedder) -> bool:
        """尝试加载已有索引；若文档或配置已变更则返回 False（调用方随后重建）。"""
        if not (self.index_path.exists() and self.meta_path.exists() and self.fingerprint_path.exists()):
            return False
        try:
            fingerprint = json.loads(self.fingerprint_path.read_text(encoding="utf-8"))
            if fingerprint != self._fingerprint(doc_text, embedder):
                return False
            # 注意：Windows 下 FAISS 的 C++ 文件 IO 打不开含中文的绝对路径，
            # 因此先切到索引目录，用纯英文的相对文件名读写（_in_dir 会自动切回）。
            with _in_dir(self.settings.index_dir):
                self.index = faiss.read_index(self.index_path.name)
            with open(self.meta_path, "rb") as f:
                self.chunks = pickle.load(f)
            return True
        except Exception as exc:  # 索引文件损坏时也走重建分支
            print(f"[提示] 索引加载失败，将重建：{exc}")
            return False

    def build(self, chunks: list[Chunk], vectors: np.ndarray, doc_text: str, embedder: Embedder) -> None:
        """★CORE-4 建库并落盘。

        IndexFlatIP = 暴力内积检索。因为向量已归一化，内积即余弦相似度。
        数据量上万之后再考虑换成 HNSW / IVF 这类近似索引；当前规模暴力检索反而又快又准。
        """
        self.chunks = chunks
        self.index = faiss.IndexFlatIP(vectors.shape[1])  # 维度以实际向量为准
        self.index.add(vectors)                            # 行号 == chunk.id，务必对齐

        # 落盘：索引文件用 _in_dir 绕开中文路径问题，元数据由 Python 直接写（支持中文路径）
        with _in_dir(self.settings.index_dir):
            faiss.write_index(self.index, self.index_path.name)
        with open(self.meta_path, "wb") as f:
            pickle.dump(chunks, f)
        self.fingerprint_path.write_text(
            json.dumps(self._fingerprint(doc_text, embedder), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def search(self, query_vector: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        """★CORE-4 检索：返回 [(chunk_id, 相似度), ...]，按相似度降序。"""
        # FAISS 要求输入是二维 float32 数组
        query = np.asarray(query_vector, dtype="float32").reshape(1, -1)
        scores, ids = self.index.search(query, top_k)
        return [(int(cid), float(score)) for cid, score in zip(ids[0], scores[0]) if cid != -1]


# ==============================================================================
# ★CORE-5  BM25 关键词检索
# ------------------------------------------------------------------------------
# 作用：补足向量检索的短板 —— 型号、编号、专有名词这类"必须字面命中"的查询。
# 复用去向：阶段 2 与 pgvector 的余弦检索并行使用，融合逻辑（CORE-6）原样复用。
# 为什么必须有它：纯向量检索在"智能门锁 Pro 排线松动"这类精确词上容易召回错型号，
#                BM25 对字面词频敏感，两者互补后 Top3 命中率明显更稳。
# ==============================================================================

class BM25Retriever:
    """对 Chunk 列表建立 BM25 倒排索引，支持关键词检索。"""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        # BM25 的输入是"每条文档的分词列表"
        self.corpus_tokens = [tokenize(c.text) for c in chunks]
        self.bm25 = BM25Okapi(self.corpus_tokens)

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        """★CORE-5 检索：返回 [(chunk_id, bm25得分), ...]，按得分降序。"""
        scores = self.bm25.get_scores(tokenize(query))
        # argsort 得到升序，再倒序取前 top_k
        ranked = np.argsort(scores)[::-1][:top_k]
        return [(int(idx), float(scores[idx])) for idx in ranked if scores[idx] > 0]


# ==============================================================================
# ★CORE-6  混合检索融合（RRF：倒数排名融合）
# ------------------------------------------------------------------------------
# 作用：把"向量通道"和"关键词通道"两个结果列表合成一个最终排序。
# 复用去向：阶段 2 原样搬进 services/retrieval_service.py，加一层元数据过滤即可。
#
# 为什么用 RRF 而不是把两路分数归一化后加权相加？
#   因为 BM25 得分是无上界的词频统计值，余弦相似度固定在 [-1, 1]，
#   两个量纲完全不同，加权相加需要人为调参且不稳定。
#   RRF 只关心"排第几"，与分数绝对值无关：
#       score(d) = Σ  1 / (k + rank_i(d))
#   无需归一化、无需调参，是工业界混合检索的默认做法。k=60 是经验值。
# ==============================================================================

def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[int, float]]],
    weights: list[float] | None = None,
    k: int = 60,
) -> list[tuple[int, float]]:
    """★CORE-6 RRF 融合。

    参数：
        ranked_lists —— 若干个通道的检索结果，每个是 [(chunk_id, score), ...]（已按分数降序）
        weights      —— 每个通道的权重（默认等权）
        k            —— 平滑系数，越小则越看重头部排名
    返回：
        [(chunk_id, 融合得分), ...]，按融合得分降序
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)

    fused: dict[int, float] = {}
    for weight, ranked in zip(weights, ranked_lists):
        for rank, (chunk_id, _score) in enumerate(ranked, start=1):
            # 同一文档在多个通道都出现 → 得分累加，这就是"互相印证会排更前"
            fused[chunk_id] = fused.get(chunk_id, 0.0) + weight * (1.0 / (k + rank))

    return sorted(fused.items(), key=lambda x: x[1], reverse=True)


def hybrid_search(
    query: str,
    embedder: Embedder,
    vector_store: VectorStore,
    bm25: BM25Retriever,
    settings: Settings,
) -> list[tuple[Chunk, float, dict[str, Any]]]:
    """★CORE-6 混合检索主入口：一次调用拿到融合后的 Top-K 切片。

    返回 [(Chunk, 融合得分, 调试信息), ...]
    调试信息里保留了两路各自的排名，方便排查"到底是哪一路召回的"。
    """
    # ---- 通道一：向量语义检索 ----
    query_vector = embedder.embed_texts([query])[0]
    vector_hits = vector_store.search(query_vector, top_k=settings.top_k * 3)  # 多召回一些供融合

    # ---- 通道二：BM25 关键词检索 ----
    bm25_hits = bm25.search(query, top_k=settings.top_k * 3)

    # ---- RRF 融合 ----
    fused = reciprocal_rank_fusion(
        [vector_hits, bm25_hits],
        weights=[settings.vector_weight, settings.bm25_weight],
        k=settings.rrf_k,
    )[: settings.top_k]

    # 记录每个 Chunk 在两路中的排名，便于命令行展示与后续调优
    vector_rank = {cid: i + 1 for i, (cid, _) in enumerate(vector_hits)}
    bm25_rank = {cid: i + 1 for i, (cid, _) in enumerate(bm25_hits)}

    results: list[tuple[Chunk, float, dict[str, Any]]] = []
    for chunk_id, score in fused:
        chunk = vector_store.chunks[chunk_id]
        results.append((
            chunk,
            score,
            {
                "vector_rank": vector_rank.get(chunk_id),
                "vector_score": round(dict(vector_hits).get(chunk_id, 0.0), 4),
                "bm25_rank": bm25_rank.get(chunk_id),
                "bm25_score": round(dict(bm25_hits).get(chunk_id, 0.0), 4),
            },
        ))
    return results


# ==============================================================================
# ★CORE-7  提示词组装
# ------------------------------------------------------------------------------
# 作用：把召回的片段 + 用户问题拼成一份结构化的提示词。
# 复用去向：阶段 2 抽到 agent/prompts/ 目录下的 .md 模板文件，支持热更新，不改代码。
# 设计要点（这三条是售后场景的"护栏"，比模型选型更重要）：
#   1) 强制引用：答案必须标注 [1][2][3]，对应给定的片段编号；
#   2) 禁止编造：片段里没有的信息必须明说"知识库未覆盖"，不许凭常识补全；
#   3) 结构化输出：按【结论】【可能原因】【处理步骤】【是否需要上门】四段回答。
# ==============================================================================

SYSTEM_PROMPT = """你叫「智家速修小智」，是智能家居售后维修的智能助理，服务对象是报修住户与一线维修工程师。

严格遵守以下规则：
1. 只依据提供的《参考资料》回答，不得使用参考资料之外的知识进行主观推断。
2. 每一条结论后面必须标注引用编号，格式如 [1] 或 [2][3]，编号对应参考资料的序号。
3. 如果参考资料中没有答案，直接回答："当前知识库未覆盖该问题，建议转人工客服。" 不要编造。
4. 涉及费用、时效、保修期限的数字，必须与参考资料完全一致，不得四舍五入或估算。
5. 回答使用简体中文，按以下四段输出，缺失的段落写"无"：
   【结论】一句话给出判断
   【可能原因】按可能性从高到低列出
   【处理步骤】分步骤说明，可远程解决的先给远程方案
   【是否需要上门】明确回答"可远程解决"或"需上门处理"，并说明理由
"""


def build_prompt(question: str, hits: list[tuple[Chunk, float, dict[str, Any]]]) -> list[dict[str, str]]:
    """★CORE-7 组装 OpenAI 兼容协议的 messages 数组。

    片段按 [1][2][3] 编号，并在片段前标注所属章节，让模型能写出准确的出处。
    """
    # 逐个编号，拼成参考资料块
    references = []
    for i, (chunk, _score, _debug) in enumerate(hits, start=1):
        references.append(f"[{i}] 来源：{chunk.as_citation()}\n{chunk.text}")

    user_content = (
        "《参考资料》\n"
        + "\n\n".join(references)
        + f"\n\n《用户问题》\n{question}\n\n"
        + "请按系统要求的四段格式回答，并为每条结论标注引用编号。"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ==============================================================================
# ★CORE-8  LLM 调用函数（独立函数，供应商可配置）—— 本作业的核心要求之一
# ------------------------------------------------------------------------------
# 作用：唯一对外的大模型对话入口。换供应商只需改 Settings，函数本身不动。
# 复用去向：阶段 2 移入 services/llm/，AgentLoop、规则校验、会话摘要全都调它。
# 设计要点：
#   - 单一职责：只负责"把 messages 发出去，把文本拿回来"，不做任何业务判断；
#   - 协议统一：统一走 OpenAI 的 /chat/completions，国产模型基本都兼容；
#   - 失败可控：网络异常/额度不足会抛出明确中文异常，由上层决定是否降级。
# ==============================================================================

def llm_chat(
    messages: list[dict[str, str]],
    settings: Settings,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """★CORE-8 调用大模型生成回答。

    参数：
        messages    —— OpenAI 兼容格式的消息数组 [{"role": "system"/"user"/"assistant", "content": "..."}]
        settings    —— 配置对象，决定打到哪家供应商
        temperature —— 温度，None 表示用配置默认值
        max_tokens  —— 最大生成长度，None 表示用配置默认值

    返回：
        {
          "content": 生成的文本,
          "model":   实际使用的模型名,
          "usage":   {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
        }
    """
    if OpenAI is None:  # pragma: no cover
        raise RuntimeError("未安装 openai 依赖，无法调用大模型，请先 pip install openai")

    if not settings.llm_api_key:
        raise RuntimeError("未配置 LLM_API_KEY，无法调用远端大模型（可加 --offline 使用降级模式）")

    # 每次调用现场创建 client：方便运行中切换供应商，也避免长连接失效问题
    client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)

    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
        temperature=settings.llm_temperature if temperature is None else temperature,
        max_tokens=settings.llm_max_tokens if max_tokens is None else max_tokens,
    )

    usage = getattr(response, "usage", None)
    return {
        "content": (response.choices[0].message.content or "").strip(),
        "model": settings.llm_model,
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        },
    }


def build_offline_answer(question: str, hits: list[tuple[Chunk, float, dict[str, Any]]]) -> str:
    """★ONCE-1 无 Key 环境下的降级回答：直接把召回片段结构化展示出来。

    目的：让没有 API Key 的人也能完整看到"检索到了什么"。
    阶段 2 不会保留这个函数（那时一定有真实模型），但它体现了重要的工程习惯：
    **任何外部依赖都要有兜底路径**。
    """
    if not hits:
        return "当前知识库未覆盖该问题，建议转人工客服。（降级模式：无任何片段命中）"

    lines = [
        "【降级模式提示】未配置大模型 Key，以下为检索命中的原文片段，未经过模型加工。",
        "",
        f"【结论】命中 {len(hits)} 条相关知识片段，请人工参考下方原文作答。",
        "",
        "【可能原因与处理步骤】请阅读以下原文：",
    ]
    for i, (chunk, score, _debug) in enumerate(hits, start=1):
        lines.append("")
        lines.append(f"[{i}] 来源：{chunk.as_citation()}（融合得分 {score:.4f}）")
        lines.append(chunk.text[:400] + ("…" if len(chunk.text) > 400 else ""))
    lines.append("")
    lines.append("【是否需要上门】请参考原文中的判定标准，或转人工客服确认。")
    return "\n".join(lines)


# ==============================================================================
# ★CORE-9  RAG 编排 Pipeline
# ------------------------------------------------------------------------------
# 作用：把"检索 → 拼提示词 → 调模型"串成一次完整问答。
# 复用去向：阶段 2 拆成 services/retrieval_service.py + agent/loop.py，
#          但 ask() 的三步骨架（召回、组装、生成）保持不变。
# ==============================================================================

@dataclass
class Answer:
    """一次问答的完整结果，便于命令行展示，也便于阶段 2 落库到 messages 表。"""
    question: str
    content: str
    hits: list[tuple[Chunk, float, dict[str, Any]]]
    model: str
    usage: dict[str, int]
    degraded: bool  # 是否走了降级模式


class RAGPipeline:
    """检索增强问答流水线：持有索引与检索器，对外提供 ask()。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.embedder = Embedder(settings)
        self.vector_store = VectorStore(settings)
        self.bm25: BM25Retriever | None = None
        self.chunks: list[Chunk] = []

    # ---------- ★ONCE-1 索引构建（阶段 2 会挪到管理端的"重建索引"按钮里） ----------
    def prepare(self, force_rebuild: bool = False) -> None:
        """加载或重建索引。这是启动阶段的耗时操作，只做一次。"""
        doc_text = load_markdown(self.settings.doc_path)

        # 1) 先尝试复用已有索引（文档没变、配置没变就不重复花钱调 Embedding）
        if not force_rebuild and self.vector_store.load_if_fresh(doc_text, self.embedder):
            self.chunks = self.vector_store.chunks
            self.bm25 = BM25Retriever(self.chunks)
            print(f"[索引] 已加载本地索引：{len(self.chunks)} 个切片（{self.embedder.mode}）")
            return

        # 2) 切片
        self.chunks = build_chunks(doc_text, self.settings)
        if not self.chunks:
            sys.exit("[错误] 知识库切片结果为空，请检查文档内容。")
        print(f"[索引] 文档切片完成：{len(self.chunks)} 个切片，"
              f"平均 {sum(c.char_len for c in self.chunks) // len(self.chunks)} 字/片")

        # 3) 向量化（这一步才会真正调用远端接口或本地降级）
        vectors = self.embedder.embed_texts([c.text for c in self.chunks])
        print(f"[索引] 向量化完成：{vectors.shape[0]} × {vectors.shape[1]} 维（{self.embedder.mode}）")

        # 4) 建库并落盘
        self.vector_store.build(self.chunks, vectors, doc_text, self.embedder)
        print(f"[索引] 已写入并持久化到：{self.vector_store.index_path}")

        # 5) 建 BM25 倒排（纯本地计算，不花钱）
        self.bm25 = BM25Retriever(self.chunks)
        print("[索引] BM25 倒排索引构建完成")

    # ---------- ★CORE-9 一次完整问答 ----------
    def ask(self, question: str) -> Answer:
        """输入问题，返回带引用的答案。三步：混合检索 → 拼提示词 → 调模型。"""
        if self.bm25 is None:
            raise RuntimeError("请先调用 prepare() 构建索引")

        # 第一步：混合检索 Top-K
        hits = hybrid_search(question, self.embedder, self.vector_store, self.bm25, self.settings)

        # 第二步 + 第三步：拼提示词并调用模型（无 Key 时走降级）
        if not self.settings.can_call_llm:
            return Answer(
                question=question,
                content=build_offline_answer(question, hits),
                hits=hits, model="local-fallback", usage={}, degraded=True,
            )

        messages = build_prompt(question, hits)
        try:
            result = llm_chat(messages, self.settings)
        except Exception as exc:  # 远端失败也要能给出答案，不能把用户晾着
            print(f"[警告] 大模型调用失败，自动降级：{exc}")
            return Answer(
                question=question,
                content=build_offline_answer(question, hits),
                hits=hits, model="local-fallback", usage={}, degraded=True,
            )

        return Answer(
            question=question,
            content=result["content"],
            hits=hits,
            model=result["model"],
            usage=result["usage"],
            degraded=False,
        )


# ==============================================================================
# ★ONCE-1  命令行交互主循环
# ------------------------------------------------------------------------------
# 阶段 2 这一段会被 FastAPI 的 /api/v1/chat 接口（SSE 流式）取代，届时可以整体删除。
# 保留它的价值：阶段 1 能脱离 Web 快速验证检索质量与提示词效果。
# ==============================================================================

def print_hits(answer: Answer) -> None:
    """把召回结果打印出来，让人能直观判断"检索准不准"。"""
    print("\n" + "-" * 70)
    print(f"召回片段（混合检索 Top{len(answer.hits)}）")
    print("-" * 70)
    for i, (chunk, score, debug) in enumerate(answer.hits, start=1):
        vec_rank = debug["vector_rank"] or "-"
        kw_rank = debug["bm25_rank"] or "-"
        print(f"[{i}] 融合得分 {score:.4f} | 向量第{vec_rank}名({debug['vector_score']}) "
              f"| 关键词第{kw_rank}名({debug['bm25_score']})")
        print(f"    来源：{chunk.as_citation()}  字数：{chunk.char_len}")
        preview = chunk.text.replace("\n", " ")[:90]
        print(f"    预览：{preview}…")


def run_cli(settings: Settings, force_rebuild: bool, single_question: str | None) -> None:
    """命令行入口：先构建索引，再进入问答循环。"""
    print("=" * 70)
    print(" 智家速修 · AI 售后工单系统 —— 阶段 1 最小原型（RAG）")
    print("=" * 70)

    pipeline = RAGPipeline(settings)
    pipeline.prepare(force_rebuild=force_rebuild)

    print("\n" + "=" * 70)
    print(f" 对话模型：{settings.llm_provider} / {settings.llm_model}"
          f"{'（降级：未调用）' if not settings.can_call_llm else ''}")
    print(f" 向量模型：{pipeline.embedder.mode}")
    print(f" 知识库：{settings.doc_path.name}　切片 {len(pipeline.chunks)} 个　召回 Top{settings.top_k}")
    print(" 输入 q / quit / exit 退出")
    print("=" * 70)

    def handle(question: str) -> None:
        answer = pipeline.ask(question)
        print_hits(answer)
        print("\n" + "-" * 70)
        if answer.degraded:
            print("回答（降级模式）")
        else:
            print(f"回答（模型：{answer.model}　"
                  f"tokens：{answer.usage.get('total_tokens', 0)}）")
        print("-" * 70)
        print(answer.content)
        print()

    if single_question:  # 单条模式：给自动化脚本/演示用
        handle(single_question)
        return

    while True:  # 交互模式
        try:
            question = input("\n请输入问题 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not question:
            continue
        if question.lower() in {"q", "quit", "exit"}:
            print("再见！")
            break
        handle(question)


def parse_args() -> argparse.Namespace:
    """★ONCE-1 命令行参数解析。"""
    parser = argparse.ArgumentParser(
        description="智家速修 AI 售后工单系统 —— 阶段 1 RAG 最小原型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--doc", help="知识库 Markdown 路径（默认 data/售后知识库.md）")
    parser.add_argument("--question", "-q", help="单条提问模式，回答后直接退出")
    parser.add_argument("--topk", type=int, help="混合检索召回条数（默认 3）")
    parser.add_argument("--chunk-size", type=int, help="切片字数（默认 500）")
    parser.add_argument("--rebuild", action="store_true", help="强制重建向量索引")
    parser.add_argument("--offline", action="store_true", help="强制本地降级模式，不调用任何远端接口")
    return parser.parse_args()


def main() -> None:
    """程序入口。"""
    args = parse_args()
    settings = build_settings(overrides={
        "doc_path": Path(args.doc) if args.doc else None,
        "top_k": args.topk,
        "chunk_size": args.chunk_size,
        "offline": True if args.offline else None,  # 命令行显式指定才覆盖
    })
    run_cli(settings, force_rebuild=args.rebuild, single_question=args.question)


if __name__ == "__main__":
    main()
