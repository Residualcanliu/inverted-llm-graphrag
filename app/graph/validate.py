"""确定性的 Cypher 校验。不花任何 LLM 调用。

三道检查：
  1. 只读拦截 —— 拦掉写操作和写过程
  2. schema 一致性 —— 标签、关系类型、属性（含点号取属性）是否真实存在
  3. 关系方向 —— 是否符合 schema 定义

第 3 道是重点。调研数据：Text2Cypher 的语义错误里关系方向搞反占 33%，
是占比最高的一类，而它完全合法，EXPLAIN 抓不到。

**能力边界（重要，别高估）**：
  这个校验器基于正则和轻量解析，不是完整的 Cypher 语法分析器。
  它能抓：不存在的标签/关系类型/属性、方向写反、写操作。
  它抓不到：聚合算错、过滤条件漏写、跳数写错、语义与问题不符。
  后几类同样是高发错误（计数/聚合错误占 30%），要靠 prompt 工程和 few-shot 挡。

  所以校验层是兜底，不是主力。投入分配不要搞反。

**已知的误判边界**：
  - `MATCH(x:Label)` 这种关键字和括号之间没空格的写法，会被当成函数调用而跳过。
    模型生成的 Cypher 几乎都写成 `MATCH (`，这个取舍可以接受。
  - 靠变量引用的关系（`MATCH (a)-[:R]->(b)` 之后再 `(b)-[:S]->(c)`）无法判方向，
    因为不知道 b 的标签。这类查询会跳过方向检查。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.graph import schema_def as S

# ---------- 只读拦截 ----------

_WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV)\b",
    re.IGNORECASE,
)
_WRITE_PROC = re.compile(
    r"\b(apoc\.(create|merge|refactor|nodes\.link|periodic\.commit)"
    r"|gds\.\w+\.write"
    r"|db\.(create|index\.create|constraint\.create))\w*",
    re.IGNORECASE,
)
_MULTI_STMT = re.compile(r";\s*\S")
_COMMENT = re.compile(r"(//|/\*)")

# 一条查询必须**以**子句关键字开头。
#
# 只判断「包含关键字」不够用 —— 英文散文里的 with / set / call / return
# 会撞上 Cypher 子句名。实测 "I cannot help with that" 就被放行了。
# 真实 Cypher 都以子句开头，所以用「开头匹配」这个更强的条件。
_LEADING_CLAUSE = re.compile(
    r"^(MATCH|OPTIONAL\s+MATCH|CREATE|MERGE|DELETE|DETACH|SET|REMOVE"
    r"|WITH|UNWIND|RETURN|CALL|FOREACH|LOAD\s+CSV)\b",
    re.IGNORECASE,
)


@dataclass
class Issue:
    level: str      # "error" 拦下不执行 | "warn" 放行但记录
    kind: str
    detail: str
    snippet: str = ""


@dataclass
class ValidationResult:
    ok: bool
    issues: list[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warn"]

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "通过"
        parts = []
        if self.errors:
            parts.append(f"{len(self.errors)} 个错误")
        if self.warnings:
            parts.append(f"{len(self.warnings)} 个警告")
        return "，".join(parts)


# ---------- 轻量模式解析 ----------

@dataclass
class NodePattern:
    var: str
    labels: list[str]
    props: list[str]
    raw: str

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, NodePattern)
                and self.var == other.var and self.labels == other.labels
                and self.props == other.props)


@dataclass
class RelPattern:
    rel_type: str
    direction: str          # "->" | "<-" | "--" | "<>"
    left: NodePattern | None
    right: NodePattern | None
    raw: str


_REL_CHUNK = re.compile(r"(<?)\s*-\s*\[\s*([^\]]*)\s*\]\s*-\s*(>?)")
_NODE_PARTS = re.compile(
    r"^\s*(?P<var>[A-Za-z_]\w*)?\s*"
    r"(?P<labels>(?::\s*[A-Za-z_]\w*\s*)*)"
    r"(?P<props>\{.*\})?\s*$",
    re.DOTALL,
)


def _is_call_paren(s: str, idx: int) -> bool:
    """判断 idx 处的 '(' 是函数调用还是节点模式。

    只看紧邻的前一个字符：是标识符字符就当函数调用。
    这里刻意不跳过空白 —— 跳了的话 `MATCH (x:Label)` 里的 MATCH 会被误判成函数名。
    代价是 `MATCH(` 这种不留空格的写法会被跳过，但模型生成时几乎不这么写。
    """
    if idx == 0:
        return False
    return s[idx - 1].isalnum() or s[idx - 1] == "_"


def _parse_node(inner: str, raw: str) -> NodePattern | None:
    m = _NODE_PARTS.match(inner)
    if not m:
        return None
    labels = re.findall(r":\s*([A-Za-z_]\w*)", m.group("labels") or "")
    props: list[str] = []
    if m.group("props"):
        body = m.group("props")[1:-1]
        # E.g. {name:'3号泵', model:'X'} -> name, model
        props = re.findall(r"[{,]\s*([A-Za-z_]\w*)\s*:", "," + body)
    return NodePattern(m.group("var") or "", labels, props, raw)


def _node_before(s: str, pos: int) -> NodePattern | None:
    """找 pos 之前紧邻的 (...) 节点模式。"""
    end = s.rfind(")", 0, pos)
    if end == -1:
        return None
    depth = 0
    start = -1
    for i in range(end, -1, -1):
        if s[i] == ")":
            depth += 1
        elif s[i] == "(":
            depth -= 1
            if depth == 0:
                start = i
                break
    if start == -1 or _is_call_paren(s, start):
        return None
    return _parse_node(s[start + 1:end], s[start:end + 1])


def _node_after(s: str, pos: int) -> NodePattern | None:
    start = s.find("(", pos)
    if start == -1 or _is_call_paren(s, start):
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return _parse_node(s[start + 1:i], s[start:i + 1])
    return None


def parse_patterns(cypher: str) -> tuple[list[NodePattern], list[RelPattern]]:
    """扫描出节点模式和关系模式。尽力而为，不做完整语法分析。"""
    rels: list[RelPattern] = []
    nodes: list[NodePattern] = []

    def remember(n: NodePattern | None) -> None:
        if n and n not in nodes:
            nodes.append(n)

    for m in _REL_CHUNK.finditer(cypher):
        body = m.group(2).strip()
        types = re.findall(r":\s*([A-Za-z_]\w*)", body)
        left = _node_before(cypher, m.start())
        right = _node_after(cypher, m.end())
        if m.group(1) == "<" and m.group(3) == ">":
            direction = "<>"
        elif m.group(1) == "<":
            direction = "<-"
        elif m.group(3) == ">":
            direction = "->"
        else:
            direction = "--"
        rels.append(RelPattern(types[0] if types else "", direction, left, right,
                               m.group(0)))
        remember(left)
        remember(right)

    # 补充没有被关系覆盖到的孤立节点
    for m in re.finditer(r"\(([^()]*)\)", cypher):
        if not m.group(1).strip() or _is_call_paren(cypher, m.start()):
            continue
        remember(_parse_node(m.group(1), m.group(0)))

    return nodes, rels


def bind_vars(nodes: list[NodePattern]) -> dict[str, set[str]]:
    """变量名 -> 它绑定的标签集合。用于检查 `e.name` 这类点号取属性。"""
    out: dict[str, set[str]] = {}
    for n in nodes:
        if n.var:
            out.setdefault(n.var, set()).update(n.labels)
    return out


# ---------- 三道检查 ----------

def check_structure(cypher: str) -> list[Issue]:
    """兜底：确认这确实是一条查询。

    实测踩到的坑：模型偶尔会输出字面量 `Cypher：` 这种没内容的字符串。
    三道检查对它全部放行 —— 没有标签、没有关系、没有写操作，所以「没发现错误」。
    结果是校验通过、EXPLAIN 才报语法错，白跑一轮。
    """
    # 去掉开头的注释再判断，别让注释挡住子句
    s = re.sub(r"^\s*(//[^\n]*\n|/\*.*?\*/)", "", cypher, flags=re.DOTALL).lstrip()
    if not s:
        return [Issue("error", "not_cypher", "内容为空")]
    if not _LEADING_CLAUSE.match(s):
        return [Issue("error", "not_cypher",
                      "不是一条查询（没有以 MATCH / RETURN 等子句开头）", s[:60])]
    return []


def check_readonly(cypher: str) -> list[Issue]:
    out: list[Issue] = []
    m = _WRITE_CLAUSE.search(cypher)
    if m:
        out.append(Issue("error", "write_clause",
                         f"包含写操作 {m.group(0).upper()}，只读链路拒绝执行", m.group(0)))
    m = _WRITE_PROC.search(cypher)
    if m:
        out.append(Issue("error", "write_proc",
                         f"调用了会产生写副作用的过程 {m.group(0)}", m.group(0)))
    if _MULTI_STMT.search(cypher):
        out.append(Issue("error", "multi_statement",
                         "包含多条语句，driver 不支持且可能是注入"))
    if _COMMENT.search(cypher):
        out.append(Issue("warn", "comment", "包含注释，已记录"))
    return out


def check_schema(cypher: str, nodes: list[NodePattern]) -> list[Issue]:
    out: list[Issue] = []

    for n in nodes:
        for lb in n.labels:
            if lb not in S.LABELS:
                out.append(Issue("error", "unknown_label",
                                 f"标签 {lb} 在 schema 里不存在", n.raw))
        known = set()
        for lb in n.labels:
            if lb in S.NODES:
                known |= set(S.NODES[lb][1])
        for p in n.props:
            if known and p not in known:
                out.append(Issue("error", "unknown_property",
                                 f"属性 {p} 不属于 {':'.join(n.labels)}", n.raw))

    # 点号取属性：用变量绑定表判断
    binding = bind_vars(nodes)
    seen: set[tuple[str, str]] = set()
    for m in re.finditer(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)", cypher):
        var, prop = m.group(1), m.group(2)
        if var not in binding or (var, prop) in seen:
            continue
        seen.add((var, prop))
        known = set()
        for lb in binding[var]:
            if lb in S.NODES:
                known |= set(S.NODES[lb][1])
        if known and prop not in known:
            out.append(Issue("error", "unknown_property",
                             f"属性 {prop} 不属于 {var}（{'/'.join(sorted(binding[var]))}）",
                             m.group(0)))
    return out


def check_directions(rels: list[RelPattern]) -> list[Issue]:
    """抓「关系方向写反」。这是占比最高的一类语义错误。"""
    out: list[Issue] = []
    for r in rels:
        if not r.rel_type or r.direction == "--":
            continue
        if r.rel_type not in S.REL_TYPES:
            out.append(Issue("error", "unknown_rel",
                             f"关系类型 {r.rel_type} 在 schema 里不存在", r.raw))
            continue
        if r.direction == "<>":
            out.append(Issue("error", "bad_direction",
                             f"关系 {r.rel_type} 的方向写法非法（<- 和 -> 同时出现）",
                             r.raw))
            continue

        frm, to = S.rel_direction(r.rel_type)      # type: ignore[misc]
        left_labels = set(r.left.labels) if r.left else set()
        right_labels = set(r.right.labels) if r.right else set()
        # 两端都有标签才判得了方向；靠变量引用的查不到，跳过
        if not left_labels or not right_labels:
            continue

        if r.direction == "->":
            actual, expect = (left_labels, right_labels), (frm, to)
        else:
            actual, expect = (right_labels, left_labels), (frm, to)

        if not (expect[0] in actual[0] and expect[1] in actual[1]):
            out.append(Issue(
                "error", "wrong_direction",
                f"关系 {r.rel_type} 方向反了。schema 规定 {frm} -> {to}，"
                f"查询里写成 {'/'.join(sorted(actual[0]))} -> "
                f"{'/'.join(sorted(actual[1]))}", r.raw))
    return out


def validate(cypher: str) -> ValidationResult:
    """跑完三道检查。

    这里不做 EXPLAIN —— 那一步需要连库，由 app/graph/client.py 的 explain() 负责。
    """
    cypher = cypher.strip()
    if not cypher:
        return ValidationResult(ok=False, issues=[
            Issue("error", "empty", "生成的 Cypher 是空的")])

    issues: list[Issue] = []
    issues += check_structure(cypher)
    issues += check_readonly(cypher)
    nodes, rels = parse_patterns(cypher)
    issues += check_schema(cypher, nodes)
    issues += check_directions(rels)
    return ValidationResult(
        ok=not any(i.level == "error" for i in issues),
        issues=issues,
        stats={"nodes": len(nodes), "rels": len(rels)},
    )
