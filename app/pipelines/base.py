"""三条链路的统一接口。

对照实验里三条链路喂同一份源数据，区别只在数据怎么组织、怎么取。为了让评测循环
能一视同仁地跑它们，三条都实现同一个 `answer()`。

    doc_rag   ① 文档模式      源数据渲染成文本 → 分块 → 向量检索 → 大模型读片段回答
    official  ② 官方 naive    neo4j-graphrag 的 Text2CypherRetriever，零工程
    inverted  ③ 倒置LLM 增强   本项目的实现：few-shot + 校验层 + 自修复

② 和 ③ 的差距度量「工程化值多少钱」，① 和 ③ 的差距就是「从文档模式提升多少」。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Answer:
    """一条链路对一道题的回答。字段含义对三条链路一致，评测才能统一处理。"""

    pipeline: str                       # 链路名
    question: str
    text: str = ""                      # 自然语言答案（文档模式是散文，图链路是把结果念出来）
    fields: dict = field(default_factory=dict)   # 结构化答案，能直接比对的形态

    cypher: str = ""                    # 图链路才有：生成的查询
    raw: str = ""                       # 模型原始输出，排查用
    trace_id: str = ""

    latency_ms: int = 0
    error: str = ""

    # 图链路的执行信息
    rows: list = field(default_factory=list)
    validation_ok: bool | None = None   # 只有 ③ 有校验层，①② 是 None

    @property
    def ok(self) -> bool:
        return not self.error


class Pipeline(ABC):
    """三条链路都实现这个。"""

    name: str = "base"
    label: str = ""                     # 中文名，报告里用

    @abstractmethod
    def answer(self, question: str, **kw) -> Answer:
        ...

    def warmup(self) -> None:
        """可选的预热，比如先加载模型、建好索引。"""
