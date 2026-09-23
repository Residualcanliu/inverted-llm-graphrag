"""图谱 schema，从库里实时读。

为什么必须实时读：schema 一旦硬编码，就跟真实数据脱钩。实测踩到的后果是双向的——

  模型生成 `avg(wo.downtime_h)`，静态校验放行（代码里有这个属性），
  但库里没有 `downtime_h`，执行返回 None；
  反过来，`e.priority` 在库里是合法的，静态校验却会拒绝它（代码里没声明）。

prompt 里的 schema 也来自这里，所以硬编码等于从第一步就给模型错的信息。

`schema_def.py` 保留为 fallback：Neo4j 没起来时（跑单元测试、离线看 prompt）
仍然能用，但会明确告警。
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field


@dataclass
class Schema:
    labels: set[str] = field(default_factory=set)
    rel_types: set[str] = field(default_factory=set)
    node_props: dict[str, set[str]] = field(default_factory=dict)     # 标签 -> 属性
    rel_pairs: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # 关系 -> [(起, 终)]
    cn_names: dict[str, str] = field(default_factory=dict)            # 标签 -> 中文名
    source: str = "graph"                                             # graph | fallback

    # ---------- 渲染 ----------

    def render_for_prompt(self) -> str:
        lines = ["## 节点（标签，属性）"]
        for lb in sorted(self.labels):
            props = ", ".join(sorted(self.node_props.get(lb, []))) or "（无）"
            cn = f"（{self.cn_names[lb]}）" if lb in self.cn_names else ""
            lines.append(f"- {lb}{cn}，属性：{props}")

        lines.append("")
        lines.append("## 关系（方向是固定的，不能反）")
        for rt in sorted(self.rel_types):
            for frm, to in self.rel_pairs.get(rt, [("?", "?")]):
                lines.append(f"- ({frm})-[:{rt}]->({to})")
        return "\n".join(lines)

    def render_direction_hints(self) -> str:
        lines = ["## 关系方向速查（写反了是最常见的错误）"]
        for rt in sorted(self.rel_types):
            for frm, to in self.rel_pairs.get(rt, [("?", "?")]):
                lines.append(f"- {frm} --{rt}--> {to}")
        return "\n".join(lines)

    # ---------- 查询 ----------

    def rel_direction(self, rel_type: str) -> tuple[str, str] | None:
        pairs = self.rel_pairs.get(rel_type)
        return pairs[0] if pairs else None

    def props_of(self, labels) -> set[str]:
        out: set[str] = set()
        for lb in labels:
            out |= self.node_props.get(lb, set())
        return out

    # ---------- 构造 ----------

    @classmethod
    def from_graph(cls) -> "Schema":
        from app.graph import client

        s = cls(source="graph")
        with client.get_driver() as d, d.session() as sess:
            for r in sess.run("CALL db.labels() YIELD label RETURN label"):
                s.labels.add(r["label"])
                s.node_props.setdefault(r["label"], set())

            # 属性：直接采样数据，比 db.schema.nodeTypeProperties 稳
            # （那个过程的输出格式在版本间变过，还带弃用警告）
            for lb in sorted(s.labels):
                for r in sess.run(
                    f"MATCH (n:`{lb}`) UNWIND keys(n) AS k "
                    "RETURN DISTINCT k AS prop LIMIT 50"
                ):
                    s.node_props[lb].add(r["prop"])

            for r in sess.run(
                "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"
            ):
                s.rel_types.add(r["relationshipType"])

            # 关系两端标签：每个类型采样一次
            for rt in sorted(s.rel_types):
                pairs: dict[tuple[str, str], int] = {}
                for r in sess.run(
                    f"MATCH (a)-[:`{rt}`]->(b) "
                    "RETURN labels(a)[0] AS f, labels(b)[0] AS t, count(*) AS n"
                ):
                    pairs[(r["f"], r["t"])] = r["n"]
                if pairs:
                    s.rel_pairs[rt] = [k for k, _ in
                                       sorted(pairs.items(), key=lambda x: -x[1])]

        # 中文名从 schema_def 取，纯粹为了 prompt 可读性，不影响校验
        try:
            from app.graph import schema_def as SD
            s.cn_names = {lb: v[0] for lb, v in SD.NODES.items() if lb in s.labels}
        except Exception:                             # noqa: BLE001
            pass
        return s

    @classmethod
    def fallback(cls) -> "Schema":
        """Neo4j 不可用时用。内容来自 schema_def，只保证结构不崩，不保证跟真实数据一致。"""
        from app.graph import schema_def as SD
        s = cls(source="fallback")
        s.labels = set(SD.NODES)
        s.node_props = {lb: set(props) for lb, (_, props) in SD.NODES.items()}
        s.rel_types = {r[0] for r in SD.RELS}
        for t, frm, to, _, _ in SD.RELS:
            s.rel_pairs.setdefault(t, []).append((frm, to))
        s.cn_names = {lb: v[0] for lb, v in SD.NODES.items()}
        return s


# ---------------- 缓存 ----------------

_cached: Schema | None = None
_warned = False


def get(refresh: bool = False) -> Schema:
    """拿当前 schema。首次调用从库读，之后走缓存。"""
    global _cached, _warned
    if _cached is not None and not refresh:
        return _cached
    try:
        _cached = Schema.from_graph()
    except Exception as e:                            # noqa: BLE001
        if not _warned:
            import sys
            print(f"[schema] 读不到库，回退到内置定义（{type(e).__name__}）。"
                  f"prompt 和校验用的属性可能与真实数据不一致。", file=sys.stderr)
            _warned = True
        _cached = Schema.fallback()
    return _cached


def summary() -> str:
    s = get()
    return (f"schema 来源 {s.source}：{len(s.labels)} 类节点、"
            f"{len(s.rel_types)} 类关系")
