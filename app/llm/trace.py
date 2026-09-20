"""倒置LLM 每次查询的 trace 记录。

每条查询落一行 JSON 到 `logs/query_trace.jsonl`，包含从问题到结果的完整链路：
生成参数、原始输出、提取出的 Cypher、三道校验的结果、EXPLAIN 结果、
自修复每一轮的失败原因、执行结果、各段耗时。

**为什么要记这个**

1. **可追溯。** 任何一条查过的题都能查到：当时用的哪个模型、prompt 哪种变体、
   生成了什么、校验报了什么、重试了几次、最终对不对。
   出了问题不用猜，翻记录就行。
2. **失败可观测。** 静态校验和 EXPLAIN 都抓不到语义错误（实测过：把「停机影响谁」
   误解成故障传播，查询合法、能执行、返回非空、但答案是错的）。
   这类错误只能靠人看记录发现。所以原始输出和提取结果都要留下来。
3. **评测的原料。** 分层评测的逐条结果直接从这份记录里出，不用重跑。

JSONL 的格式：一行一条，方便 append、方便 grep、坏了也不影响前面的行。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app import config

TRACE_DIR = config.ROOT / "logs"
TRACE_FILE = TRACE_DIR / "query_trace.jsonl"


@dataclass
class QueryTrace:
    """一次倒置LLM 查询的完整记录。"""

    question: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    source: str = "manual"           # manual | bench | eval | api
    model: str = ""
    prompt_variant: str = ""          # 例：fewshot+direction / schema_only

    # 生成
    prompt_chars: int = 0
    prompt_tokens: int = 0
    cypher: str = ""
    raw_output: str = ""              # 模型原始输出（含 think 块），排查用
    gen_elapsed_s: float = 0.0        # 墙钟总耗时
    gen_tok_per_s: float = 0.0
    truncated: bool = False

    # 耗时拆解。分开记才能判断慢在哪一段：
    #   加载大   -> Ollama 闲置卸载了模型，不是模型本身慢
    #   prefill 大 -> prompt 太长
    #   生成大   -> 模型真的慢，或者 thinking 在烧 token
    load_duration_s: float = 0.0
    prefill_s: float = 0.0
    eval_s: float = 0.0
    eval_count: int = 0

    # 校验
    validation_ok: bool = False
    validation_issues: list[str] = field(default_factory=list)
    explain_ok: bool | None = None    # None = 没连库，没跑
    explain_error: str = ""

    # 自修复。每轮记下：失败原因、回喂给模型的错误、重写后的 Cypher
    repairs: list[dict] = field(default_factory=list)

    # 执行
    executed: bool = False
    exec_ok: bool = False
    exec_error: str = ""
    row_count: int = 0
    exec_ms: int = 0
    sample_rows: list = field(default_factory=list)

    # 汇总
    total_ms: int = 0
    outcome: str = ""                 # answered | validation_failed | exec_failed | repair_exhausted

    def finish(self, outcome: str) -> "QueryTrace":
        self.outcome = outcome
        if not self.total_ms:
            self.total_ms = int(self.gen_elapsed_s * 1000) + self.exec_ms
        return self


def append(trace: QueryTrace) -> Path:
    """追加一条记录。返回文件路径。"""
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    with TRACE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(trace), ensure_ascii=False) + "\n")
    return TRACE_FILE


def load_all(limit: int | None = None) -> list[dict]:
    """读回全部记录。limit 取最近 N 条。"""
    if not TRACE_FILE.exists():
        return []
    rows = []
    with TRACE_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # 单行坏了不该拖垮整个读取
                continue
    return rows[-limit:] if limit else rows


def render(t: dict | QueryTrace) -> str:
    """把一条记录渲染成人看的文本。"""
    d = asdict(t) if isinstance(t, QueryTrace) else t
    ok = "成功" if d.get("exec_ok") else ("失败" if d.get("executed") else "未执行")
    lines = [
        f"[{d.get('ts')}] {d.get('run_id')}  {d.get('outcome') or ok}",
        f"  问题      {d.get('question')}",
        f"  模型      {d.get('model')}   prompt变体: {d.get('prompt_variant')}",
        f"  生成的Cypher",
    ]
    for ln in (d.get("cypher") or "(空)").split("\n"):
        lines.append(f"      {ln}")

    v_ok = "通过" if d.get("validation_ok") else "不通过"
    lines.append(f"  静态校验  {v_ok}")
    for issue in d.get("validation_issues") or []:
        lines.append(f"      - {issue}")

    if d.get("explain_ok") is not None:
        lines.append(f"  EXPLAIN   {'通过' if d['explain_ok'] else '不通过 ' + d.get('explain_error', '')}")

    for i, r in enumerate(d.get("repairs") or [], 1):
        lines.append(f"  修复第{i}轮 原因: {r.get('reason', '')}")
        lines.append(f"      回喂错误: {str(r.get('feedback', ''))[:120]}")
        for ln in (r.get("cypher") or "").split("\n"):
            lines.append(f"      {ln}")

    if d.get("executed"):
        lines.append(f"  执行      {'成功' if d.get('exec_ok') else '失败'}  "
                     f"{d.get('row_count', 0)} 行 / {d.get('exec_ms', 0)} ms")
        if d.get("exec_error"):
            lines.append(f"      错误: {d['exec_error'][:160]}")
    seg = []
    if d.get("load_duration_s", 0) > 0.05:
        seg.append(f"加载 {d['load_duration_s']:.2f}s")
    seg.append(f"prefill {d.get('prefill_s', 0):.2f}s")
    seg.append(f"生成 {d.get('eval_s', 0):.2f}s")
    lines.append(f"  耗时      {' + '.join(seg)}  =  {d.get('gen_elapsed_s', 0):.2f}s "
                 f"({d.get('gen_tok_per_s', 0):.1f} tok/s, {d.get('eval_count', 0)} tok)")
    lines.append(f"  总计      {d.get('total_ms', 0)} ms")
    return "\n".join(lines)


def stats(rows: list[dict] | None = None) -> dict:
    """汇总。给评测报告用。"""
    rows = rows if rows is not None else load_all()
    n = len(rows)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "answered": sum(1 for r in rows if r.get("outcome") == "answered"),
        "validation_passed": sum(1 for r in rows if r.get("validation_ok")),
        "executed": sum(1 for r in rows if r.get("executed")),
        "exec_ok": sum(1 for r in rows if r.get("exec_ok")),
        "repaired": sum(1 for r in rows if r.get("repairs")),
        "truncated": sum(1 for r in rows if r.get("truncated")),
        "avg_total_ms": int(sum(r.get("total_ms", 0) for r in rows) / n),
    }
