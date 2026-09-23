"""② 朴素实现：同一个模型，不做任何工程化。

和 ③ 倒置LLM 增强版的**唯一区别**是：这里不加 few-shot 示例、不加方向提示、
不做静态校验、没有自修复。生成完直接执行，失败就失败。

对照实验要的是「工程化值多少钱」，所以两边必须只差工程化：

    ③ 增强版   schema + 方向提示 + few-shot  →  四道静态校验  →  EXPLAIN  →  执行  →  失败则修复（≤2 轮）
    ② 朴素版   schema                        →                            执行

**不用 neo4j-graphrag 官方包**（原计划用它）。原因：那个包要求 numpy>=2，
装它会把本机的 pandas 和 streamlit 一起带崩。而且官方包自带 prompt 模板，
会引入实现差异——差异就不纯粹是「工程化」了。自己写反而更干净。
"""

from __future__ import annotations

from app.graph import client
from app.llm import ollama_client as oc
from app.pipelines.base import Answer, Pipeline

# 朴素 prompt：只给 schema 和问题。
PLAIN_PROMPT = """根据下面的图谱 schema，把问题翻译成一条 Cypher 查询。

{schema}

问题：{question}

Cypher："""


class NaivePipeline(Pipeline):
    name = "naive"
    label = "② 朴素实现（无工程化）"

    def __init__(self, model: str | None = None, seed: int | None = None):
        self.model = model
        self.seed = seed

    def answer(self, question: str, **kw) -> Answer:
        from app.graph import schema as schema_mod
        try:
            sc = schema_mod.get()
            prompt = PLAIN_PROMPT.format(schema=sc.render_for_prompt(),
                                         question=question)
            g = oc.generate(prompt, model=self.model, seed=self.seed)
        except Exception as e:                        # noqa: BLE001
            return Answer(pipeline=self.name, question=question,
                          error=f"{type(e).__name__}: {e}")

        ans = Answer(
            pipeline=self.name,
            question=question,
            cypher=g.text,
            raw=g.raw,
            latency_ms=int(g.elapsed_s * 1000),
        )

        if not g.text.strip():
            ans.error = "模型没有输出可识别的查询"
            return ans

        # 不校验，直接执行。执行失败就记下来，不重试
        try:
            r = client.run_readonly(g.text)
        except Exception as e:                        # noqa: BLE001
            ans.error = f"{type(e).__name__}: {str(e)[:200]}"
            return ans

        ans.rows = r.rows[:200]
        ans.latency_ms += r.elapsed_ms
        ans.fields = {"rows": r.rows[:200], "row_count": len(r.rows)}
        if len(r.rows) == 1 and len(r.columns) == 1:
            ans.text = str(r.rows[0][r.columns[0]])
        else:
            cols = r.columns
            ans.text = ("；".join("，".join(f"{k}={row[k]}" for k in cols)
                                 for row in r.rows[:5])
                        + (f"（共 {len(r.rows)} 条）" if len(r.rows) > 5 else ""))
        if not r.rows:
            ans.text = "没有查到匹配的数据。"
        return ans
