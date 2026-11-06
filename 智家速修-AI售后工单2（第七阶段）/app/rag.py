# -*- coding: utf-8 -*-
"""RAG 检索模块（阶段 1 同源算法的服务化版本）。

与阶段 1 的关系：
- 切片算法（按标题分节 + 500 字固定切片，句号处优先断开）、BM25 分词、
  RRF 融合公式与阶段 1 完全一致 —— 这些是验证过的核心资产，不做无谓重写；
- 差异在"存储"：阶段 1 用 FAISS 文件索引，本阶段把向量直接存 SQLite BLOB 列，
  全量载入内存用 numpy 做余弦。原因：
  1) 管理后台要支持"上传即入库、删除即生效"，FAISS 索引文件每次都要落盘重建；
  2) 文档量级小（几十份），内存暴力检索毫无压力；
  3) 顺便绕开了阶段 1 踩过的"FAISS 打不开中文路径"的 Windows 坑。
"""

import hashlib
import re
from typing import Any

import numpy as np

from . import db
from .config import Config

# 常见虚词与标点：分词后过滤，避免稀释 BM25 区分度（与阶段 1 相同）
STOPWORDS = set("""的 了 是 在 和 与 或 有 也 就 都 而 及 等 个 之 为 以 并 但 很 到 说 吗 呢 吧 啊
， 。 、 ； ： ？ ！ “ ” ‘ ’ （ ） 《 》 〈 〉 【 】 — … · \t\n\r""".split())

try:
    import jieba  # 中文分词，效果最好
    _HAS_JIEBA = True
except ImportError:  # 兜底：按二元组切分
    _HAS_JIEBA = False

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


def tokenize(text: str) -> list[str]:
    """中文分词 + 去停用词（BM25 与本地哈希向量共用）。"""
    text = text.lower()
    if _HAS_JIEBA:
        tokens = jieba.lcut(text)
    else:
        tokens = [text[i:i + 2] for i in range(len(text) - 1)] or list(text)
    return [t.strip() for t in tokens if t.strip() and t.strip() not in STOPWORDS]


# ----------------------------------------------------------------------------
# 文档解析：md / pdf → 纯文本
# ----------------------------------------------------------------------------

def parse_upload(filename: str, raw: bytes) -> tuple[str, str]:
    """把上传文件解析成纯文本。返回 (正文, source_type)。不支持的类型抛 ValueError。"""
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix in {".md", ".markdown"}:
        return raw.decode("utf-8", errors="replace"), "md"
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(raw))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages), "pdf"
        except Exception as exc:  # pdf 解析失败原因五花八门，统一给中文提示
            raise ValueError(f"PDF 解析失败：{exc}") from exc
    raise ValueError("仅支持 Markdown（.md）与 PDF（.pdf）文件")


# ----------------------------------------------------------------------------
# 切片（与阶段 1 相同的两步算法）
# ----------------------------------------------------------------------------

def split_markdown_by_heading(md_text: str) -> list[tuple[str, str]]:
    """第一步：按 Markdown 标题拆成 (标题, 正文) 列表。"""
    sections: list[tuple[str, str]] = []
    current_heading = "（开头）"
    buffer: list[str] = []
    for line in md_text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            if buffer:
                sections.append((current_heading, "\n".join(buffer).strip()))
                buffer = []
            current_heading = match.group(2).strip()
        else:
            buffer.append(line)
    if buffer:
        sections.append((current_heading, "\n".join(buffer).strip()))
    return [(h, b) for h, b in sections if b]


def split_text_fixed_size(text: str, size: int, overlap: int) -> list[str]:
    """第二步：固定字数切片，优先在句号处断开，相邻片重叠 overlap 个字。"""
    if len(text) <= size:
        return [text.strip()] if text.strip() else []
    pieces: list[str] = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + size, text_len)
        window = text[start:end]
        if end < text_len:  # 非最后一片才找断点
            search_from = int(len(window) * 0.7)
            cut = -1
            for i in range(len(window) - 1, search_from - 1, -1):
                if window[i] in "。！？；\n":
                    cut = i + 1
                    break
            if cut > 0:
                window = window[:cut]
        piece = window.strip()
        if piece:
            pieces.append(piece)
        start += max(1, len(window) - overlap)
    return pieces


