"""问题集生成。

按题型套模板生成候选，每题配一条**手写的参考 Cypher**，标准答案由它从图上算出。

为什么标准答案不能从被测系统拿：那等于拿模型的答案验模型。参考 Cypher 是人工写的，
跟四条链路都无关，它算出来的才是 ground truth。

生成的是候选，还要人工审三件事：
  ① 答案唯一吗（会不会并列）
  ② 答案在数据里吗（问的东西真的存在吗）
  ③ 措辞有歧义吗（两种合理解读会不会导致不同答案）
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from app.graph import client


# 答案经过了几层「解释」。数字越小越可信，人工审的优先级越低。
#
# 这条链的中间人是我（Claude），不是领域专家。所以每道题的答案离原始数据有多远，
# 必须标出来 —— 不然审的人只能 153 道平均用力，而真正需要看的只有后两类。
DERIVATION = {
    "direct":      "直读：从一条记录里读出来，不需要任何语义判断",
    "aggregate":   "聚合：数多条记录，不需要语义判断",
    "negation":    "补集：集合相减，「没有」的定义是明确的",
    "relational":  "关系路径：依赖我定义的路径语义（比如「影响」是上游还是下游）",
    "interpreted": "文本解释：依赖我把一段自由文本解释成了因果或措施",
}

# 题层 -> 推导层级。同层里也可能不齐，所以 add() 允许单独覆盖。
LAYER_DERIVATION = {
    "A": "direct",
    "B2": "aggregate",
    "B3": "negation",
    "B1": "relational",
    "B4": "relational",
    "B5": "interpreted",
    "REFUSE": "direct",
}


@dataclass
class QAItem:
    id: str
    layer: str                  # A | B1 | B2 | B3 | B4 | B5 | REFUSE
    question: str
    answer_type: str            # scalar | set | number | list
    ref_cypher: str
    answer: dict = field(default_factory=dict)
    note: str = ""
    derivation: str = "direct"  # 见 DERIVATION

    def to_dict(self) -> dict:
        return {"id": self.id, "layer": self.layer, "question": self.question,
                "answer_type": self.answer_type, "ref_cypher": self.ref_cypher,
                "answer": self.answer, "note": self.note,
                "derivation": self.derivation,
                "derivation_desc": DERIVATION.get(self.derivation, "")}


def _rows(cypher: str, **kw) -> list[dict]:
    return client.run_readonly(cypher, kw or None).rows


def _one(cypher: str, **kw):
    r = _rows(cypher, **kw)
    return r[0] if r else {}


class QABuilder:
    def __init__(self, seed: int = 42):
        self.rnd = random.Random(seed)
        self.items: list[QAItem] = []
        self._n = 0

    def add(self, layer: str, question: str, answer_type: str,
            cypher: str, answer: dict, note: str = "",
            derivation: str | None = None) -> None:
        self._n += 1
        self.items.append(QAItem(
            id=f"{layer}-{self._n:03d}", layer=layer, question=question,
            answer_type=answer_type, ref_cypher=cypher.strip(),
            answer=answer, note=note,
            derivation=derivation or LAYER_DERIVATION.get(layer, "direct")))

    def pick(self, seq: list, k: int) -> list:
        seq = list(seq)
        self.rnd.shuffle(seq)
        return seq[:k]

    # ---------------- A 类：事实查找 ----------------

    def build_a(self, n: int = 40) -> None:
        eq = [r["id"] for r in _rows("MATCH (e:Equipment) RETURN e.id AS id ORDER BY e.id")]
        sp = [r["id"] for r in _rows("MATCH (p:SparePart) RETURN p.id AS id ORDER BY p.id")]
        # 型号汇总，避免问出来答案千篇一律
        by_model: dict[str, list[str]] = {}
        for e in eq:
            by_model.setdefault(e.split("-")[0], []).append(e)

        # 分三类各占一部分：车间位置、功能、备件位置
        n1, n2, n3 = n // 2, n // 4, n - n // 2 - n // 4

        for eid in self.pick(eq, n1):
            q = f"{eid} 属于哪个车间？"
            c = "MATCH (e:Equipment {id:$i})-[:IN_SHOP]->(l:Location) RETURN l.name AS v"
            a = _one(c, i=eid)
            if a.get("v"):
                self.add("A", q, "scalar", c.replace("$i", f"'{eid}'"), {"车间": a["v"]})

        for eid in self.pick(eq, n2):
            q = f"{eid} 是什么加工设备？"
            c = "MATCH (e:Equipment {id:$i}) RETURN e.function AS v"
            a = _one(c, i=eid)
            if a.get("v"):
                self.add("A", q, "scalar", c.replace("$i", f"'{eid}'"), {"功能": a["v"]})

        for pid in self.pick(sp, n3):
            q = f"{pid} 存放在哪里？"
            c = ("MATCH (p:SparePart {id:$i})-[:STORED_AT]->(l:Location) "
                 "RETURN l.name AS v")
            a = _one(c, i=pid)
            if a.get("v"):
                self.add("A", q, "scalar", c.replace("$i", f"'{pid}'"),
                         {"存放位置": a["v"]})

    # ---------------- B1 多跳依赖 ----------------

    def build_b1(self, n: int = 25) -> None:
        # 只挑有下游的设备，否则答案是空集，测不出东西
        cands = _rows("""MATCH (e:Equipment)<-[:DEPENDS_ON*1..5]-(x:Equipment)
                         RETURN e.id AS id, count(DISTINCT x) AS n
                         ORDER BY n DESC""")
        cands = [r["id"] for r in cands if r["n"] > 0]
        for eid in self.pick(cands, n):
            q = f"{eid} 停机会影响哪些设备？"
            c = ("MATCH (e:Equipment {id:$i})<-[:DEPENDS_ON*1..5]-(x:Equipment) "
                 "RETURN DISTINCT x.id AS v ORDER BY v")
            rows = _rows(c, i=eid)
            self.add("B1", q, "set", c.replace("$i", f"'{eid}'"),
                     {"受影响设备": [r["v"] for r in rows]})

    # ---------------- B2 聚合统计 ----------------

    def build_b2(self, n: int = 25) -> None:
        half = n // 2
        eq = [r["id"] for r in _rows("""MATCH (e:Equipment)-[:EXPERIENCED]->(:WorkOrder)
                                        RETURN DISTINCT e.id AS id ORDER BY id""")]
        for eid in self.pick(eq, half):
            q = f"{eid} 一共检修过几次？"
            c = ("MATCH (e:Equipment {id:$i})-[:EXPERIENCED]->(w:WorkOrder) "
                 "RETURN count(w) AS v")
            a = _one(c, i=eid)
            self.add("B2", q, "number", c.replace("$i", f"'{eid}'"),
                     {"检修次数": a.get("v")})

        fm = [r["name"] for r in _rows("""
            MATCH (w:WorkOrder)-[:CAUSED_BY]->(f:FailureMode)
            RETURN f.name AS name, count(w) AS n ORDER BY n DESC""")]
        for name in self.pick(fm, n - half):
            q = f"「{name}」这个故障一共出现过几次？"
            c = ("MATCH (w:WorkOrder)-[:CAUSED_BY]->(f:FailureMode {name:$n}) "
                 "RETURN count(w) AS v")
            a = _one(c, n=name)
            self.add("B2", q, "number", c.replace("$n", f"'{name}'"),
                     {"出现次数": a.get("v")})

    # ---------------- B3 否定与补集 ----------------

    # 型号前缀，用来构造「限定范围的补集」
    MODELS = ("tenlong", "yuelong", "chengxin", "huanmai", "heyue")

    def build_b3(self, n: int = 20) -> None:
        """否定与补集。

        **不硬凑题量。** 全局补集只有 8 个答案非空，硬凑 20 道同一道题问五遍
        不增加信息量。改用两层：全局补集 + 按型号限定的补集。空集答案自动丢掉 ——
        「哪些备件库存低于安全线」这类返回空的题目测不出东西。
        """
        built = 0

        # 第一层：全局补集
        global_specs = [
            ("哪些设备还有未完成的检修？",
             """MATCH (e:Equipment)-[:EXPERIENCED]->(w:WorkOrder) WHERE w.done = false
                RETURN DISTINCT e.id AS v ORDER BY v""", "设备"),
            ("哪些设备没有参与任何工艺流程？",
             """MATCH (e:Equipment) WHERE NOT (e)<-[:USES]-(:Operation)
                RETURN e.id AS v ORDER BY v""", "设备"),
            ("哪些设备没有任何上下游依赖关系？",
             """MATCH (e:Equipment) WHERE NOT (e)-[:DEPENDS_ON]-()
                RETURN e.id AS v ORDER BY v""", "设备"),
            ("哪些备件或工具没有设置安全库存？",
             """MATCH (p:SparePart) WHERE p.safety_stock IS NULL
                RETURN p.id AS v ORDER BY v""", "备件"),
            ("哪些故障模式还没有对应的处理措施？",
             """MATCH (f:FailureMode) WHERE NOT (f)-[:MITIGATED_BY]->()
                RETURN f.name AS v ORDER BY v""", "故障模式"),
            ("哪些故障模式不会引发其他故障？",
             """MATCH (f:FailureMode) WHERE NOT (f)-[:TRIGGERS]->()
                RETURN f.name AS v ORDER BY v""", "故障模式"),
            ("哪些备件从来没有在检修中被使用过？",
             """MATCH (p:SparePart) WHERE NOT ()-[:USED_PART]->(p)
                RETURN p.id AS v ORDER BY v""", "备件"),
            ("哪些设备只检修过一次？",
             """MATCH (e:Equipment)-[:EXPERIENCED]->(w:WorkOrder)
                WITH e, count(w) AS c WHERE c = 1
                RETURN e.id AS v ORDER BY v""", "设备"),
        ]
        for q, c, key in global_specs:
            ans = [r["v"] for r in _rows(c)]
            if not ans:                       # 空集答案丢掉
                continue
            self.add("B3", q, "set", c, {key: ans}, note=f"补集大小 {len(ans)}")
            built += 1

        # 第二层：按型号限定。答案非空的才留
        scoped = [
            ("{m} 系列里哪些设备还有未完成的检修？",
             """MATCH (e:Equipment)-[:EXPERIENCED]->(w:WorkOrder)
                WHERE w.done = false AND e.id STARTS WITH $m
                RETURN e.id AS v ORDER BY v""", "设备"),
            ("{m} 系列里哪些设备没有参与任何工艺流程？",
             """MATCH (e:Equipment) WHERE NOT (e)<-[:USES]-(:Operation)
                AND e.id STARTS WITH $m
                RETURN e.id AS v ORDER BY v""", "设备"),
            ("{m} 系列里哪些设备只检修过一次？",
             """MATCH (e:Equipment)-[:EXPERIENCED]->(w:WorkOrder)
                WHERE e.id STARTS WITH $m
                WITH e, count(w) AS c WHERE c = 1
                RETURN e.id AS v ORDER BY v""", "设备"),
        ]
        for m in self.MODELS:
            for tmpl, c, key in scoped:
                if built >= n:
                    break
                ans = [r["v"] for r in _rows(c, m=m)]
                if not ans:
                    continue
                self.add("B3", tmpl.format(m=m), "set",
                         c.replace("$m", f"'{m}'"), {key: ans},
                         note=f"{m} 系列，补集大小 {len(ans)}")
                built += 1

    # ---------------- B4 排序与阈值 ----------------

    def build_b4(self, n: int = 20) -> None:
        """排序与阈值。

        维度 × 取几名的组合。同一个维度取 Top-3 和 Top-5 算两道不同的题
        （答案不同，模型也得看懂 N），但它们同模板，信息量低于换维度。
        报告里会按维度也统计一遍。
        """
        dims = [
            ("库存余量最少的{top}个备件是哪些？",
             "MATCH (p:SparePart) WHERE p.safety_stock IS NOT NULL "
             "RETURN p.id AS v ORDER BY p.stock - p.safety_stock ASC LIMIT {k}", "备件"),
            ("被最多设备依赖的{top}台设备是哪些？",
             "MATCH (e:Equipment)<-[:DEPENDS_ON]-(d:Equipment) "
             "RETURN e.id AS v, count(d) AS n ORDER BY n DESC LIMIT {k}", "设备"),
            ("检修次数最多的{top}台设备是哪些？",
             "MATCH (e:Equipment)-[:EXPERIENCED]->(w:WorkOrder) "
             "RETURN e.id AS v, count(w) AS n ORDER BY n DESC LIMIT {k}", "设备"),
            ("出现次数最多的{top}个故障模式是哪些？",
             "MATCH (w:WorkOrder)-[:CAUSED_BY]->(f:FailureMode) "
             "RETURN f.name AS v, count(w) AS n ORDER BY n DESC LIMIT {k}", "故障模式"),
            ("适配设备最多的{top}个备件是哪些？",
             "MATCH (p:SparePart)<-[:FITS]-(e:Equipment) "
             "RETURN p.id AS v, count(DISTINCT e) AS n ORDER BY n DESC LIMIT {k}", "备件"),
        ]
        built = 0
        for k in (3, 5, 10, 20):
            for tmpl, c, key in dims:
                if built >= n:
                    break
                ans = [r["v"] for r in _rows(c.replace("{k}", str(k)))]
                if not ans:
                    continue
                q = tmpl.format(top=f" {k} ")
                self.add("B4", q.replace("  ", " "), "list",
                         c.replace("{k}", str(k)), {key: ans},
                         note="排序题答案可能并列，判分按集合算")
                built += 1

    # ---------------- B5 根因追溯 ----------------

    def build_b5(self, n: int = 20) -> None:
        half = n // 2
        causes = [r["name"] for r in _rows("""
            MATCH (a:FailureMode)-[:TRIGGERS]->()
            RETURN DISTINCT a.name AS name ORDER BY name""")]
        for name in self.pick(causes, half):
            q = f"「{name}」会导致哪些故障？"
            c = """MATCH (a:FailureMode {name:$n})-[:TRIGGERS*1..3]->(b:FailureMode)
                   RETURN DISTINCT b.name AS v ORDER BY v"""
            rows = _rows(c, n=name)
            self.add("B5", q, "set", c.replace("$n", f"'{name}'"),
                     {"引发的故障": [r["v"] for r in rows]})

        fixed = [r["name"] for r in _rows("""
            MATCH (f:FailureMode)-[:MITIGATED_BY]->()
            RETURN DISTINCT f.name AS name ORDER BY name""")]
        for name in self.pick(fixed, n - half):
            q = f"「{name}」这个故障应该怎么处理？"
            c = """MATCH (f:FailureMode {name:$n})-[:MITIGATED_BY]->(s:SafetyMeasure)
                   RETURN s.name AS v ORDER BY v"""
            rows = _rows(c, n=name)
            self.add("B5", q, "set", c.replace("$n", f"'{name}'"),
                     {"处理措施": [r["v"] for r in rows]})

    # ---------------- 拒答 ----------------

    def build_refuse(self, n: int = 10) -> None:
        """问题和答案都不在知识库里，两边的正确行为都是说「查不到」。"""
        cases = [
            ("3号泵的检修周期是多久？", "设备编号不是 3号泵 这种形式，图里没有这台设备"),
            ("tenlong-001 的采购价格是多少？", "档案里没有价格字段"),
            ("昨天车间里有没有人受伤？", "档案里没有事故记录"),
            ("tenlong-001 的负责人是谁？", "档案里没有责任人字段"),
            ("yuelong-005 的能耗是多少？", "档案里没有能耗数据"),
            ("这批设备是哪个厂家生产的？", "档案里没有厂家字段（只有供应商字段，且针对备件）"),
            ("下个月的维护计划是什么？", "档案里只有历史记录，没有计划"),
            ("chengxin-001 的操作员是谁？", "档案里没有操作员字段"),
            ("A区2号货架的湿度是多少？", "档案里没有环境数据"),
            ("这台设备还能用几年？", "档案里没有寿命估算"),
        ]
        for q, why in cases[:n]:
            self.add("REFUSE", q, "refuse", "",
                     {"期望": "应回答查不到相关信息"},
                     note=why)

    # ---------------- 汇总 ----------------

    def build_all(self) -> list[QAItem]:
        self.build_a(40)
        self.build_b1(25)
        self.build_b2(25)
        self.build_b3(20)
        self.build_b4(20)
        self.build_b5(20)
        self.build_refuse(10)
        return self.items
