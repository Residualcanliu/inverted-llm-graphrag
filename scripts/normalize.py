"""① 规范化：原始文件 → 结构化 JSON。

     python scripts/normalize.py

读 data/raw/ 下的原始文件，清掉五类格式问题，写到 data/clean/。
同时产出一份处理报告，记录每一步做了什么。

不依赖数据库，也不调模型。跑完可以立刻检查解析结果。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                    # noqa: BLE001
    pass

from app import config
from app.ingest import readers

CLEAN = config.DATA_DIR / "clean"
RAW = config.DATA_DIR / "raw"


def dump(name: str, data) -> Path:
    CLEAN.mkdir(parents=True, exist_ok=True)
    p = CLEAN / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def main() -> int:
    if not RAW.exists():
        print(f"找不到原始数据目录：{RAW}")
        print("把设备表备份解压到 data/raw/ 下再跑。")
        return 1

    print("=" * 70)
    print("① 规范化")
    print("=" * 70)
    print(f"  原始目录 {RAW}")
    print(f"  输出目录 {CLEAN}")
    print()

    eq = readers.read_equipment(str(RAW))
    print(f"[OK] 设备台账      {len(eq):>4} 台")
    dump("equipment.json", eq)

    sp = readers.read_spare_parts(str(RAW))
    n_bak = sum(1 for x in sp if x["category"] == "备件")
    n_tool = sum(1 for x in sp if x["category"] == "工具")
    print(f"[OK] 备件与工具    {len(sp):>4} 条  （备件 {n_bak} + 工具 {n_tool}）")
    dump("spare_parts.json", sp)

    rp = readers.read_repairs(str(RAW))
    dated = sum(1 for x in rp if x["date"])
    print(f"[OK] 检修记录      {len(rp):>4} 条  （带日期 {dated}）")
    dump("repairs.json", rp)

    flows, checks = readers.read_processes(str(RAW))
    n_steps = sum(len(f["steps"]) for f in flows)
    print(f"[OK] 工艺流程      {len(flows):>4} 条  （工序 {n_steps}）")
    dump("processes.json", flows)

    manuals = readers.read_manuals(str(RAW))
    n_fail = sum(len(m["failures"]) for m in manuals)
    n_safe = sum(len(m["safety"]) for m in manuals)
    print(f"[OK] 故障手册      {len(manuals):>4} 份  （故障 {n_fail} 条，安全注意 {n_safe} 条）")
    dump("manuals.json", manuals)

    # ---- 自检 ----
    print()
    print("台数校验（工艺流程图每格标了台数，展开后必须对上）")
    bad = [c for c in checks if c.startswith("[失败]")]
    for c in checks:
        print(f"    {c}")
    print(f"  {len(checks) - len(bad)}/{len(checks)} 通过")

    # ---- 对账所需的基准值 ----
    baseline = {
        "Equipment": len(eq),
        "SparePart": len(sp),
        "WorkOrder": len(rp),
        "Process": len(flows),
        "Operation": n_steps,
        "Location": len({x["shop"] for x in eq if x["shop"]})
                    + len({x["location"] for x in sp if x["location"]}),
        "DEPENDS_ON": _count_depends_on(flows),
    }
    dump("baseline.json", baseline)

    print()
    print("建图基准（第④步对账用这些数核对）")
    for k, v in baseline.items():
        print(f"    {k:<12} {v}")

    # ---- 处理报告 ----
    report = CLEAN / "normalize_report.md"
    report.write_text(_report(eq, sp, rp, flows, manuals, checks, baseline),
                      encoding="utf-8")
    print()
    print(f"处理报告 {report}")
    print(f"结构化产物 {CLEAN}/*.json")

    return 1 if bad else 0


def _count_depends_on(flows: list[dict]) -> int:
    """依赖边 = 相邻工序组两两相连。

    假设同工序的设备可互换，上游任一停机都会拖累下游全部设备。
    """
    n = 0
    for f in flows:
        steps = f["steps"]
        for a, b in zip(steps, steps[1:]):
            n += len({x for x in a["devices"]} ) * len({y for y in b["devices"]})
    return n


def _report(eq, sp, rp, flows, manuals, checks, baseline) -> str:
    n_bak = sum(1 for x in sp if x["category"] == "备件")
    n_tool = sum(1 for x in sp if x["category"] == "工具")
    n_hazard = sum(1 for x in sp if x.get("hazard"))
    n_low = sum(1 for x in sp if x["stock"] is not None and x["safety_stock"]
                and x["stock"] < x["safety_stock"])
    lines = [
        "# 规范化处理报告",
        "",
        "由 `scripts/normalize.py` 生成。记录本次读了什么、清掉了什么。",
        "",
        "## 产物",
        "",
        "| 文件 | 条数 |",
        "|---|---|",
        f"| equipment.json | {len(eq)} 台设备 |",
        f"| spare_parts.json | {len(sp)} 条（备件 {n_bak} + 工具 {n_tool}） |",
        f"| repairs.json | {len(rp)} 条检修记录 |",
        f"| processes.json | {len(flows)} 条流程 |",
        f"| manuals.json | {len(manuals)} 份手册 |",
        "",
        "## 处理过的格式问题",
        "",
        "源数据里的五种脏写法，都在 `app/ingest/normalize.py` 里处理：",
        "",
        "| 问题 | 例 |",
        "|---|---|",
        "| 编号省略前缀 | `tenlong-001、002、005、006` |",
        "| 区间省略前缀 | `chengxin-001～006、025～030` |",
        "| 「通用」后缀 | `tenlong/yuelong通用` |",
        "| 分隔符不统一 | `、` `，` `/` |",
        "| 规格嵌在名称里 | `硬质合金车刀片（CNMG120408）` |",
        "",
        "## 台数校验",
        "",
        "工艺流程图每格标了台数，展开后逐一核对。",
        "",
        "```",
        *checks,
        "```",
        "",
        "## 建图基准",
        "",
        "第④步对账用这些数字核对。任何一项对不上，说明管线有问题。",
        "",
        "| 指标 | 预期值 |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in baseline.items()],
        "",
        "## 数据特征",
        "",
        f"- 检修记录带日期：{sum(1 for x in rp if x['date'])}/{len(rp)}",
        f"- 高危工具：{n_hazard} 项",
        f"- 库存低于安全线的备件：{n_low} 项",
        f"- 有依赖边的设备：{len({d for f in flows for s in f['steps'] for d in s['devices']})} 台",
        "",
        "## 已知缺口",
        "",
        "- 切割设备（heyue）不在工艺流程里，源文档说明它是独立下料工序，"
        "因此在依赖网络上孤立。",
        "- 故障成因列是自由文本，里面的名字（如「磨粒钝化」「熔渣堵塞」）"
        "多数不在故障词表中，建 TRIGGERS 边需要人工映射。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
