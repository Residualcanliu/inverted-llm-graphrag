"""三条链路，统一接口。

    doc_rag   ① 文档模式      源数据渲染成文本 → 分块 → 向量检索 → 大模型读片段回答
    naive     ② 朴素实现      同一个模型，不加 few-shot / 校验 / 自修复
    inverted  ③ 倒置LLM 增强  本项目的实现

对比的意义：

    ① vs ③   从文档模式提升多少 —— 同样的信息，扁平文本 vs 图结构
    ② vs ③   工程化值多少钱 —— 同样的图和模型，加不加 prompt 工程和校验层
"""

from __future__ import annotations

from app.pipelines.base import Answer, Pipeline
from app.pipelines.doc_rag import DocRagPipeline
from app.pipelines.inverted import InvertedPipeline
from app.pipelines.naive import NaivePipeline

PIPELINES: dict[str, type[Pipeline]] = {
    "doc_rag": DocRagPipeline,
    "naive": NaivePipeline,
    "inverted": InvertedPipeline,
}


def get(name: str, **kw) -> Pipeline:
    if name == "doc_rag_full":
        return DocRagPipeline(full_context=True, **kw)
    return PIPELINES[name](**kw)


def all_pipelines(with_cheat: bool = True, **kw) -> list[Pipeline]:
    """with_cheat 加上 ①' 全语料版。

    它是「作弊版」对照：不做检索，把全部语料塞进上下文。用来分离
    「检索丢失」和「算不出来」这两个失败原因 —— 我们的语料只有约 18K token，
    装得下，所以这个对照在这里特别有意义。
    """
    out = [cls(**kw) for cls in PIPELINES.values()]
    if with_cheat:
        out.append(DocRagPipeline(full_context=True, **kw))
    return out


__all__ = ["Answer", "Pipeline", "PIPELINES", "get", "all_pipelines",
           "DocRagPipeline", "NaivePipeline", "InvertedPipeline"]
