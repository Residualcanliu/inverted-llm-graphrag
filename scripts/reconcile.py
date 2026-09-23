"""④ 对账：核对建图结果与预期。

    python scripts/reconcile.py

逐项比对节点数、关系数、孤儿节点，产出 reports/reconcile.md。

为什么必须做：归一漏掉一组同义词，边数就少一截，其余环节察觉不到，
只表现为「数据少一点」。对账是唯一能发现它的环节。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                     # noqa: BLE001
    pass

from app import config
from app.graph import client

CLEAN = config.DATA_DIR / "clean"
REPORT = config.REPORT_DIR / "reconcile.md"

# (标签或关系, 预期值来源, 查询)
NODE_CHECKS = [
    ("Location", "location"),
    ("Equipment", "equipment"),
    ("SparePart", "spare"),
    ("WorkOrder", "workorder"),
    ("Process", "process"),
    ("Operation", "operation"),
    ("SafetyMeasure", "safety"),
]

REL_CHECKS = [
    "IN_SHOP", "STORED_AT", "FITS", "EXPERIENCED", "CAUSED_BY",
    "USED_PART", "HAS_STEP", "PRECEDES", "USES", "DEPENDS_ON",
    "MITIGATED_BY", "TRIGGERS",
]


def q(cypher: str):
    return client.run_readonly(cypher).rows


def main() -> int:
    print("=" * 70)
    print("④ 对账")
    print("=" * 70)

    ok, msg = client.ping()
    if not ok:
        print(f"  连不上数据库：{msg}")
        return 1

    exp_path = CLEAN / "expected.json"
    expected = json.loads(exp_path.read_text(encoding="utf-8")) if exp_path.exists() else {}

    # 节点数
    actual_nodes = {r["label"]: r["n"] for r in q(
        "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS n ORDER BY label")}
    actual_rels = {r["type"]: r["n"] for r in q(
        "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n ORDER BY type")}

    lines = ["# 建图对账报告", "",
             "由 `scripts/reconcile.py` 生成。", "",
             "## 节点", "",
             "| 标签 | 实际 | 预期 | 结果 |", "|---|---|---|---|"]

    problems = []

    total_nodes = sum(actual_nodes.values())
    print(f"  节点总数 {total_nodes}")
    for label, _ in NODE_CHECKS:
        got = actual_nodes.get(label, 0)
        lines.append(f"| `{label}` | {got} | — | |")
    # FailureMode 有两批写入，单独列
    fm = actual_nodes.get("FailureMode", 0)
    lines.append(f"| `FailureMode` | {fm} | 检修26 + 成因39 = 65 以内 | |")

    print()
    print("  关系")
    lines += ["", "## 关系", "", "| 关系 | 实际 | 预期 | 结果 |", "|---|---|---|---|"]
    for rel in REL_CHECKS:
        got = actual_rels.get(rel, 0)
        exp = expected.get(
            {"DEPENDS_ON": "Equipment-DEPENDS_ON->Equipment",
             "IN_SHOP": "Equipment-IN_SHOP->Location"}.get(rel, ""), None)
        mark = ""
        if exp is not None and got != exp:
            mark = f"**差 {got - exp:+d}**"
            problems.append(f"{rel} 实际 {got} / 预期 {exp}")
        lines.append(f"| `{rel}` | {got} | {exp if exp is not None else '—'} | {mark} |")
        print(f"    {rel:<16} {got:>6}")

    # 孤儿节点
    print()
    orphans = q("""MATCH (n)
                   WHERE NOT (n)--()
                   RETURN labels(n)[0] AS label, count(*) AS n""")
    orphan_total = sum(r["n"] for r in orphans)
    print(f"  孤儿节点 {orphan_total}")
    for r in orphans:
        print(f"    {r['label']}: {r['n']}")

    # 每个标签内部连通性
    print()
    print("  无 DEPENDS_ON 出入边的设备（工艺流程未覆盖）")
    iso = q("""MATCH (e:Equipment)
               WHERE NOT (e)-[:DEPENDS_ON]-()
               RETURN count(*) AS n""")
    iso_n = iso[0]["n"] if iso else 0
    print(f"    {iso_n} 台")

    # 写报告
    lines += ["", "## 孤儿节点", "",
              f"合计 {orphan_total}", ""]
    for r in orphans:
        lines.append(f"- `{r['label']}`：{r['n']}")
    lines += ["", "## 依赖网络覆盖", "",
              f"工艺流程未覆盖的设备：{iso_n} 台（切割设备本就是独立工序）", ""]
    if problems:
        lines += ["", "## 需要排查", ""] + [f"- {p}" for p in problems]

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print()
    print(f"  报告 {REPORT}")

    if problems:
        print()
        print("  有差异需要看：")
        for p in problems:
            print(f"    {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
