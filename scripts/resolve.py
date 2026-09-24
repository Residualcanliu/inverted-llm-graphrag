"""② 归一：找出不同叫法，合并到同一个节点。

     python scripts/resolve.py

读 data/clean/ 的结构化数据，把三组名称交给模型找同义候选，
按阈值分成「自动合并」「待确认」「丢弃」，写进 data/synonyms.json 和 data/pending.json。

synonyms.json 是人工维护的资产，进仓库。pending.json 是临时队列，不进。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                    # noqa: BLE001
    pass

from app import config
from app.ingest import decisions as D
from app.ingest import resolve as R
from app.ingest import spare_resolve as SR
from app.llm import ollama_client as oc

CLEAN = config.DATA_DIR / "clean"
SYNONYMS = config.DATA_DIR / "synonyms.json"
PENDING = config.DATA_DIR / "pending.json"
CAUSES = config.DATA_DIR / "causes.json"


def load(name: str):
    p = CLEAN / name
    if not p.exists():
        print(f"缺少 {p}，先跑 python scripts/normalize.py")
        raise SystemExit(1)
    return json.loads(p.read_text(encoding="utf-8"))


def collect_names(repairs, manuals) -> tuple[list[str], list[str], list[str]]:
    """收集三组名称。

    故障名和现象名可能有交集，这里不去重 —— 让模型看到完整上下文更有助于判断。
    """
    from_repairs = sorted({r["failure"] for r in repairs if r["failure"]})
    from_symptoms = sorted({f["symptom"] for m in manuals for f in m["failures"] if f["symptom"]})
    # 成因用拆开后的列表，不要用原话
    from_causes = sorted({c for m in manuals for f in m["failures"]
                          for c in f.get("causes", []) if c})
    return from_repairs, from_symptoms, from_causes


def write_synonyms(pairs: list[R.Candidate], existing: dict | None = None) -> dict:
    """合成归一表。

    保留人工确认过的条目，把新自动合并的并进去。
    """
    data = existing or {"_note": "别名 → 主名。人工可编辑，建图时按此合并。",
                        "故障模式": {}}
    merged = R.to_synonym_map(pairs)
    data.setdefault("故障模式", {})
    data["故障模式"].update(merged)
    return data


def _write(path: Path, data, suffix: str, dry: bool) -> None:
    if dry:
        return
    out = path.with_name(path.stem + suffix + path.suffix)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="② 归一")
    ap.add_argument("--model", default=None, help="换模型跑（默认用 config.GEN_MODEL）")
    ap.add_argument("--tag", default="", help="输出文件名后缀，用于对比实验不覆盖原结果")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args()
    model = args.model
    suffix = f".{args.tag}" if args.tag else ""

    print("=" * 70)
    print("② 归一")
    if model:
        print(f"   模型覆盖为 {model}")
    print("=" * 70)

    repairs = load("repairs.json")
    manuals = load("manuals.json")
    pend_prev = (json.loads(PENDING.read_text(encoding="utf-8"))
                 if PENDING.exists() else {})
    from_repairs, from_symptoms, from_causes = collect_names(repairs, manuals)

    print(f"  检修表故障名   {len(from_repairs)} 个")
    print(f"  手册现象名     {len(from_symptoms)} 个")
    print(f"  手册成因名     {len(from_causes)} 个")
    print()

    # 已确认过的名字直接从候选里排除，不重复问
    existing = json.loads(SYNONYMS.read_text(encoding="utf-8")) if SYNONYMS.exists() else None
    known = set((existing or {}).get("故障模式", {}).keys())
    if known:
        print(f"  已有归一表，跳过 {len(known)} 个已确认的别名")
        from_repairs = [x for x in from_repairs if x not in known]
        from_symptoms = [x for x in from_symptoms if x not in known]

    # ---- 第一问：同义（故障名 vs 现象名）----
    #
    # 成因不参与这一问。成因和故障不是同类概念，混在一起问，
    # 模型会把「粉尘油污附着」判成「镜片污染」的同义，而实际是因果关系。
    print("─" * 70)
    print("第一问：同义判定（检修表故障名 ↔ 手册现象名）")
    prompt = R.build_synonym_prompt(from_repairs, from_symptoms)
    try:
        g = oc.generate(prompt, model=model, num_predict=1024)
    except Exception as e:                            # noqa: BLE001
        print(f"调模型失败：{e}")
        print("确认 ollama 在跑，且 GEN_MODEL 存在。")
        return 1
    print(f"  模型 {g.model}  {g.elapsed_s:.1f}s  {g.eval_count} tok"
          f"{'  [截断]' if g.truncated else ''}")

    known = set(from_repairs) | set(from_symptoms) | known
    raw_cands = R.parse_candidates(g.raw)
    bad = R.rejected_candidates(raw_cands, known)
    if bad:
        print(f"  丢弃 {len(bad)} 组假名字：{[c.a for c in bad][:3]}")
    cands = R.normalize_pairs(R.validate_candidates(raw_cands, known))
    res = R.split_by_threshold(cands)

    print(f"  候选 {len(cands)} 组 -> {res.summary()}")
    print()
    if res.auto:
        print("  自动合并（≥ 0.9）")
        for c in res.auto:
            print(f"    {c.a}  ←  {c.b}   [{c.kind}] {c.confidence:.2f}  {c.reason[:38]}")
    if res.pending:
        print()
        print("  待确认（0.6 ~ 0.9）")
        for c in res.pending:
            print(f"    {c.a}  ↕  {c.b}   [{c.kind}] {c.confidence:.2f}  {c.reason[:38]}")

    data = write_synonyms(res.auto, existing)
    _write(SYNONYMS, data, suffix, args.dry_run)

    # ---- 第二问：因果（成因 → 现象）----
    print()
    print("─" * 70)
    print("第二问：因果判定（成因 → 故障现象）")
    print("  从手册直接抽，不让模型判。手册每行是「现象 | 成因 | 处理」配好的，")
    print("  这层对应关系文档已经写死，模型插一脚只会引入错误。")
    print()
    print("  实测反例：手册里「卡盘松动」是「主轴异响」那一行的成因，")
    print("  模型却判定它导致「工件尺寸超差」—— 文档里没有这个依据。")
    print("  而这类错误看起来合理，特别隐蔽。评测的 ground truth 必须来自文档，")
    print("  尤其不能来自跟被测系统同一个模型的推理，那是循环论证。")

    ok_links = R.causes_from_manuals(manuals)
    print(f"  从手册抽出 {len(ok_links)} 条，每条都是文档明说")
    for l in ok_links[:8]:
        print(f"    {l.cause}  →  {l.effect}")
    if len(ok_links) > 8:
        print(f"    …… 另有 {len(ok_links) - 8} 条")
    CAUSES.write_text(json.dumps({
        "_note": "故障成因 → 故障现象，直接从手册的「快速判断」列抽，"
                 "成因连到它所在那一行的现象。不经过模型判断。",
        "links": [l.to_dict() for l in ok_links],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 第三问：备件与工具的归一 ----
    print()
    print("─" * 70)
    print("第三问：备件与工具的归一")
    print("  规则能判一大半：同名同规格的疑似重复记录，直接进待确认。")
    parts = load("spare_parts.json")
    same, diff = SR.rule_based_candidates(parts)

    # 排除已处理项。合并过的在归一表里，保留/跳过的在 decisions 文件里。
    # 不排除的话，重跑一次队列里会冒出早就处理完的对，看起来像没生效。
    # （踩过：重跑一次冒出 11 组旧项，连带把 decided 的记录冲掉了。）
    done = set((existing or {}).get("备件工具", {})) | D.decided_pairs()
    same = [c for c in same if c.a not in done and c.b not in done]
    diff = [c for c in diff if c.a not in done and c.b not in done]
    groups = SR.find_duplicates(parts)
    print(f"  名称重复 {len(groups)} 组 -> 同规格 {len(same)} 对，不同规格 {len(diff)} 对")

    pairs = SR.containment_pairs(parts)
    print(f"  名称包含关系 {len(pairs)} 对，交给模型判")

    contain_cands = []
    if pairs:
        try:
            g3 = oc.generate(SR.build_containment_prompt(pairs), model=model, num_predict=2048)
            contain_cands = SR.parse_containment(g3.raw, pairs)
            print(f"  模型判定 {len(contain_cands)} 对属于同义（其余是部件关系或无关）")
            for c in contain_cands[:6]:
                print(f"    {c.a}  ←  {c.b}   {c.reason[:44]}")
        except Exception as e:                        # noqa: BLE001
            print(f"  判定失败：{e}")

    # 同规格的进自动合并，不同规格的和包含关系的进待确认
    spare_auto = same
    spare_pending = diff + contain_cands
    if spare_auto or spare_pending:
        spmap = R.to_synonym_map(spare_auto) if spare_auto else {}
        data.setdefault("备件工具", {})
        data["备件工具"].update(spmap)
        _write(SYNONYMS, data, suffix, args.dry_run)

    # ---- 落盘 ----
    _write(PENDING, {
        "_note": "待人工确认的同义候选。前端读取此队列，决策结果写回 synonyms.json。",
        "pending": [c.to_dict() for c in res.pending + spare_pending],
        "dropped": [c.to_dict() for c in res.dropped],
        # 决策记录在 data/resolve_decisions.json，不放这里。
        # 这个队列每次重跑都会重置，混在一起会把人工决定冲掉。
    }, suffix, args.dry_run)

    print()
    print("─" * 70)
    print(f"归一表   {SYNONYMS.name:<12} {len(data['故障模式'])} 条别名映射")
    print(f"待确认   {PENDING.name:<12} {len(res.pending) + len(spare_pending)} 组")
    print(f"因果链   {CAUSES.name:<12} {len(ok_links)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
