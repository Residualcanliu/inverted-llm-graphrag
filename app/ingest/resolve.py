"""归一：把不同叫法合并到同一个节点。

分两段，性质不同：

  第一段  AI 生成候选     「这两个名字是不是指同一个东西」需要语义判断
  第二段  规则拍板         「该不该合并」是决策，阈值以下进待确认队列

只有第一段用模型。阈值判定、去重、写文件都是确定性代码。

为什么不全部自动：源数据里超过半数的分歧来自文本本身有多种合理解读，
不是解析失误。例如「喷嘴堵塞」的成因写成「熔渣堵塞」，两者既可能是同一件事
的两种描述，也可能是故障和成因的关系。这个判断需要懂机床的人做。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# 阈值：高置信自动合并，中间进待确认，低置信丢弃
AUTO_MERGE = 0.9
PENDING_FLOOR = 0.6


@dataclass
class Candidate:
    a: str
    b: str
    confidence: float
    kind: str = "同义"          # 同义 | 上下位
    reason: str = ""

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "confidence": self.confidence,
                "kind": self.kind, "reason": self.reason}


@dataclass
class ResolveResult:
    auto: list[Candidate] = field(default_factory=list)      # 自动合并
    pending: list[Candidate] = field(default_factory=list)   # 待确认
    dropped: list[Candidate] = field(default_factory=list)   # 低于阈值

    def summary(self) -> str:
        return (f"自动合并 {len(self.auto)} 组，待确认 {len(self.pending)} 组，"
                f"丢弃 {len(self.dropped)} 组")


# ---------------- prompt ----------------

def _as_list(names: list[str]) -> str:
    """逐行列出，每项独立一行。

    踩过的坑：最初用顿号拼成一行，模型把整行读成了一个项，
    产出「换刀失败、接地不良」这种拼接出来的假名字，然后再说它跟其中一半是上下位关系。
    候选里全是垃圾。每项独立成行之后就没有这个问题。
    """
    return "\n".join(f"- {n}" for n in names)


# 同义判定：只在「故障名」和「故障现象名」之间找。
#
# 为什么不把成因也放进来一起问：成因和故障不是同一类概念。
# 混在一起问，模型会把「粉尘油污附着」判成「镜片污染」的同义，
# 而实际是因果关系。合并之后因果链就没了，B5 根因追溯用的正是这条链。
# 所以因果单独问，见下面的 CAUSE_PROMPT。
SYNONYM_PROMPT = """下面是同一家工厂两份文档里的故障名称。

【检修记录里的故障名】
{group_a}

【故障手册里的故障现象名】
{group_b}

请找出**跨这两组之间可能指同一个东西**的名称对。

判断标准：

- 「同义」：同一件事的两种叫法，例如简称和全称
- 「上下位」：一个是另一个的具体化，例如「主轴轴承磨损」和「轴承磨损」
- 两组是对等的同类概念，问的是「同一个东西的两种叫法」
- **因果关系不算同义**。如果一个名称是另一个的原因，不要把它们配成对
- 拿不准的也给出来，把置信度写低
- 每行是一个独立名称，不要拼接组合。a 和 b 必须是上面出现过的原文

输出 JSON 数组：

```json
[
  {{"a": "磁力吸盘失效", "b": "电磁吸盘失效", "confidence": 0.95,
   "kind": "同义", "reason": "同一部件的两种叫法"}},
  {{"a": "主轴轴承磨损", "b": "轴承磨损", "confidence": 0.6,
   "kind": "上下位", "reason": "前者是后者的具体化"}}
]
```

confidence 取 0 到 1。只输出 JSON 数组，不要解释。"""


# 因果判定：成因 → 故障现象。产出的是边，不是合并。
CAUSE_PROMPT = """下面是故障手册里记录的故障现象，以及文档里写的成因。

【故障现象】
{effects}

【成因】
{causes}

请判断哪些成因是哪个故障现象的原因。因果不是同义，两者**不合并**，只是建立连接。

注意：

- 一条成因可能对应多个现象，一个现象也可能有多个成因
- 有些成因不在现象列表里，那属于更底层的因素，保留原文即可
- 只输出能明确对应的
- 每行是一个独立名称，不要拼接组合。名称必须是上面出现过的原文

输出 JSON 数组：

```json
[
  {{"cause": "镜片污染", "effect": "激光功率下降", "confidence": 0.9}},
  {{"cause": "轴承磨损", "effect": "主轴异响", "confidence": 0.85}}
]
```