def build_chunks(text: str) -> list[dict]:
    """纯文本 → 切片列表 [{index, heading, text}]。标题拼进正文，检索时标题关键词也能命中。"""
    chunks: list[dict] = []
    for heading, body in split_markdown_by_heading(text):
        for piece in split_text_fixed_size(body, Config.CHUNK_SIZE, Config.CHUNK_OVERLAP):
            full = f"{heading}\n{piece}" if heading != "（开头）" else piece
            chunks.append({"index": len(chunks), "heading": heading, "text": full})
    return chunks


# ----------------------------------------------------------------------------
# Embedding：远端（OpenAI 兼容）+ 本地哈希降级（与阶段 1 同源）
# ----------------------------------------------------------------------------

class Embedder:
    """统一向量化入口。远端不可用时自动降级为本地哈希向量，保证链路永远可跑。"""

    def __init__(self) -> None:
        self.dim = Config.EMBED_DIM
        self._client = None
        if Config.embed_ready() and OpenAI is not None:
            self._client = OpenAI(api_key=Config.EMBED_API_KEY, base_url=Config.EMBED_BASE_URL)

    @property
    def mode(self) -> str:
        if self._client:
            return f"远端 Embedding（{Config.EMBED_PROVIDER} / {Config.EMBED_MODEL}）"
        return "本地哈希向量（降级模式）"

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """一批文本 → (n, dim) 的 float32 矩阵，已 L2 归一化（内积==余弦）。"""
        if self._client is not None:
            vectors = self._embed_remote(texts)
        else:
            vectors = self._embed_local(texts)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vectors / norms).astype("float32")

    def _embed_remote(self, texts: list[str]) -> np.ndarray:
        result: list[list[float]] = []
        batch = 64  # 绝大多数供应商单次上限 64 条
        for i in range(0, len(texts), batch):
            resp = self._client.embeddings.create(model=Config.EMBED_MODEL, input=texts[i:i + batch])
            for item in sorted(resp.data, key=lambda x: x.index):
                result.append(item.embedding)
        arr = np.asarray(result, dtype="float32")
        if arr.shape[1] != self.dim:  # 以接口实际返回为准，避免维度错乱
            self.dim = arr.shape[1]
        return arr

    def _embed_local(self, texts: list[str]) -> np.ndarray:
        """Hashing Trick 词袋：字面重合度高 → 向量相近。仅供无 Key 演示，无语义泛化能力。"""
        matrix = np.zeros((len(texts), self.dim), dtype="float32")
        for row, text in enumerate(texts):
            for token in tokenize(text):
                digest = hashlib.md5(token.encode("utf-8")).hexdigest()
                matrix[row, int(digest[:8], 16) % self.dim] += 1.0
        return matrix


# ----------------------------------------------------------------------------
# RRF 融合（与阶段 1 相同）
# ----------------------------------------------------------------------------

def rrf_fusion(ranked_lists: list[list[tuple[int, float]]], k: int = 60,
               weights: list[float] | None = None) -> list[tuple[int, float]]:
    """score(d) = Σ 1/(k + rank)。只看排名不看分值，免去归一化调参。"""
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    fused: dict[int, float] = {}
    for weight, ranked in zip(weights, ranked_lists):
        for rank, (cid, _score) in enumerate(ranked, start=1):
            fused[cid] = fused.get(cid, 0.0) + weight * (1.0 / (k + rank))
    return sorted(fused.items(), key=lambda x: x[1], reverse=True)


# ----------------------------------------------------------------------------
# 检索服务（内存索引：切片 + 向量矩阵 + BM25）
# ----------------------------------------------------------------------------

