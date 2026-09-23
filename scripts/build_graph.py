"""③ 建图：结构化数据 → Neo4j。

    python scripts/build_graph.py
    python scripts/build_graph.py --reset      # 先清空再建

读 data/clean/ 的 JSON、data/synonyms.json 的归一表、data/causes.json 的因果链，
按批次写入 Neo4j。全部用 MERGE，可以反复跑。

待确认的合并对**不合并**。图谱宁可少合并几个，也不错误合并。
前端的确认界面处理完，改 synonyms.json 后重跑本脚本即可。

这是整个项目唯一会写数据库的脚本。查询链路走 READ_ACCESS，服务端强制只读。
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                     # noqa: BLE001
    pass

from app import config
from app.graph import client, loader

CLEAN = config.DATA_DIR / "clean"


def main() -> int:
    ap = argparse.ArgumentParser(description="③ 建图")
    ap.add_argument("--reset", action="store_true", help="建之前先清空数据库")
    args = ap.parse_args()

    print("=" * 70)
    print("③ 建图")
    print("=" * 70)

    if not CLEAN.exists():
        print(f"找不到 {CLEAN}，先跑 python scripts/normalize.py")
        return 1

    ok, msg = client.ping()
    print(f"  数据库 {msg}")
    if not ok:
        print("  连不上。docker compose up -d 起容器。")
        return 1

    if args.reset:
        print("  清空数据库 ...")
        client.wipe()

    d = loader.load(CLEAN, config.DATA_DIR / "synonyms.json",
                    config.DATA_DIR / "causes.json")

    syn = d.synonyms
    print(f"  归一表 故障模式 {len(syn.get('故障模式', {}))} 条，"
          f"备件工具 {len(syn.get('备件工具', {}))} 条")
    print(f"  因果链 {len(d.causes)} 条")
    print()

    batches = loader.all_batches(d)
    total_ms = 0
    for b in batches:
        t0 = time.time()
        try:
            r = client.run_write(b.cypher, b.params)
        except Exception as e:                        # noqa: BLE001
            print(f"  [失败] {b.name:<38} {type(e).__name__}: {str(e)[:80]}")
            return 1
        ms = int((time.time() - t0) * 1000)
        total_ms += ms
        print(f"  [OK]   {b.name:<38} {r.counters:<22} {ms:>5} ms")
        b.note = r.counters

    print()
    print(f"  {len(batches)} 批全部写入，耗时 {total_ms} ms")

    # 落一份预期值给对账用
    import json
    expect = {b.name: b.expected for b in batches}
    (CLEAN / "expected.json").write_text(
        json.dumps(expect, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  预期值已写入 {CLEAN / 'expected.json'}")
    print()
    print("  下一步：python scripts/reconcile.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
