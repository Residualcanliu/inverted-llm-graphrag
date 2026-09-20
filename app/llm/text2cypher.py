"""倒置LLM 主链路。

    问题 → 组装 prompt → LLM 生成 Cypher → 静态校验 → EXPLAIN 预检
                                                          ↓ 不通过
                                                     自修复（≤2 轮）
                                                          ↓
                                              READ_ACCESS 执行 → 返回结果

每一轮都写进 trace（见 app/llm/trace.py），可追溯。

**这个模块和校验层各自能保证什么**

静态校验和 EXPLAIN 抓的是「结构不合法」：不存在的标签/关系/属性、方向写反、
语法错误。它们**抓不到语义错误**。

这不是理论担忧，是实测过的：bench 里有一道「更换3号泵的机械密封需要什么资质？」，
模型有时会把「机械密封」当成 SparePart 走备件→供应商那条链，答成「哪家供应商」。
那条查询语法全对、三道校验全过、EXPLAIN 通过、能执行、返回非空结果 —— 只是答错了。

所以这里的定位是「不让坏查询进库」，不是「保证答案对」。答案对不对要靠评测。
"""

from __future__ import annotations

import time

from app import config
from app.graph import client
from app.graph.validate import validate
from app.llm import ollama_client as oc
from app.llm import prompt as P
from app.llm.trace import QueryTrace, append

# 修复时额外追加的指示。
#
# 两点都要说清楚，缺一不可：
#   1. 把错误原文给全 —— 只给「语法错误」这种代号，模型不知道错在哪，
#      会原样重写一遍（实测连写两次，两轮修复全白费）
#   2. 明确要求换个写法 —— 否则模型倾向于复述上一个答案
REPAIR_HINT = """你上一次生成的查询有问题，请重新生成一条。

问题：{question}

你上一次写的是：
{cypher}

报错内容：
{feedback}

要求：
- 不要原样重复上一次的写法。上次的写法本身有问题，照抄没有意义
- 针对上面报的错做具体修正
- 只输出查询语句本身，不要解释"""


def _prompt_variant(use_examples: bool, use_direction_hints: bool) -> str:
    parts = []
    parts.append("fewshot" if use_examples else "no_fewshot")
    parts.append("direction" if use_direction_hints else "no_direction")
    return "+".join(parts)


def query(
    question: str,
    *,
    model: str | None = None,
    max_repairs: int | None = None,
    use_examples: bool = True,
    use_direction_hints: bool = True,
    source: str = "manual",
    seed: int | None = None,
    dry_run: bool = False,
) -> QueryTrace:
    """跑一条完整的倒置LLM 查询。

    dry_run=True 时只生成和校验，不连库执行（Neo4j 没起来时也能用）。
    """
    max_repairs = config.MAX_REPAIR_ROUNDS if max_repairs is None else max_repairs
    model = model or config.GEN_MODEL
    t_start = time.time()

    trace = QueryTrace(
        question=question,
        source=source,
        model=model,
        prompt_variant=_prompt_variant(use_examples, use_direction_hints),
    )

    prompt = P.build_prompt(
        question,
        with_examples=use_examples,
        with_direction_hints=use_direction_hints,
    )
    trace.prompt_chars = len(prompt)

    def _generate(p: str) -> bool:
        """生成一轮，写进 trace。返回是否成功拿到非空 Cypher。"""
        g = oc.generate(p, model=model, seed=seed)
        trace.prompt_tokens = g.prompt_eval_count
        trace.cypher = g.text
        trace.raw_output = g.raw
        trace.gen_elapsed_s = g.elapsed_s
        trace.gen_tok_per_s = g.tok_per_s
        trace.truncated = g.truncated
        trace.load_duration_s = g.load_duration_s
        trace.prefill_s = g.prompt_eval_duration_s
        trace.eval_s = g.eval_duration_s
        trace.eval_count = g.eval_count
        return bool(g.text.strip())

    outcome = ""

    for attempt in range(max_repairs + 1):
        if not _generate(prompt):
            outcome = "empty_output"
            break

        # 静态校验：只读拦截 / schema 一致性 / 关系方向
        v = validate(trace.cypher)
        trace.validation_ok = v.ok
        trace.validation_issues = [f"[{i.kind}] {i.detail}" for i in v.issues]

        if not v.ok:
            feedback = "；".join(f"{i.detail}" for i in v.errors)
            if attempt < max_repairs:
                trace.repairs.append({
                    "round": attempt + 1, "reason": "静态校验不通过",
                    "feedback": feedback, "cypher": trace.cypher,
                })
                prompt = REPAIR_HINT.format(
                    question=question, cypher=trace.cypher, feedback=feedback)
                continue
            outcome = "validation_failed"
            break

        if dry_run:
            outcome = "validated_only"
            break

        # EXPLAIN 预检：不执行就验语法
        ex_ok, ex_err = client.explain(trace.cypher)
        trace.explain_ok = ex_ok
        trace.explain_error = ex_err
        if not ex_ok:
            if attempt < max_repairs:
                trace.repairs.append({
                    "round": attempt + 1, "reason": "EXPLAIN 不通过",
                    "feedback": ex_err, "cypher": trace.cypher,
                })
                prompt = REPAIR_HINT.format(
                    question=question, cypher=trace.cypher, feedback=ex_err)
                continue
            outcome = "explain_failed"
            break

        # READ_ACCESS 执行。写操作在这一层被服务端拒绝
        trace.executed = True
        try:
            r = client.run_readonly(trace.cypher)
            trace.exec_ok = True
            trace.row_count = len(r.rows)
            trace.exec_ms = r.elapsed_ms
            trace.sample_rows = r.rows[:5]
            outcome = "answered"
        except Exception as e:                        # noqa: BLE001
            trace.exec_ok = False
            trace.exec_error = f"{type(e).__name__}: {e}"
            if attempt < max_repairs:
                trace.repairs.append({
                    "round": attempt + 1, "reason": "执行失败",
                    "feedback": trace.exec_error, "cypher": trace.cypher,
                })
                prompt = REPAIR_HINT.format(
                    question=question, cypher=trace.cypher,
                    feedback=trace.exec_error)
                continue
            outcome = "exec_failed"
        break

    if not outcome:
        outcome = "repair_exhausted"
    trace.total_ms = int((time.time() - t_start) * 1000)
    trace.finish(outcome)
    append(trace)
    return trace


def batch(questions: list[str], **kw) -> list[QueryTrace]:
    """批量跑。评测用。"""
    out = []
    for i, q in enumerate(questions, 1):
        t = query(q, **kw)
        out.append(t)
        print(f"  [{i}/{len(questions)}] {t.outcome:<18} {t.total_ms:>5}ms  {q[:36]}")
    return out
