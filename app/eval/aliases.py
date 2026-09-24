"""答案里的实体别名表。

**为什么需要这个**

标准答案是图算的，返回 id；模型回答同一批对象时经常返回 name。
问「哪些备件没有安全库存」，答「刀架」比答「A-001」更自然 —— 两者都对。

但 judge 只比值本身，不知道 `A-001` 和「刀架」是同一个东西。实测 B3-088：
模型给的 39 项**与标准答案是同一批对象（交集 39/39）**，只因返回 name 而
标准答案存 id，被判 0/39。这是判定 bug，不是能力差异。

**为什么值是「id 集合」而不是单个 id**

备件名会重复：95 个备件里 12 个重名，「主轴轴承」有两台。
**名字到 id 不是函数**，硬映射成一个 id 会指错 —— 实测 B4-104 就栽在这：
模型返回的「主轴轴承」确实在并列区内，却被映射到区外的同名备件，判成错。

所以表的值是一个集合：这个名字可能指代的全部 id。

**为什么表这么小**

得同时有 id 和 name 的实体才有别名。核过一遍：
`SparePart` 有（id=A-001, name=硬质合金车刀片），
`Equipment` 只有 id，`FailureMode` / `SafetyMeasure` / `Location` 只有 name。
所以目前只有备件这一类。建表按「哪些文件同时有 id 和 name 字段」推导，
不写死文件名 —— 以后加了别的实体，这里会自动跟上。
"""

from __future__ import annotations

import json
from pathlib import Path

# 一个实体同时有这两个字段，才算有别名
ID_FIELD = "id"
NAME_FIELD = "name"


def build(clean_dir: Path) -> dict[str, set[str]]:
    """扫 clean 目录，建「任意写法 -> 可能指代的 id 集合」。

    id 自己映射到 `{自己}`，这样调用方不用分情况。
    """
    table: dict[str, set[str]] = {}
    if not clean_dir.is_dir():
        return table

    for path in sorted(clean_dir.glob("*.json")):
        rows = _load_rows(path)
        if not rows or not isinstance(rows[0], dict):
            continue
        if ID_FIELD not in rows[0] or NAME_FIELD not in rows[0]:
            continue

        for row in rows:
            canon = row.get(ID_FIELD)
            if canon is None or not str(canon).strip():
                continue
            canon = str(canon).strip()
            table.setdefault(canon, set()).add(canon)
            name = row.get(NAME_FIELD)
            if name is not None and str(name).strip():
                # 用 add 不用赋值 —— 重名的每一个 id 都要留在集合里
                table.setdefault(str(name).strip(), set()).add(canon)

    return table


def _load_rows(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return data
    # 有些 clean 文件是 {key: [...], ...} 的形态，挑第一个列表
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def ids_of(value: str, table: dict[str, set[str]]) -> set[str]:
    """一个取值可能指代的全部 id。表里没有就当成它自己。"""
    return table.get(value) or {value}


def match(got: set[str], want: set[str],
          table: dict[str, set[str]]) -> tuple[set[str], set[str]]:
    """按**实体重叠**配对，返回 (命中的 got, 命中的 want)。

    不用字面相等：`A-001` 和「刀架」要能配上，重名的「主轴轴承」要能跟
    它的任意一个 id 配上。两边都可能有歧义，所以按集合相交判。
    """
    if not table:
        return got & want, got & want
    m_got = {g for g in got if any(ids_of(g, table) & ids_of(w, table)
                                   for w in want)}
    m_want = {w for w in want if any(ids_of(g, table) & ids_of(w, table)
                                     for g in got)}
    return m_got, m_want


def forms(table: dict[str, set[str]]) -> dict[str, set[str]]:
    """id -> 它的全部写法。文本匹配要用。

    文档链路的答案是一段散文，里面写的是 name；标准答案存的是 id。
    想判它答没答对，得拿 name 去散文里找，不能拿 id 去找。
    """
    out: dict[str, set[str]] = {}
    for surface, ids in table.items():
        for i in ids:
            out.setdefault(i, set()).add(surface)
    return out
