"""人工决策的记录。

归一那一步会产生三类决定：合并、保留、跳过。合并的结果落在 synonyms.json，
但**保留和跳过也要记下来** —— 它们是人工判断，重跑管线时会用到。

踩过的坑：最初把决策和待确认队列放在同一个文件（pending.json）。重跑 resolve.py
会从 clean/*.json 重新生成候选，把整个队列覆盖掉，连带把人工决定冲没了。
所以决策单独存一个文件，只增不改。
"""

from __future__ import annotations

import json
from pathlib import Path

from app import config

PATH = config.DATA_DIR / "resolve_decisions.json"


def load() -> dict:
    if not PATH.exists():
        return {"_note": "人工对归一候选的决定。merge 的落在 synonyms.json，"
                         "keep / skip 记在这里。重跑管线不会清空本文件。",
                "decisions": []}
    return json.loads(PATH.read_text(encoding="utf-8"))


def record(pairs: list[tuple[str, str]], action: str,
           reason: str = "") -> int:
    """记一条或多条决定。已存在的对不重复记。"""
    data = load()
    seen = {(d["a"], d["b"]) for d in data["decisions"]}
    added = 0
    for a, b in pairs:
        key = tuple(sorted((a, b)))
        if key in seen or (a, b) in seen or (b, a) in seen:
            continue
        data["decisions"].append({"a": a, "b": b, "action": action,
                                  "reason": reason})
        seen.add((a, b))
        added += 1
    if added:
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return added


def decided_pairs() -> set[str]:
    """所有被决定过的名字（合并别名 + 保留/跳过的两端）。

    归一候选生成时用这个排除已处理项 —— 不然重跑一次，队列里会冒出一批
    早就处理完的对，看起来像没生效。
    """
    out: set[str] = set()
    for d in load()["decisions"]:
        out |= {d["a"], d["b"]}
    return out


def kept_pairs() -> list[dict]:
    return [d for d in load()["decisions"] if d["action"] in ("keep", "skip")]
