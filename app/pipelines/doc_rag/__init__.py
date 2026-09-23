"""① 文档模式：传统 RAG。

源数据渲染成文本 → 分块 → bge-m3 嵌入 → 向量检索 top-k → 大模型读片段回答。

这是对照实验的基线，也是「从文档模式提升多少」里的「文档模式」。

索引落在 data/doc_index/，建一次就能反复用。语料只有几百块，用 numpy 手算余弦
就够了，不引入向量库。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from app import config
from app.llm import ollama_client as oc
from app.pipelines.base import Answer, Pipeline
from app.pipelines.doc_rag.render import build_documents

INDEX_DIR = config.DATA_DIR / "doc_index"

ANSWER_PROMPT = """下面是从工业设备档案里检索到的片段。根据这些片段回答问题。

{context}

问题：{question}

要求：
- 只根据上面的片段回答，不要编造
- 如果片段里没有答案，直接说「资料中没有相关信息」
- 答案尽量简短，直接给出问的东西"""


def build_index(force: bool = False, chunk_size: int = 400) -> dict:
    """建向量索引。已存在且不强制就复用。"""
    vec_p, meta_p = INDEX_DIR / "vectors.npy", INDEX_DIR / "chunks.json"
    if vec_p.exists() and meta_p.exists() and not force:
        return {"reused": True,
                "chunks": len(json.loads(meta_p.read_text(encoding="utf-8")))}

    chunks = build_documents(config.DATA_DIR / "clean", chunk_size)
    texts = [c["text"] for c in chunks]
    vecs = np.asarray(oc.embed(texts), dtype=np.float32)
    # 归一化，之后点积就是余弦相似度
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vecs = vecs / np.maximum(norms, 1e-9)

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    np.save(vec_p, vecs)
    meta_p.write_text(json.dumps(chunks, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return {"reused": False, "chunks": len(chunks)}


class DocRagPipeline(Pipeline):
    name = "doc_rag"
    label = "① 文档模式（传统 RAG，top-k 检索）"

    def __init__(self, top_k: int | None = None, model: str | None = None,
                 seed: int | None = None, full_context: bool = False):
        self.top_k = top_k or config.TOP_K
        self.model = model
        self.seed = seed
        self.full_context = full_context
        if full_context:
            self.name = "doc_rag_full"
            self.label = "①' 文档模式（全语料塞进上下文）"
        self._vecs: np.ndarray | None = None
        self._chunks: list[dict] | None = None

    def warmup(self) -> None:
        build_index()
        self._load()

    def _load(self) -> None:
        if self._vecs is not None:
            return
        vec_p, meta_p = INDEX_DIR / "vectors.npy", INDEX_DIR / "chunks.json"
        if not vec_p.exists():
            build_index()
        self._vecs = np.load(vec_p)
        self._chunks = json.loads(meta_p.read_text(encoding="utf-8"))

    def _retrieve(self, question: str, k: int) -> list[dict]:
        self._load()
        q = np.asarray(oc.embed([question])[0], dtype=np.float32)
        q = q / max(float(np.linalg.norm(q)), 1e-9)
        # 用 einsum 不用 @：这台机器上 numpy 的 BLAS 后端在矩阵乘法上会静默崩溃
        # （同一个坑在别的项目里踩过，进程无输出直接退出）。einsum 走自己的循环，
        # 不经过 BLAS，几百个向量也就几毫秒。
        sims = np.einsum("ij,j->i", self._vecs, q)
        idx = np.argsort(-sims)[:k]
        return [{**self._chunks[i], "score": float(sims[i])} for i in idx]

    def answer(self, question: str, **kw) -> Answer:
        t0 = time.time()
        try:
            if self.full_context:
                # 作弊版：不做检索，把全部语料塞进去。
                # 我们的语料只有约 18K token，装得下 —— 这正好把「信息完整性」
                # 这个变量彻底消除。如果这样它在图计算类问题上仍然答不对，
                # 就证明瓶颈是「算不出来」而不是「看不到」。
                self._load()
                hits = self._chunks
            else:
                hits = self._retrieve(question, self.top_k)
            context = "\n\n".join(h["text"] for h in hits)
            prompt = ANSWER_PROMPT.format(context=context, question=question)
            g = oc.generate(prompt, model=self.model, seed=self.seed,
                            num_predict=512)
        except Exception as e:                        # noqa: BLE001
            return Answer(pipeline=self.name, question=question,
                          error=f"{type(e).__name__}: {e}")

        return Answer(
            pipeline=self.name,
            question=question,
            text=g.plain,   # 用 plain：文档模式给的是自然语言，不是 Cypher
            # fields 留空：文档模式给的是散文，要抽成结构化字段得靠评测环节的
            # 抽取步骤。这个抽取本身会引入误差，所以单独统计，不混进这里。
            fields={},
            raw=g.raw,
            latency_ms=int((time.time() - t0) * 1000),
            rows=[{"chunk": h["id"], "source": h["source"],
                   "score": round(h.get("score", 1.0), 4)} for h in hits],
        )
