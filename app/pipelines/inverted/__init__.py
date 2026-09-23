"""③ 倒置LLM 增强版。

大模型只把自然语言翻译成 Cypher，取数和计算由图引擎做。链路上带四道静态校验、
EXPLAIN 预检、自修复循环（≤2 轮），执行走 READ_ACCESS 模式（服务端强制只读）。

这是本项目的主体实现。包一层统一接口，让评测循环能跟另外两条链路一样调它。
"""

from __future__ import annotations

import json

from app.llm import text2cypher
from app.pipelines.base import Answer, Pipeline


def _to_text(rows: list, columns: list[str]) -> str:
    """把查询结果念成一句自然语言。

    图链路的答案本来就在结果集里，这里只是为了让「回答」这个字段
    三条链路的形态一致，方便并排看。
    """
    if not rows:
        return "没有查到匹配的数据。"
    if len(rows) == 1 and len(columns) == 1:
        return str(rows[0][columns[0]])
    return "；".join(
        "，".join(f"{k}={v}" for k, v in r.items()) for r in rows[:5]
    ) + (f"（共 {len(rows)} 条）" if len(rows) > 5 else "")


class InvertedPipeline(Pipeline):
    name = "inverted"
    label = "③ 倒置LLM 增强版"

    def __init__(self, use_examples: bool = True,
                 use_direction_hints: bool = True, seed: int | None = None):
        self.use_examples = use_examples
        self.use_direction_hints = use_direction_hints
        self.seed = seed

    def answer(self, question: str, **kw) -> Answer:
        try:
            t = text2cypher.query(
                question,
                use_examples=self.use_examples,
                use_direction_hints=self.use_direction_hints,
                seed=self.seed,
                source="eval",
            )
        except Exception as e:                        # noqa: BLE001
            return Answer(pipeline=self.name, question=question,
                          error=f"{type(e).__name__}: {e}")

        # fields 直接就是查询结果 —— 图链路的答案天生是结构化的，
        # 不需要事后抽取，这是它相对文档模式的一个便宜之处。
        fields = {"rows": t.sample_rows, "row_count": t.row_count}
        cols = list(t.sample_rows[0].keys()) if t.sample_rows else []
        return Answer(
            pipeline=self.name,
            question=question,
            text=_to_text(t.sample_rows, cols),
            fields=fields,
            cypher=t.cypher,
            raw=t.raw_output,
            trace_id=t.run_id,
            latency_ms=t.total_ms,
            rows=t.sample_rows,
            validation_ok=t.validation_ok,
            error="" if t.exec_ok else (t.exec_error or t.outcome),
        )
