"""用独立路径验证问题集的标准答案。

**为什么要做这件事**

标准答案是从图算的。但如果建图逻辑有错，图和参考查询会共享同一个错误假设，
两边自洽地错，互相印证，看不出问题 —— 依赖边的方向就这么错过一次：
数量 650 对、对账全过、方向全反。

所以要用**不经过图引擎**的路径重算一遍：直接从 `data/clean/*.json` 读源数据，
用纯 Python 实现同样的语义。两条独立实现给出同样的答案，才算验过。

    clean/*.json --(纯 Python 重算)--> 答案甲
    Neo4j       --(参考 Cypher)   --> 答案乙
                                      甲 == 乙 才算标准答案可信

任何一条不一致，都是需要查的信号：可能建图错了，可能参考 Cypher 错了，
也可能两边对源数据的理解都错了。
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path


class SourceGraph:
    """直接从 clean/*.json 重建关系，不经 Neo4j。

    每种关系都按「源头数据怎么说的」实现一遍，不看建图脚本怎么写的 ——
    这样才有独立性。两边都从 clean/*.json 出发，但走的是完全不同的代码路径。
    """

    def __init__(self, clean_dir: Path, synonyms_path: Path | None = None):
        def j(name):
            p = clean_dir / f"{name}.json"
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

        self.equipment = j("equipment")
        self.spare_parts = j("spare_parts")
        self.repairs = j("repairs")
        self.processes = j("processes")

        # 归一表必须应用。它是人工决策的一部分，属于「正确答案」的定义 ——
        # 没有它，被合并掉的条目（比如 B-013 并进 A-013）会在这里凭空出现，
        # 而图里早就没有它们了。这个缺陷实测抓出来过：B-013 余量 2、
        # 本该排在库存最紧张的前几名，独立重算里有、图里没有。
        self.idmap: dict[str, str] = {}
        if synonyms_path and synonyms_path.exists():
            syn = json.loads(synonyms_path.read_text(encoding="utf-8"))
            self.idmap = dict(syn.get("备件工具", {}))
        self.spare_parts = [p for p in self.spare_parts
                            if p["id"] not in self.idmap]

        self.eq_by_id = {e["id"]: e for e in self.equipment}
        self.sp_by_id = {p["id"]: p for p in self.spare_parts}

    # ---------- 依赖网络 ----------

    def depends_on(self) -> dict[str, set[str]]:
        """设备 -> 它依赖的设备（上游）。

        语义来自工艺流程：第 N 道工序的产出流向第 N+1 道工序，
        所以**下游依赖上游**。

        这里刻意按物理语义独立实现一遍，跟 loader.py 的写法无关：
        遍历每道工序，把它的上游工序组里所有设备，记成它的依赖。
        """
        deps: dict[str, set[str]] = defaultdict(set)
        for f in self.processes:
            steps = f["steps"]
            for i, step in enumerate(steps):
                if i == 0:
                    continue
                upstream = steps[i - 1]["devices"]
                for down in step["devices"]:
                    for up in upstream:
                        if up != down:
                            deps[down].add(up)
        return deps

    def downstream_of(self, eid: str) -> set[str]:
        """谁依赖 eid，即它的下游。BFS 走 5 跳。"""
        deps = self.depends_on()
        rev: dict[str, set[str]] = defaultdict(set)
        for down, ups in deps.items():
            for up in ups:
                rev[up].add(down)
        seen, q = set(), deque([(eid, 0)])
        while q:
            cur, d = q.popleft()
            if d >= 5:
                continue
            for nxt in rev.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append((nxt, d + 1))
        seen.discard(eid)
        return seen

    def upstream_of(self, eid: str) -> set[str]:
        deps = self.depends_on()
        seen, q = set(), deque([(eid, 0)])
        while q:
            cur, d = q.popleft()
            if d >= 5:
                continue
            for nxt in deps.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append((nxt, d + 1))
        seen.discard(eid)
        return seen

    def dependents_count(self, eid: str) -> int:
        return len(self.downstream_of(eid))

    # ---------- 事实 ----------

    def shop_of(self, eid: str) -> str:
        return self.eq_by_id.get(eid, {}).get("shop", "")

    def function_of(self, eid: str) -> str:
        return self.eq_by_id.get(eid, {}).get("function", "")

    def location_of(self, pid: str) -> str:
        return self.sp_by_id.get(pid, {}).get("location", "")

    def repair_count(self, eid: str) -> int:
        return sum(1 for r in self.repairs if r["device"] == eid)

    def failure_count(self, name: str) -> int:
        return sum(1 for r in self.repairs if r["failure"] == name)

    # ---------- 补集 ----------

    def undone_devices(self) -> set[str]:
        return {r["device"] for r in self.repairs if not r["done"]}

    def devices_not_in_any_process(self) -> set[str]:
        used = {d for f in self.processes for s in f["steps"] for d in s["devices"]}
        return {e["id"] for e in self.equipment} - used

    def devices_without_dependency(self) -> set[str]:
        deps = self.depends_on()
        touched = set(deps) | {u for v in deps.values() for u in v}
        return {e["id"] for e in self.equipment} - touched

    def sp_without_safety_stock(self) -> set[str]:
        return {p["id"] for p in self.spare_parts
                if p.get("safety_stock") is None}

    def single_repair_devices(self) -> set[str]:
        cnt = defaultdict(int)
        for r in self.repairs:
            cnt[r["device"]] += 1
        return {d for d, c in cnt.items() if c == 1}

    # ---------- 排序 ----------

    def top_by(self, key, k: int) -> list[str]:
        return [x[0] for x in sorted(key, key=lambda x: (-x[1], x[0]))[:k]]

    def top_stock_tight(self, k: int) -> list[str]:
        cand = [(p["id"], p["stock"] - p["safety_stock"])
                for p in self.spare_parts
                if p.get("safety_stock") is not None and p.get("stock") is not None]
        return [x[0] for x in sorted(cand, key=lambda x: (x[1], x[0]))[:k]]

    def top_dependents(self, k: int) -> list[str]:
        deps = self.depends_on()
        rev: dict[str, set[str]] = defaultdict(set)
        for down, ups in deps.items():
            for up in ups:
                rev[up].add(down)
        cand = [(e["id"], len(rev.get(e["id"], set()))) for e in self.equipment]
        return self.top_by(cand, k)

    def top_repairs(self, k: int) -> list[str]:
        cnt = defaultdict(int)
        for r in self.repairs:
            cnt[r["device"]] += 1
        return self.top_by(list(cnt.items()), k)

    def top_failures(self, k: int) -> list[str]:
        cnt = defaultdict(int)
        for r in self.repairs:
            cnt[r["failure"]] += 1
        return self.top_by(list(cnt.items()), k)


# ---------------- 比对 ----------------

def compare(qa_items: list[dict], sg: SourceGraph) -> list[dict]:
    """逐题用独立路径重算，跟标准答案比。返回不一致的条目。"""
    bad = []
    for it in qa_items:
        qid, layer = it["id"], it["layer"]
        ans = it["answer"]
        if layer == "REFUSE":
            continue
        got = None
        try:
            got = _recompute(it, sg)
        except Exception as e:                        # noqa: BLE001
            bad.append({"id": qid, "why": f"重算失败 {type(e).__name__}: {e}",
                        "question": it["question"]})
            continue
        if got is None:
            continue                                   # 这层没实现独立路径
        want = _first_value(ans)
        if not _same(got, want):
            bad.append({
                "id": qid, "question": it["question"],
                "why": "两条路径答案不一致",
                "图算的": _brief(want), "独立重算": _brief(got),
            })
    return bad


def _first_value(ans: dict):
    for v in ans.values():
        return v
    return None


def _brief(v, limit: int = 90) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= limit else s[:limit] + " …"


def _same(a, b) -> bool:
    """集合类答案按集合比，标量按值比。"""
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    return a == b


def _recompute(it: dict, sg: SourceGraph):
    """按题目类型走对应的独立实现。返回 None 表示这层没实现。"""
    q, layer = it["question"], it["layer"]

    if layer == "A":
        if "属于哪个车间" in q:
            eid = q.split(" ")[0]
            return sg.shop_of(eid)
        if "是什么加工设备" in q:
            eid = q.split(" ")[0]
            return sg.function_of(eid)
        if "存放在哪里" in q:
            pid = q.split(" ")[0]
            return sg.location_of(pid)
        return None

    if layer == "B1":
        eid = q.split(" ")[0]
        return sorted(sg.downstream_of(eid))

    if layer == "B2":
        if "一共检修过几次" in q:
            return sg.repair_count(q.split(" ")[0])
        if "这个故障一共出现过几次" in q:
            return sg.failure_count(q.split("「")[1].split("」")[0])
        return None

    if layer == "B3":
        if "还有未完成的检修" in q or "只检修过一次" in q:
            for m in ("tenlong", "yuelong", "chengxin", "huanmai", "heyue"):
                if q.startswith(m):
                    base = (sg.undone_devices() if "未完成" in q
                            else sg.single_repair_devices())
                    return sorted(x for x in base if x.startswith(m))
            if "未完成" in q:
                return sorted(sg.undone_devices())
            return sorted(sg.single_repair_devices())
        if "没有参与任何工艺流程" in q:
            base = sg.devices_not_in_any_process()
            for m in ("tenlong", "yuelong", "chengxin", "huanmai", "heyue"):
                if q.startswith(m):
                    return sorted(x for x in base if x.startswith(m))
            return sorted(base)
        if "没有任何上下游依赖关系" in q:
            return sorted(sg.devices_without_dependency())
        if "没有设置安全库存" in q:
            return sorted(sg.sp_without_safety_stock())
        return None

    if layer == "B4":
        k = int("".join(c for c in q.split("的")[-1].split("个")[0].split("台")[0]
                        if c.isdigit()) or 5)
        if "库存余量最少" in q:
            return sorted(sg.top_stock_tight(k))
        if "被最多设备依赖" in q:
            return sorted(sg.top_dependents(k))
        if "检修次数最多" in q:
            return sorted(sg.top_repairs(k))
        if "出现次数最多" in q:
            return sorted(sg.top_failures(k))
        return None

    return None
