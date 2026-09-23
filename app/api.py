"""FastAPI 后端。

    uvicorn app.api:app --reload

接口分两组：查询链路（倒置LLM）和数据管线（规范化→归一→建图→对账）。

管线那组的写操作只有一处：/pipeline/resolve，把人工确认结果写回归一表。
其余全是只读。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app import config
from app.graph import client, schema as schema_mod
from app.llm import text2cypher

# 控制台日志。trace（logs/query_trace.jsonl）记的是完整链路，供事后追溯；
# 这里记的是关键节点，供实时观察 —— 界面卡住或答错时，先看控制台。
#
# 两个 Windows 上的坑：
#   1. 默认编码是 GBK，中文会乱码 —— 显式指定 UTF-8
#   2. 日志里别用 ✓ ✗ 这类符号，会被转义成 ✓ —— 用 ASCII 的 OK / --
_handler = logging.StreamHandler(
    open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1,
         closefd=False))
_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("graphrag")

app = FastAPI(title="inverted-llm-graphrag", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

DATA = config.DATA_DIR
CLEAN = DATA / "clean"


# ---------------- 模型 ----------------

class QueryIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    use_examples: bool = True
    use_direction_hints: bool = True
    seed: int | None = None


class ResolveIn(BaseModel):
    a: str
    b: str
    action: str = Field(..., pattern="^(merge|keep|skip)$")


class ResolveAllIn(BaseModel):
    action: str = Field(..., pattern="^(merge|keep|skip)$")
    only: list[str] = []          # 限定只处理这几组，空表示全部


class ResolveBatchIn(BaseModel):
    """按组指定动作。前端「按分析结果处理」用这个。"""
    decisions: list[ResolveIn]


# ---------------- 自检 ----------------

@app.get("/health")
def health():
    ok_db, msg_db = client.ping()
    models: list[str] = []
    try:
        from app.llm import ollama_client as oc
        models = oc.list_models()
    except Exception:                                 # noqa: BLE001
        pass
    return {
        "neo4j": {"ok": ok_db, "msg": msg_db},
        "ollama": {"models": models, "gen_model": config.GEN_MODEL,
                   "available": config.GEN_MODEL in models},
        "schema": {"source": schema_mod.get().source,
                   "labels": len(schema_mod.get().labels),
                   "rels": len(schema_mod.get().rel_types)},
    }


# ---------------- 查询 ----------------

@app.post("/query")
def query(body: QueryIn):
    log.info("查询  %s", body.question)
    t0 = time.time()
    t = text2cypher.query(
        body.question,
        use_examples=body.use_examples,
        use_direction_hints=body.use_direction_hints,
        seed=body.seed,
        source="api",
    )
    d = asdict(t)
    d.pop("raw_output", None)      # 原始输出太长，前端按需单独取

    # 一行摘要，够看清这次发生了什么
    flag = []
    if not t.validation_ok:
        flag.append("校验不过")
    if t.repairs:
        flag.append(f"修复 {len(t.repairs)} 轮")
    if t.truncated:
        flag.append("截断")
    log.info(
        "  -> %-18s %-3s %4d 行 / %.2fs  (生成 %.2fs, %.1f tok/s)  %s",
        t.outcome,
        "OK" if t.exec_ok else "--",
        t.row_count,
        time.time() - t0,
        t.gen_elapsed_s,
        t.gen_tok_per_s,
        " ".join(flag) or "-",
    )
    if not t.validation_ok:
        for i in (t.validation_issues or [])[:3]:
            log.info("     校验: %s", i)
    if not t.exec_ok and t.exec_error:
        log.info("     执行: %s", t.exec_error[:150])
    return d


# ---------------- 数据管线 ----------------

@app.get("/pipeline/status")
def pipeline_status():
    def _j(name, default):
        p = DATA / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    syn = _j("synonyms.json", {})
    pend = _j("pending.json", {}).get("pending", [])
    causes = _j("causes.json", {}).get("links", [])
    expected = _j(str(Path("clean") / "expected.json"), {})

    steps = [
        {"name": "规范化", "script": "scripts/normalize.py",
         "done": (CLEAN / "equipment.json").exists(),
         "detail": _clean_summary()},
        {"name": "归一", "script": "scripts/resolve.py",
         "done": bool(syn),
         "detail": {
             "故障模式别名": len(syn.get("故障模式", {})),
             "备件工具别名": len(syn.get("备件工具", {})),
             "因果链": len(causes),
             "待确认": len(pend),
         }},
        {"name": "建图", "script": "scripts/build_graph.py",
         "done": _graph_node_count() > 0,
         "detail": {"节点": _graph_node_count(), "关系": _graph_rel_count()}},
        {"name": "对账", "script": "scripts/reconcile.py",
         "done": (config.REPORT_DIR / "reconcile.md").exists(),
         "detail": {"孤儿节点": _orphan_count()}},
    ]
    return {"steps": steps, "expected": expected}


def _clean_summary() -> dict:
    def n(name):
        p = CLEAN / name
        return len(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else 0
    return {"设备": n("equipment.json"), "备件工具": n("spare_parts.json"),
            "检修记录": n("repairs.json"), "流程": n("processes.json"),
            "手册": n("manuals.json")}


def _graph_node_count() -> int:
    try:
        return client.run_readonly("MATCH (n) RETURN count(n) AS c").rows[0]["c"]
    except Exception:                                 # noqa: BLE001
        return 0


def _graph_rel_count() -> int:
    try:
        return client.run_readonly("MATCH ()-[r]->() RETURN count(r) AS c").rows[0]["c"]
    except Exception:                                 # noqa: BLE001
        return 0


def _orphan_count() -> int:
    try:
        return client.run_readonly(
            "MATCH (n) WHERE NOT (n)--() RETURN count(n) AS c").rows[0]["c"]
    except Exception:                                 # noqa: BLE001
        return 0


@app.get("/pipeline/pending")
def pipeline_pending():
    """待确认队列，每条附上合并的影响，供人工判断。

    只给名字对是不够的 —— 判断依据是「合并后哪些边会指向同一节点」。
    """
    p = DATA / "pending.json"
    if not p.exists():
        return {"pending": []}
    raw = json.loads(p.read_text(encoding="utf-8")).get("pending", [])

    items = []
    for c in raw:
        items.append({**c, "impact": _impact(c["a"], c["b"])})
    return {"pending": items}


def _impact(a: str, b: str) -> dict:
    """两条记录各自的邻居，以及邻居的重合度。

    只给「边数」不够做判断。判断依据是**邻居集合是否重合**：
    两个节点如果连着同一批设备，很可能是同一件东西记了两遍；
    如果连的设备完全不同，多半是两回事。
    """
    def neighbors(ident: str) -> dict:
        try:
            r = client.run_readonly(
                "MATCH (n) WHERE n.id = $i OR n.name = $i "
                "OPTIONAL MATCH (n)--(m) "
                "WITH n, collect(DISTINCT coalesce(m.id, m.name)) AS nb "
                "RETURN labels(n)[0] AS label, "
                "       [x IN nb WHERE x IS NOT NULL] AS neighbors",
                {"i": ident}).rows
            return r[0] if r else {}
        except Exception:                             # noqa: BLE001
            return {}

    na, nb = neighbors(a), neighbors(b)
    sa = set(na.get("neighbors") or [])
    sb = set(nb.get("neighbors") or [])
    return {
        "a": {"label": na.get("label", ""), "degree": len(sa)},
        "b": {"label": nb.get("label", ""), "degree": len(sb)},
        "shared": len(sa & sb),
        "only_a": len(sa - sb),
        "only_b": len(sb - sa),
        "sample_shared": sorted(sa & sb)[:5],
    }


@app.post("/pipeline/resolve")
def pipeline_resolve(body: ResolveIn):
    """人工确认一组同义候选。写回归一表，需要重建图才生效。"""
    log.info("确认  %s ↔ %s  -> %s", body.a, body.b, body.action)
    r = _apply_decisions([(body.a, body.b, body.action)])
    log.info("  -> 合并 %d，剩余待确认 %d", r["merged"], r["remaining"])
    return {
        "ok": True, "action": body.action,
        "remaining": r["remaining"], "need_rebuild": r["need_rebuild"],
        "message": "已写回归一表，重建图后生效" if body.action == "merge"
                   else "已从队列移除，图谱不需要变",
    }


def _apply_decisions(decisions: list[tuple[str, str, str]]) -> dict:
    """把一批决策写回归一表。返回统计。

    抽出来是因为单条确认、批量确认、按分析结果处理三条路径都要用它，
    免得三处各写一遍写歪。
    """
    p = DATA / "synonyms.json"
    syn = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    syn.setdefault("故障模式", {})
    syn.setdefault("备件工具", {})

    pend_path = DATA / "pending.json"
    pend = json.loads(pend_path.read_text(encoding="utf-8")) if pend_path.exists() else {}
    items = pend.get("pending", [])

    done_keys, decided, merged = set(), [], 0
    for a, b, action in decisions:
        key = frozenset((a, b))
        if key in done_keys:
            continue
        src = next((c for c in items if frozenset((c["a"], c["b"])) == key), None)
        if src is None:
            continue
        done_keys.add(key)
        if action == "merge":
            main, alias = sorted([a, b], key=lambda x: (len(x), x))
            syn[_which_table(main, alias)][alias] = main
            merged += 1
        else:
            decided.append({**src, "action": action})

    p.write_text(json.dumps(syn, ensure_ascii=False, indent=2), encoding="utf-8")
    pend["pending"] = [c for c in items
                       if frozenset((c["a"], c["b"])) not in done_keys]
    if decided:
        pend.setdefault("decided", []).extend(decided)
    pend_path.write_text(json.dumps(pend, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"merged": merged, "kept": len(decided),
            "remaining": len(pend["pending"]),
            "need_rebuild": merged > 0}


@app.post("/pipeline/resolve_all")
def pipeline_resolve_all(body: ResolveAllIn):
    """一键对全部（或指定的几组）应用同一个动作。"""
    pend_path = DATA / "pending.json"
    if not pend_path.exists():
        return {"merged": 0, "kept": 0, "remaining": 0, "need_rebuild": False}
    items = json.loads(pend_path.read_text(encoding="utf-8")).get("pending", [])
    only = set(body.only)
    todo = [c for c in items if not only or c["a"] in only or c["b"] in only]
    log.info("批量确认  %s  %d 组", body.action, len(todo))
    r = _apply_decisions([(c["a"], c["b"], body.action) for c in todo])
    log.info("  -> 合并 %d，保留/跳过 %d，剩余 %d",
             r["merged"], r["kept"], r["remaining"])
    return r


@app.post("/pipeline/resolve_batch")
def pipeline_resolve_batch(body: ResolveBatchIn):
    """按组指定不同动作。"""
    log.info("按分析结果处理  %d 组", len(body.decisions))
    r = _apply_decisions([(d.a, d.b, d.action) for d in body.decisions])
    log.info("  -> 合并 %d，保留/跳过 %d，剩余 %d",
             r["merged"], r["kept"], r["remaining"])
    return r


@app.post("/pipeline/rebuild")
def pipeline_rebuild():
    """重建图谱。按新的归一表重新写一遍，MERGE 保证幂等。"""
    import subprocess
    import sys
    t0 = time.time()
    log.info("重建图谱 ...")
    # 必须 --reset。建图用的是 MERGE，只增不删 —— 归一表改小之后，
    # 被合并掉的那些节点会继续留在库里，节点数对不上。
    # 实测踩过：合并了 11 组，节点数纹丝不动，因为旧节点没清。
    # 图很小（700 节点），清空重建 2 秒，不值得做增量删除。
    r = subprocess.run([sys.executable, "scripts/build_graph.py", "--reset"],
                       capture_output=True, text=True, cwd=str(config.ROOT))
    ok = r.returncode == 0
    log.info("  -> %s  %.2fs", "成功" if ok else "失败", time.time() - t0)
    if not ok:
        log.info("     %s", (r.stderr or "")[-300:])
    return {"ok": ok, "elapsed_s": round(time.time() - t0, 2),
            "tail": (r.stdout or r.stderr).strip().split("\n")[-6:]}


def _which_table(main: str, alias: str) -> str:
    """判断这对该进哪张归一表。

    别用正则猜编号 —— 踩过：`刀架总成 -> 刀架` 是中文名，不带编号，
    被 `[A-Za-z一-龥]{1,3}-\\d{2,4}` 判成故障名，写进了「故障模式」表。
    改成查实际数据：这两个名字在备件/工具里存在，就是备件工具那一类。
    """
    p = CLEAN / "spare_parts.json"
    if p.exists():
        parts = json.loads(p.read_text(encoding="utf-8"))
        known = {x["id"] for x in parts} | {x["name"] for x in parts}
        if main in known or alias in known:
            return "备件工具"
    return "故障模式"


# ---------------- 图谱 ----------------

@app.get("/graph/stats")
def graph_stats():
    labels = client.run_readonly(
        "MATCH (n) UNWIND labels(n) AS l RETURN l AS label, count(*) AS n ORDER BY n DESC")
    rels = client.run_readonly(
        "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n ORDER BY n DESC")
    return {"nodes": labels.rows, "rels": rels.rows,
            "total_nodes": sum(x["n"] for x in labels.rows),
            "total_rels": sum(x["n"] for x in rels.rows)}


@app.get("/graph/subgraph")
def graph_subgraph(center: str, hops: int = 2, limit: int = 120):
    """取某个节点周围的子图，给前端可视化用。"""
    try:
        r = client.run_readonly(
            "MATCH (c) WHERE c.id = $c OR c.name = $c "
            "CALL apoc.path.subgraphAll(c, {maxLevel: $h, limit: $l}) "
            "YIELD nodes, relationships "
            "RETURN [n IN nodes | {id: elementId(n), label: labels(n)[0], "
            "        name: coalesce(n.id, n.name)}] AS nodes, "
            "       [x IN relationships | {from: elementId(startNode(x)), "
            "        to: elementId(endNode(x)), type: type(x)}] AS rels",
            {"c": center, "h": hops, "l": limit}).rows
        return r[0] if r else {"nodes": [], "rels": []}
    except Exception as e:                            # noqa: BLE001
        raise HTTPException(500, f"取子图失败：{e}") from e


@app.get("/graph/labels")
def graph_labels():
    """取每个标签的几个样本，前端做选择用。"""
    sc = schema_mod.get()
    out = {}
    for lb in sorted(sc.labels):
        rows = client.run_readonly(
            f"MATCH (n:`{lb}`) RETURN coalesce(n.id, n.name) AS v LIMIT 8").rows
        out[lb] = [r["v"] for r in rows]
    return out
