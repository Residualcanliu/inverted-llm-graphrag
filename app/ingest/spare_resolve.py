"""备件与工具的归一候选。

与故障名的归一不同，这里规则能先判掉一大半：

  同名 + 同规格   -> 高度疑似重复记录
  同名 + 不同规格 -> 疑似两个不同的东西恰好同名
  名称包含关系    -> 可能同义，也可能是部件关系，交给模型

后一类必须人工或模型判断。例如「焊枪」和「焊枪喷嘴」是部件关系，合并会抹掉层级；
而「刀架」和「刀架总成」很可能是同一个东西的简称和全称。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from app.ingest.resolve import Candidate


@dataclass
class DuplicateGroup:
    """同名的一组条目。"""
    name: str
    items: list[dict]

    @property
    def same_spec(self) -> bool:
        specs = {i.get("spec", "") for i in self.items}
        return len(specs) == 1

    @property
    def same_category(self) -> bool:
        return len({i.get("category", "") for i in self.items}) == 1

    @property
    def same_location(self) -> bool:
        return len({i.get("location", "") or i.get("group", "") for i in self.items}) == 1

    @property
    def likely_duplicate(self) -> bool:
        """同名 + 同规格 + 同类别 + 同位置，才够格判为重复记录。

        只比规格是不够的。实测踩到：立铣刀在备件表（A-007，Φ10）和工具表
        （铣-001，Φ10）里各有一条，规格相同，但一个是备件、一个是工具，
        合并就把两类东西混成一个节点了。
        """
        return self.same_spec and self.same_category and self.same_location

    def describe(self) -> str:
        parts = [f"{i['id']}({i['category']}"
                 f"{'/' + i['spec'] if i.get('spec') else ''}"
                 f"@{i.get('location') or i.get('group') or '-'})" for i in self.items]
        return " + ".join(parts)


def find_duplicates(parts: list[dict]) -> list[DuplicateGroup]:
    """找出名称完全相同的多条目。"""
    by_name: dict[str, list[dict]] = {}
    for p in parts:
        by_name.setdefault(p["name"], []).append(p)
    return [DuplicateGroup(name=n, items=v) for n, v in sorted(by_name.items())
            if len(v) > 1]


def rule_based_candidates(parts: list[dict]) -> tuple[list[Candidate], list[Candidate]]:
    """规则能判的返回 (疑似重复, 需要人工看)。

    四个条件全中才自动合并：同名、同规格、同类别、同位置。
    只中一部分的交给人工 —— 例如同名的备件和工具，字面上分不出是不是一个东西。
    """
    dup, review = [], []
    for g in find_duplicates(parts):
        ids = [i["id"] for i in g.items]
        for a, b in itertools.combinations(ids, 2):
            if g.likely_duplicate:
                dup.append(Candidate(a=a, b=b, confidence=0.85, kind="疑似重复",
                                     reason=f"{g.name}：{g.describe()}"))
            else:
                why = []
                if not g.same_spec:
                    why.append("规格不同")
                if not g.same_category:
                    why.append("类别不同")
                if not g.same_location:
                    why.append("位置不同")
                review.append(Candidate(a=a, b=b, confidence=0.5,
                                        kind="同名待判" + "/".join(why),
                                        reason=f"{g.name}：{g.describe()}"))
    return dup, review


def containment_pairs(parts: list[dict]) -> list[tuple[str, str, str, str]]:
    """找出名称之间有包含关系的对，返回 (编号A, 名称A, 编号B, 名称B)。

    **返回编号而不是名称。** 踩过的坑：早期版本返回名称，
    于是待确认队列里混了两种标识 —— 同名的那些是编号（A-004），
    包含关系的那些是名称（刀架）。批量处理时按编号发的决策匹配不上名称，
    两组被静默跳过；而且名称对写进归一表也没用，建图只按编号合并。

    只对**不同条目**之间找包含关系。同名条目由 find_duplicates 负责。
    """
    out, seen = [], set()
    for a, b in itertools.combinations(parts, 2):
        na, nb = a["name"], b["name"]
        if not na or not nb or na == nb:
            continue
        short, long_ = (a, b) if len(na) < len(nb) else (b, a)
        if short["name"] in long_["name"]:
            key = (short["id"], long_["id"])
            if key not in seen:
                seen.add(key)
                out.append((short["id"], short["name"], long_["id"], long_["name"]))
    return out


CONTAINMENT_PROMPT = """下面每一行是工业备件或工具的两个名称，其中一个是另一个的子串。

请判断每一对的关系，分三种：

- `same`  —— 同一个东西的简称和全称，应该合并
- `part`  —— 一个是另一个的部件或配件，**不能合并**（合并会抹掉装配层级）
- `none`  —— 只是恰好有共同的字，无关

判断依据是工业常识：名称里带「总成」「组」的通常是整体，
而主体名加具体部位（如「焊枪」和「焊枪喷嘴」）是部件关系。

{lines}

输出 JSON 数组，每条含 pair 下标、关系和理由：

```json
[
  {{"i": 0, "relation": "part", "reason": "焊枪喷嘴是焊枪的部件"}},
  {{"i": 1, "relation": "same", "reason": "刀架总成就是刀架"}}
]
```

只输出 JSON 数组，不要解释。"""


def build_containment_prompt(pairs: list[tuple[str, str, str, str]]) -> str:
    """给模型看名称（可读），但下标对应的是编号对。"""
    lines = "\n".join(f"{i}. {na}  /  {nb}"
                      for i, (_, na, _, nb) in enumerate(pairs))
    return CONTAINMENT_PROMPT.format(lines=lines)


def parse_containment(text: str,
                      pairs: list[tuple[str, str, str, str]]) -> list[Candidate]:
    """解析包含关系的判定结果，返回**编号对**。

    part 关系不进候选（不能合并），same 进待确认，none 丢弃。
    """
    import json
    import re
    s = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.DOTALL)
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
        try:
            i = int(item.get("i", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= i < len(pairs)):
            continue
        rel = str(item.get("relation", "")).strip().lower()
        if rel != "same":
            continue
        ia, na, ib, nb = pairs[i]
        out.append(Candidate(a=ia, b=ib, confidence=0.7, kind="同名包含",
                             reason=f"{na} / {nb}　"
                                    + str(item.get("reason", "")).strip()))
    return out