confidence 取 0 到 1。只输出 JSON 数组，不要解释。"""


def build_synonym_prompt(group_a: list[str], group_b: list[str]) -> str:
    return SYNONYM_PROMPT.format(group_a=_as_list(group_a), group_b=_as_list(group_b))


def build_cause_prompt(effects: list[str], causes: list[str]) -> str:
    return CAUSE_PROMPT.format(effects=_as_list(effects), causes=_as_list(causes))


@dataclass
class CausalLink:
    cause: str
    effect: str
    confidence: float

    def to_dict(self) -> dict:
        return {"cause": self.cause, "effect": self.effect,
                "confidence": self.confidence}


def parse_causal_links(text: str) -> list[CausalLink]:
    """解析因果候选。容错方式与 parse_candidates 一致。"""
    s = (text or "").strip()
    m = _FENCE.search(s)
    if m:
        s = m.group(1).strip()
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end < start:
        return []
    try:
        raw = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        c = str(item.get("cause", "")).strip()
        e = str(item.get("effect", "")).strip()
        if not c or not e or c == e:
            continue
        try:
            conf = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        out.append(CausalLink(cause=c, effect=e, confidence=max(0.0, min(1.0, conf))))
    return out


def validate_links(links: list[CausalLink], causes: set[str],
                   effects: set[str]) -> tuple[list[CausalLink], list[CausalLink]]:
    """分开返回合法与不合法的因果链接。"""
    ok, bad = [], []
    for l in links:
        (ok if l.cause in causes and l.effect in effects else bad).append(l)
    return ok, bad


def validate_candidates(cands: list[Candidate], known_names: set[str]) -> list[Candidate]:
    """过滤掉名字对不上的候选。

    模型有时会拼出一个输入里不存在的名字，或者把两个名字缩成一个。
    这类候选直接丢掉 —— 归一表里出现假名字，建图时会凭空建出节点。
    """
    out = []
    for c in cands:
        if c.a in known_names and c.b in known_names:
            out.append(c)
    return out


def rejected_candidates(cands: list[Candidate], known_names: set[str]) -> list[Candidate]:
    """返回被过滤掉的那些，用于报告。"""
    return [c for c in cands if c.a not in known_names or c.b not in known_names]


# ---------------- 解析模型输出 ----------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_candidates(text: str) -> list[Candidate]:
    """从模型输出里抠出候选列表。

    模型可能套 Markdown 围栏、可能前后加说明，所以要容错。
    解析失败返回空列表，由调用方决定是否重试。
    """
    s = (text or "").strip()
    m = _FENCE.search(s)
    if m:
        s = m.group(1).strip()
    # 找第一个 JSON 数组
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        raw = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []

    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        a, b = str(item.get("a", "")).strip(), str(item.get("b", "")).strip()
        if not a or not b or a == b:
            continue
        try:
            conf = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        out.append(Candidate(
            a=a, b=b, confidence=max(0.0, min(1.0, conf)),
            kind=str(item.get("kind", "同义")).strip() or "同义",
            reason=str(item.get("reason", "")).strip(),
        ))
    return out


def normalize_pairs(cands: list[Candidate]) -> list[Candidate]:
    """去重并统一方向。

    (a,b) 和 (b,a) 是同一组，只留一条；方向统一成字典序，避免重复。
    """
    seen: dict[tuple[str, str], Candidate] = {}
    for c in cands:
        key = tuple(sorted((c.a, c.b)))
        prev = seen.get(key)
        if prev is None or c.confidence > prev.confidence:
            a, b = key
            seen[key] = Candidate(a=a, b=b, confidence=c.confidence,
                                  kind=c.kind, reason=c.reason)
    return sorted(seen.values(), key=lambda c: -c.confidence)


def split_by_threshold(cands: list[Candidate],
                       auto: float = AUTO_MERGE,
                       floor: float = PENDING_FLOOR) -> ResolveResult:
    r = ResolveResult()
    for c in cands:
        if c.confidence >= auto:
            r.auto.append(c)
        elif c.confidence >= floor:
            r.pending.append(c)
        else:
            r.dropped.append(c)
    return r


def to_synonym_map(cands: list[Candidate]) -> dict[str, str]:
    """把候选对转成「别名 → 主名」的映射表。

    一组里选最短的当主名：通常全称更长，简称更短且更常用。
    上下位关系里短的也恰好是更宽泛的那个，合并到它比较安全。
    """
    groups: list[set[str]] = []
    for c in cands:
        hit = [g for g in groups if c.a in g or c.b in g]
        if not hit:
            groups.append({c.a, c.b})
            continue
        base = hit[0]
        base |= {c.a, c.b}
        for other in hit[1:]:
            base |= other
            groups.remove(other)
    out: dict[str, str] = {}
    for g in groups:
        main = sorted(g, key=lambda x: (len(x), x))[0]
        for name in g:
            if name != main:
                out[name] = main
    return out