class RagService:
    """知识库检索服务。所有写操作（上传/删除/重建）后调用 refresh() 同步内存索引。"""

    def __init__(self) -> None:
        self.embedder = Embedder()
        self.chunks: list[dict] = []      # [{chunk_id, doc_id, filename, heading, text}]
        self.matrix: np.ndarray | None = None   # (n, dim) 归一化矩阵
        self.bm25 = None
        self._bm25_corpus: list[list[str]] = []

    # ---------- 索引装载 ----------

    def refresh(self) -> None:
        """从 SQLite 全量加载切片与向量，重建内存索引。文档量小，全量重建毫秒级。"""
        rows = db.load_all_chunks()
        self.chunks = [
            {"chunk_id": r["id"], "doc_id": r["doc_id"], "filename": r["filename"],
             "heading": r["heading"], "text": r["text"]}
            for r in rows
        ]
        vectors: list[np.ndarray] = []
        for r in rows:
            blob = r["embedding"]
            vectors.append(np.frombuffer(blob, dtype="float32") if blob
                           else np.zeros(self.embedder.dim, dtype="float32"))
        if vectors:
            self.matrix = np.vstack(vectors)
        else:
            self.matrix = None
        self._build_bm25()

    def _build_bm25(self) -> None:
        """BM25 倒排：纯本地计算，切片变更后重建。"""
        if not self.chunks:
            self.bm25 = None
            return
        from rank_bm25 import BM25Okapi
        self._bm25_corpus = [tokenize(c["text"]) for c in self.chunks]
        self.bm25 = BM25Okapi(self._bm25_corpus)

    # ---------- 写操作（管理后台） ----------

    def add_document(self, filename: str, text: str, uploaded_by: int) -> dict:
        """上传入库：解析 → 切片 → 向量化 → 落库 → 刷新内存索引。"""
        chunks = build_chunks(text)
        if not chunks:
            raise ValueError("文档切片结果为空：文件内容太少或为空")
        vectors = self.embedder.embed_texts([c["text"] for c in chunks])
        doc_id = db.create_document(filename, "md" if filename.lower().endswith((".md", ".markdown")) else "pdf",
                                    len(text), uploaded_by)
        rows = [(doc_id, c["index"], c["heading"], c["text"], vectors[i].tobytes())
                for i, c in enumerate(chunks)]
        db.insert_chunks(rows)
        db.set_chunk_count(doc_id, len(chunks))
        self.refresh()
        return {"doc_id": doc_id, "chunks": len(chunks), "chars": len(text)}

    def delete_document(self, doc_id: int) -> None:
        db.delete_document(doc_id)
        self.refresh()

    def revectorize_all(self) -> int:
        """全部文档重新向量化（切换 Embedding 供应商/维度后使用）。"""
        rows = db.load_all_chunks()
        if not rows:
            return 0
        texts = [r["text"] for r in rows]
        vectors = self.embedder.embed_texts(texts)
        for r, v in zip(rows, vectors):
            db.update_chunk_embedding(r["id"], v.tobytes())
        self.refresh()
        return len(rows)

    # ---------- 读操作（问答） ----------

    @property
    def ready(self) -> bool:
        return len(self.chunks) > 0

    def stats(self) -> dict[str, Any]:
        docs, chunks = db.count_docs_and_chunks()
        return {"documents": docs, "chunks": chunks, "embed_mode": self.embedder.mode}

    def search(self, question: str) -> list[dict]:
        """混合检索：向量语义 + BM25 关键词 → RRF 融合 → Top-K。

        返回 [{index, file, heading, snippet, score, vector_rank, bm25_rank}]，
        其中 index 与拼进提示词的 [n] 编号一致，前端引用区据此渲染。
        """
        if not self.ready or self.matrix is None:
            return []

        # 通道一：向量（问题归一化后与切片矩阵做内积 = 余弦相似度）
        qv = self.embedder.embed_texts([question])[0]
        sims = (self.matrix @ qv).astype("float32")
        vec_hits = sorted(enumerate(sims.tolist()), key=lambda x: x[1], reverse=True)[: Config.TOP_K * 3]

        # 通道二：BM25 关键词
        bm25_hits: list[tuple[int, float]] = []
        if self.bm25 is not None:
            scores = self.bm25.get_scores(tokenize(question))
            bm25_hits = sorted(enumerate(scores.tolist()), key=lambda x: x[1], reverse=True)[: Config.TOP_K * 3]

        # RRF 融合
        fused = rrf_fusion([vec_hits, bm25_hits], k=Config.RRF_K,
                           weights=[Config.VECTOR_WEIGHT, Config.BM25_WEIGHT])[: Config.TOP_K]

        vec_rank = {cid: i + 1 for i, (cid, _) in enumerate(vec_hits)}
        bm25_rank = {cid: i + 1 for i, (cid, _) in enumerate(bm25_hits)}
        hits: list[dict] = []
        for i, (cid, score) in enumerate(fused, start=1):
            chunk = self.chunks[cid]
            hits.append({
                "index": i,
                "file": chunk["filename"],
                "heading": chunk["heading"],
                "snippet": chunk["text"][:200],
                "score": round(float(score), 4),
                "vector_rank": vec_rank.get(cid),
                "bm25_rank": bm25_rank.get(cid),
            })
        return hits


# 全局单例：main.py 启动时构建并刷新
rag_service = RagService()
