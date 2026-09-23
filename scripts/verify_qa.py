"""用独立路径验证问题集的标准答案。

    python scripts/verify_qa.py

标准答案是从图算的。但如果建图逻辑有错，图和参考查询会共享同一个错误假设，
自洽地错。这个脚本从 clean/*.json 用纯 Python 重算一遍，两条路径对比。

**它验不了的事**：如果两边对源数据的理解都错了（比如都把依赖方向理解反），
这里会一致通过。那种错只能靠人工回源文件核对。所以结论要这么读：

    一致  -> 建图没引入错误（但语义理解是否正确仍未验证）
    不一致 -> 一定有问题，查建图或参考查询
"""

from __future__ import annotations

import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                     # noqa: BLE001
    pass

from app import config
from app.eval.verify_qa import SourceGraph, compare

QA = config.DATA_DIR / "qa_set.json"


def main() -> int:
    print("=" * 70)
    print("标准答案的独立验证")
    print("=" * 70)

    if not QA.exists():
        print(f"  找不到 {QA}，先跑 python scripts/build_qa.py")
        return 1

    items = json.loads(QA.read_text(encoding="utf-8"))["items"]
    sg = SourceGraph(config.DATA_DIR / "clean",
                     config.DATA_DIR / "synonyms.json")

    print(f"  题目 {len(items)} 道")
    print(f"  独立路径：直接读 data/clean/*.json，用纯 Python 重算，不经 Neo4j")
    print()

    bad = compare(items, sg)

    # 统计哪些层参与验证了
    from collections import Counter
    layers = Counter(i["layer"] for i in items if i["layer"] != "REFUSE")
    checked = Counter(i["layer"] for i in items
                      if i["layer"] != "REFUSE")
    print(f"  参与验证的层：{dict(sorted(layers.items()))}")
    print()

    if not bad:
        print("  [OK] 两条路径答案完全一致")
    else:
        print(f"  [!!] {len(bad)} 道不一致：")
        for b in bad[:15]:
            print(f"\n    {b['id']}  {b['question'][:44]}")
            print(f"      {b['why']}")
            if "图算的" in b:
                print(f"      图算的  : {b['图算的']}")
                print(f"      独立重算: {b['独立重算']}")
        if len(bad) > 15:
            print(f"\n    …… 另有 {len(bad) - 15} 道")

    out = config.DATA_DIR / "qa_verify.json"
    out.write_text(json.dumps({"mismatch": bad}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n  明细 {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
