"""建图：把结构化数据写成 Cypher 批次。

全部用 MERGE 不用 CREATE，脚本可以反复跑。批量写用 UNWIND，比逐条快一个量级。

每个批次都带 expected —— 预期写入的行数。对账（reconcile.py）拿它跟库里实际数量比，
对不上说明这一批有问题。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Batch:
    name: str
    cypher: str
    params: dict
    expected: int          # 预期影响的行数，对账用
    note: str = ""


@dataclass
class GraphData:
    clean: dict = field(default_factory=dict)
    synonyms: dict = field(default_factory=dict)
    causes: list = field(default_factory=list)


def load(clean_dir: Path, synonyms_path: Path, causes_path: Path) -> GraphData:
    def _j(p, default):
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default

    clean = {}
    for f in clean_dir.glob("*.json"):
        if f.name == "baseline.json":
            continue
        clean[f.stem] = _j(f, [])

    syn = _j(synonyms_path, {})
    cau = _j(causes_path, {}).get("links", [])
    return GraphData(clean=clean, synonyms=syn, causes=cau)


# ---------------- 归一 ----------------

def apply_map(name: str, table: dict) -> str:
    """按别名表合并名字。表是「别名 → 主名」。"""
    return table.get(name, name)


def merge_ids(syn_table: dict) -> dict[str, str]:
    """备件按 ID 合并时，别名 ID → 主 ID。"""
    return dict(syn_table or {})


# ---------------- 各批次 ----------------

def location_batches(d: GraphData) -> list[Batch]:
    eq = d.clean.get("equipment", [])
    sp = d.clean.get("spare_parts", [])
    shops = sorted({e["shop"] for e in eq if e["shop"]})
    shelves = sorted({p["location"] for p in sp if p["location"]})
    rows = ([{"name": s, "kind": "车间"} for s in shops]
            + [{"name": s, "kind": "货架"} for s in shelves])
    return [Batch(
        name="Location",
        cypher="UNWIND $rows AS r MERGE (l:Location {name: r.name}) SET l.kind = r.kind",
        params={"rows": rows}, expected=len(rows))]


def equipment_batches(d: GraphData) -> list[Batch]:
    eq = d.clean.get("equipment", [])
    rows = [{"id": e["id"], "model": e["model"], "function": e["function"],
             "shop": e["shop"], "inspected": e["inspected"],
             "priority": e["priority"],
             "common_tools": e.get("uses_common_tools", False)} for e in eq]
    out = [Batch(
        name="Equipment",
        cypher="""UNWIND $rows AS r
                  MERGE (e:Equipment {id: r.id})
                  SET e.model=r.model, e.function=r.function, e.shop=r.shop,
                      e.inspected=r.inspected, e.priority=r.priority,
                      e.common_tools=r.common_tools""",
        params={"rows": rows}, expected=len(rows))]

    links = [{"eid": e["id"], "shop": e["shop"]} for e in eq if e["shop"]]
    out.append(Batch(
        name="Equipment-IN_SHOP->Location",
        cypher="""UNWIND $rows AS r
                  MATCH (e:Equipment {id: r.eid}), (l:Location {name: r.shop})
                  MERGE (e)-[:IN_SHOP]->(l)""",
        params={"rows": links}, expected=len(links)))
    return out


def spare_batches(d: GraphData) -> list[Batch]:
    sp = d.clean.get("spare_parts", [])
    idmap = merge_ids(d.synonyms.get("备件工具", {}))

    # 被合并掉的条目不建节点，它的边改指向主条目
    alive = [p for p in sp if p["id"] not in idmap]

    def main_id(pid: str) -> str:
        return idmap.get(pid, pid)

    def mid(name: str) -> str:
        """备件名不做名称归一，只做 ID 归一 —— 同名但不同物的情况占多数。"""
        return name

    rows = [{"id": p["id"], "name": mid(p["name"]), "spec": p["spec"],
             "category": p["category"], "stock": p["stock"],
             "safety_stock": p["safety_stock"], "note": p["note"],
             "hazard": p.get("hazard", False),
             "consumable": p.get("consumable", False)} for p in alive]
    out = [Batch(
        name="SparePart",
        cypher="""UNWIND $rows AS r
                  MERGE (p:SparePart {id: r.id})
                  SET p.name=r.name, p.spec=r.spec, p.category=r.category,
                      p.stock=r.stock, p.safety_stock=r.safety_stock,
                      p.note=r.note, p.hazard=r.hazard,
                      p.consumable=r.consumable""",
        params={"rows": rows}, expected=len(rows))]

    by_id = {p["id"]: p for p in sp}
    stored = [{"pid": p["id"], "loc": p["location"]}
              for p in alive if p["location"]]
    out.append(Batch(
        name="SparePart-STORED_AT->Location",
        cypher="""UNWIND $rows AS r
                  MATCH (p:SparePart {id: r.pid}), (l:Location {name: r.loc})
                  MERGE (p)-[:STORED_AT]->(l)""",
        params={"rows": stored}, expected=len(stored)))

    # 备件/工具 → 适配设备。fits_models 是型号前缀，可能是 ['*']
    eq = d.clean.get("equipment", [])
    by_model: dict[str, list[str]] = {}
    for e in eq:
        by_model.setdefault(e["model"], []).append(e["id"])
    fits = []
    for p in alive:
        targets: set[str] = set()
        for m in p["fits_models"]:
            if m == "*":
                targets |= {x["id"] for x in eq}
            else:
                targets |= set(by_model.get(m, []))
        for t in targets:
            fits.append({"pid": p["id"], "eid": t})
    out.append(Batch(
        name="Equipment-FITS->SparePart",
        cypher="""UNWIND $rows AS r
                  MATCH (e:Equipment {id: r.eid}), (p:SparePart {id: r.pid})
                  MERGE (e)-[:FITS]->(p)""",
        params={"rows": fits}, expected=len(fits)))
    return out


def work_order_batches(d: GraphData) -> list[Batch]:
    rp = d.clean.get("repairs", [])
    syn_f = d.synonyms.get("故障模式", {})
    idmap = merge_ids(d.synonyms.get("备件工具", {}))
    by_id = {p["id"]: p for p in d.clean.get("spare_parts", [])}
    by_name = {}
    for p in d.clean.get("spare_parts", []):
        by_name.setdefault(p["name"], []).append(p["id"])

    # 故障模式有两个来源，都要建节点。
    #
    # 只建检修记录里的那些会漏 —— 实测有一例：「工件尺寸超差」只在手册里出现，
    # 检修表里没有。漏掉之后手册的应急处理连不上任何节点，成了孤儿。
    fail_sources: dict[str, str] = {}
    for m in d.clean.get("manuals", []):
        for f in m["failures"]:
            if f.get("symptom"):
                fail_sources.setdefault(apply_map(f["symptom"], syn_f), "故障手册")

    # 用「设备-日期-序号」做工单编号
    seen: dict[tuple, int] = {}
    orders, exp_edges = [], []
    used_parts, used_tools = [], []
    for r in rp:
        key = (r["device"], r["date"])
        seen[key] = seen.get(key, 0) + 1
        wid = f"WO-{r['device']}-{r['date']}-{seen[key]}"
        orders.append({"id": wid, "date": r["date"], "done": r["done"],
                       "device": r["device"], "failure": r["failure"]})
        if r["failure"]:
            fname = apply_map(r["failure"], syn_f)
            fail_sources.setdefault(fname, "检修记录")
            exp_edges.append({"wid": wid, "fname": fname})
        # 更换配件按名称匹配到备件 ID
        for pname in r.get("parts", []):
            for pid in by_name.get(pname, []):
                used_parts.append({"wid": wid, "pid": main_ok(pid, idmap)})
        for tname in r.get("tools", []):
            for pid in by_name.get(tname, []):
                used_tools.append({"wid": wid, "pid": main_ok(pid, idmap)})

    out = [
        Batch(name="WorkOrder",
              cypher="""UNWIND $rows AS r
                        MERGE (w:WorkOrder {id: r.id})
                        SET w.date=r.date, w.done=r.done""",
              params={"rows": orders}, expected=len(orders)),
        Batch(name="Equipment-EXPERIENCED->WorkOrder",
              cypher="""UNWIND $rows AS r
                        MATCH (e:Equipment {id: r.device}), (w:WorkOrder {id: r.wid})
                        MERGE (e)-[:EXPERIENCED]->(w)""",
              params={"rows": [{"device": o["device"], "wid": o["id"]} for o in orders]},
              expected=len(orders)),
        Batch(name="FailureMode",
              cypher="""UNWIND $rows AS r
                        MERGE (f:FailureMode {name: r.name})
                        SET f.source = coalesce(f.source, r.source)""",
              params={"rows": [{"name": n, "source": src}
                               for n, src in sorted(fail_sources.items())]},
              expected=len(fail_sources)),
        Batch(name="WorkOrder-CAUSED_BY->FailureMode",
              cypher="""UNWIND $rows AS r
                        MATCH (w:WorkOrder {id: r.wid}), (f:FailureMode {name: r.fname})
                        MERGE (w)-[:CAUSED_BY]->(f)""",
              params={"rows": exp_edges}, expected=len(exp_edges)),
        Batch(name="WorkOrder-USED_PART->SparePart",
              cypher="""UNWIND $rows AS r
                        MATCH (w:WorkOrder {id: r.wid}), (p:SparePart {id: r.pid})
                        MERGE (w)-[:USED_PART]->(p)""",
              params={"rows": used_parts + used_tools},
              expected=len(used_parts) + len(used_tools)),
    ]
    return out


def main_ok(pid: str, idmap: dict) -> str:
    return idmap.get(pid, pid)


def process_batches(d: GraphData) -> list[Batch]:
    flows = d.clean.get("processes", [])
    procs, ops, steps, prec, uses, depends = [], [], [], [], [], []
    for f in flows:
        procs.append({"id": f["id"], "name": f["name"],
                      "part_type": f.get("part_type", "")})
        prev = None
        for s in f["steps"]:
            oid = f"{f['id']}-{s['order']}"
            ops.append({"id": oid, "name": s["operation"], "order": s["order"]})
            steps.append({"pid": f["id"], "oid": oid})
            if prev:
                prec.append({"a": prev, "b": oid})
            for dev in s["devices"]:
                uses.append({"oid": oid, "eid": dev})
            if prev:
                # 相邻工序组两两相连 = 依赖边
                #
                # **方向按「谁依赖谁」写**：DEPENDS_ON 的字面语义是
                # `(A)-[:DEPENDS_ON]->(B)` 读作「A 依赖 B」。
                # 下游工序消耗上游的来料，所以是**下游依赖上游**，
                # 边应该从下游指向上游。
                #
                # 踩过的坑：最初写成 (上游)-[:DEPENDS_ON]->(下游)，
                # 读起来成了「上游依赖下游」，语义反了。
                # 后果是问「X 停机影响谁」返回的是 X 的上游，
                # 而正确答案是下游 —— 21 台设备全错，但校验器抓不到
                # （DEPENDS_ON 两端都是 Equipment，自反关系任何方向都合法）。
                prev_devs = next(x for x in f["steps"]
                                 if f"{f['id']}-{x['order']}" == prev)["devices"]
                for up in prev_devs:            # 上游
                    for down in s["devices"]:   # 下游
                        if up != down:
                            depends.append({"a": down, "b": up})
            prev = oid

    return [
        Batch(name="Process",
              cypher="""UNWIND $rows AS r
                        MERGE (p:Process {id: r.id})
                        SET p.name=r.name, p.part_type=r.part_type""",
              params={"rows": procs}, expected=len(procs)),
        Batch(name="Operation",
              cypher="""UNWIND $rows AS r
                        MERGE (o:Operation {id: r.id})
                        SET o.name=r.name, o.order=r.order""",
              params={"rows": ops}, expected=len(ops)),
        Batch(name="Process-HAS_STEP->Operation",
              cypher="""UNWIND $rows AS r
                        MATCH (p:Process {id: r.pid}), (o:Operation {id: r.oid})
                        MERGE (p)-[:HAS_STEP]->(o)""",
              params={"rows": steps}, expected=len(steps)),
        Batch(name="Operation-PRECEDES->Operation",
              cypher="""UNWIND $rows AS r
                        MATCH (a:Operation {id: r.a}), (b:Operation {id: r.b})
                        MERGE (a)-[:PRECEDES]->(b)""",
              params={"rows": prec}, expected=len(prec)),
        Batch(name="Operation-USES->Equipment",
              cypher="""UNWIND $rows AS r
                        MATCH (o:Operation {id: r.oid}), (e:Equipment {id: r.eid})
                        MERGE (o)-[:USES]->(e)""",
              params={"rows": uses}, expected=len(uses)),
        Batch(name="Equipment-DEPENDS_ON->Equipment",
              cypher="""UNWIND $rows AS r
                        MATCH (a:Equipment {id: r.a}), (b:Equipment {id: r.b})
                        MERGE (a)-[:DEPENDS_ON]->(b)""",
              params={"rows": depends}, expected=len(depends)),
    ]


def safety_batches(d: GraphData) -> list[Batch]:
    """故障手册的应急处理列，作为安全措施，连到对应故障。

    Hazard 没有单独建节点。「高危」是备件的一个属性，不是独立实体，
    建节点会让 schema 多一层且查询要绕路。
    """
    man = d.clean.get("manuals", [])
    syn_f = d.synonyms.get("故障模式", {})
    measures, links = {}, []
    for m in man:
        for f in m["failures"]:
            act = f.get("action", "").strip()
            if not act:
                continue
            measures[act] = True
            links.append({"fname": apply_map(f["symptom"], syn_f), "measure": act})
    rows = [{"name": k} for k in sorted(measures)]
    return [
        Batch(name="SafetyMeasure",
              cypher="""UNWIND $rows AS r
                        MERGE (s:SafetyMeasure {name: r.name})""",
              params={"rows": rows}, expected=len(rows)),
        Batch(name="FailureMode-MITIGATED_BY->SafetyMeasure",
              cypher="""UNWIND $rows AS r
                        MATCH (f:FailureMode {name: r.fname}),
                              (s:SafetyMeasure {name: r.measure})
                        MERGE (f)-[:MITIGATED_BY]->(s)""",
              params={"rows": links}, expected=len(links)),
    ]


def cause_batches(d: GraphData) -> list[Batch]:
    """因果链。成因也要建成 FailureMode 节点，两端都是故障。"""
    links = d.causes or []
    syn_f = d.synonyms.get("故障模式", {})
    names = set()
    rows = []
    for l in links:
        c = apply_map(l["cause"], syn_f)
        e = apply_map(l["effect"], syn_f)
        if c == e:
            continue
        names |= {c, e}
        rows.append({"cause": c, "effect": e})
    return [
        Batch(name="FailureMode(成因)",
              cypher="""UNWIND $rows AS r
                        MERGE (f:FailureMode {name: r.name})
                        SET f.source = coalesce(f.source, '手册成因列')""",
              params={"rows": [{"name": n} for n in sorted(names)]},
              expected=len(names)),
        Batch(name="FailureMode-TRIGGERS->FailureMode",
              cypher="""UNWIND $rows AS r
                        MATCH (a:FailureMode {name: r.cause}),
                              (b:FailureMode {name: r.effect})
                        MERGE (a)-[:TRIGGERS]->(b)""",
              params={"rows": rows}, expected=len(rows)),
    ]


def all_batches(d: GraphData) -> list[Batch]:
    return (location_batches(d) + equipment_batches(d) + spare_batches(d)
            + work_order_batches(d) + process_batches(d)
            + safety_batches(d) + cause_batches(d))
