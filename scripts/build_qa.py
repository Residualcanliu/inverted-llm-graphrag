"""生成评测问题集。

    python scripts/build_qa.py

按题型套模板生成候选，标准答案由**手写的参考 Cypher** 从图上算出。
生成的是候选，还要人工审三件事：答案唯不唯一、在不在数据里、措辞有没有歧义。
"""

from __future__ import annotations

import json
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                     # noqa: BLE001
    pass

from app import config
from app.eval.qa_gen import QABuilder

OUT = config.DATA_DIR / "qa_set.json"

LAYER_NAME = {
    "A": "A 类 事实查找", "B1": "B1 多跳依赖", "B2": "B2 聚合统计",
    "B3": "B3 否定与补集", "B4": "B4 排序与阈值", "B5": "B5 根因追溯",
    "REFUSE": "拒答",
}


def audit(items: list) -> tuple[list[str], list[str]]:
    """出完题自检。返回 (真问题, 供人工看的提示)。

    只报「答案空、答案缺失」这类硬伤；「排序题答案可能并列」是已知的设计取舍，
    不该刷屏，归到提示里。
    """
    problems, hints = [], []
    for it in items:
        if it.layer == "REFUSE":
            continue
        vals = list(it.answer.values())
        if not vals or all(v in (None, "", []) for v in vals):
            problems.append(f"{it.id} 答案是空的：{it.question}")
            continue
        for k, v in it.answer.items():
            if isinstance(v, list) and len(v) == 0:
                problems.append(f"{it.id} 集合为空（{k}）：{it.question}")
        if it.layer == "B4":
            hints.append(f"{it.id} 排序题，判分按集合算")
        # 答案规模异常小的集合题，值得人工看一眼
        for k, v in it.answer.items():
            if isinstance(v, list) and 0 < len(v) <= 2:
                hints.append(f"{it.id} 答案只有 {len(v)} 项（{k}）：{it.question[:30]}")
    return problems, hints


def main() -> int:
    print("=" * 70)
    print("生成评测问题集")
    print("=" * 70)

    ok, msg = __import__("app.graph.client", fromlist=["client"]).ping()
    if not ok:
        print(f"  连不上图数据库：{msg}")
        return 1

    items = QABuilder(seed=42).build_all()
    print(f"  共生成 {len(items)} 道\n")

    print(f"  {'层':<18}{'题量':>6}")
    print("  " + "-" * 26)
    for layer, n in sorted(Counter(i.layer for i in items).items(),
                           key=lambda x: list(LAYER_NAME).index(x[0])):
        print(f"  {LAYER_NAME[layer]:<18}{n:>6}")

    problems, hints = audit(items)
    if problems:
        print(f"\n  [需修] {len(problems)} 条：")
        for p in problems[:10]:
            print(f"    {p}")
    else:
        print("\n  [OK] 没有空答案的题")

    if hints:
        print(f"\n  [人工审时留意] {len(hints)} 条")
        by_kind = {}
        for h in hints:
            key = h.split(" ", 1)[1].split("：")[0]
            by_kind[key] = by_kind.get(key, 0) + 1
        for k, v in sorted(by_kind.items(), key=lambda x: -x[1]):
            print(f"    {v:>3} 条  {k}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "_note": "评测问题集。标准答案由 ref_cypher 从图上算出，与四条链路无关。"
                 "生成后需人工审：答案唯不唯一、在不在数据里、措辞有没有歧义。",
        "items": [i.to_dict() for i in items],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  已写入 {OUT}")

    sheet = config.DATA_DIR / "qa_review.md"
    sheet.write_text(render_review(items), encoding="utf-8")
    print(f"  审阅清单 {sheet}")
    print("  下一步：人工审一遍，然后 scripts/run_eval.py")
    return 0


def render_review(items: list) -> str:
    """出一份给人看的审阅清单。

    **按「答案离原始数据有多远」排序，不按题层。** 这条链的中间人是 AI，
    不是领域专家，所以答案的可信度差别很大：直读台账的题几乎不可能错，
    而「某个故障会导致哪些故障」是我从一段自由文本里解释出来的，最需要人看。
    按可信度排，审的人才能把力气花在刀刃上。
    """
    from app.eval.qa_gen import DERIVATION

    order = ["interpreted", "relational", "negation", "aggregate", "direct"]
    by_d = {}
    for it in items:
        by_d.setdefault(it.derivation, []).append(it)

    lines = [
        "# 问题集审阅清单",
        "",
        f"共 {len(items)} 道。由 `scripts/build_qa.py` 生成。",
        "",
        "**标准答案的中间人是 AI，不是领域专家。** 这条链是：",
        "",
        "```",
        "源文件 → 规范化 → 归一 → 建图 → 手写参考 Cypher → 标准答案",
        "```",
        "",
        "每一环都由 AI 完成。所以答案离原始数据越远，越需要人核。清单**按这个距离排序**，",
        "最需要看的在最前面。后面几类可以快速扫过。",
        "",
        "## 各层可信度",
        "",
        "| 层级 | 题量 | 含义 |",
        "|---|---|---|",
    ]
    for d in order:
        if d in by_d:
            lines.append(f"| {d} | {len(by_d[d])} | {DERIVATION[d]} |")

    lines += [
        "",
        "## 审这三件事",
        "",
        "1. **答案唯一吗** —— 会不会并列。实测撞到过 33 台设备并列同一分值的情况",
        "2. **答案在数据里吗** —— 问的东西真的存在吗",
        "3. **措辞有歧义吗** —— 两种合理解读会不会导致不同答案",
        "",
        "**对 `interpreted` 和 `relational` 两类，还要多问一句：这个推导的依据，"
        "源文件里真的是这么说的吗？** 这两类依赖的是 AI 对语义的理解，"
        "而不是数据直接给出的事实。",
        "",
        "有问题在下面标 `[改]` 或 `[删]`，改完重跑评测即可。",
        "",
    ]

    for d in order:
        group = by_d.get(d)
        if not group:
            continue
        lines += ["", "---", "",
                  f"## {d}（{len(group)} 道）", "",
                  f"> {DERIVATION[d]}", ""]
        for it in group:
            ans = json.dumps(it.answer, ensure_ascii=False)
            if len(ans) > 200:
                ans = ans[:200] + " …"
            lines.append(f"**{it.id}**　{it.question}")
            lines.append("")
            lines.append(f"- 答案（{it.answer_type}）：`{ans}`")
            if it.note:
                lines.append(f"- 备注：{it.note}")
            lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
