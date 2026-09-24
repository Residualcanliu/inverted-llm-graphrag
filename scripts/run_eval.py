"""跑对照实验。

    python scripts/run_eval.py                 # 全部链路
    python scripts/run_eval.py --only inverted # 只跑一条
    python scripts/run_eval.py --limit 5       # 先跑 5 道试水

四条链路 × 153 道题，跑一轮十几分钟。结果逐题落盘到 eval_runs/<链路>.jsonl，
**已存在的行会跳过** —— 中断了直接重跑就行，不会从头再来。

判定走 app/eval/judge.py，统计走 app/eval/stats.py。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                     # noqa: BLE001
    pass

from app import config
from app.eval import judge as J
from app.eval import stats as S
from app.pipelines import PIPELINES, all_pipelines, get

QA = config.DATA_DIR / "qa_set.json"
OUT = config.ROOT / "eval_runs"

LAYER_NAME = {"A": "A 类 事实", "B1": "B1 多跳", "B2": "B2 聚合",
              "B3": "B3 补集", "B4": "B4 排序", "B5": "B5 根因",
              "REFUSE": "拒答"}


def load_done(path) -> dict[str, dict]:
    """读已跑完的题。断点续跑用。"""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["qid"]] = r
    return out


def run_one(pipe_name: str, items: list[dict], *, use_llm: bool) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{pipe_name}.jsonl"
    done = load_done(path)

    todo = [it for it in items if it["id"] not in done]
    if not todo:
        print(f"  {pipe_name:<14} 已全部跑完（{len(done)} 题），跳过")
        return 0

    pipe = get(pipe_name, seed=42)
    pipe.warmup() if hasattr(pipe, "warmup") else None
    print(f"  {pipe_name:<14} {len(todo)} 题待跑（已完成 {len(done)}）")

    t0 = time.time()
    with path.open("a", encoding="utf-8") as f:
        for i, it in enumerate(todo, 1):
            try:
                ans = pipe.answer(it["question"])
                v = J.judge(it, ans, use_llm=use_llm)
            except Exception as e:                    # noqa: BLE001
                v = J.Verdict(it["id"], pipe_name, False, 0.0, "error",
                              f"{type(e).__name__}: {e}")
            rec = {**v.to_dict(), "layer": it["layer"],
                   "question": it["question"]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if i % 10 == 0 or i == len(todo):
                el = time.time() - t0
                print(f"    {i}/{len(todo)}  已用 {el:.0f}s  "
                      f"预计还要 {el / i * (len(todo) - i):.0f}s")
    return 0


def report(names: list[str], use_llm: bool) -> str:
    data: dict[str, dict[str, dict]] = {}
    for n in names:
        data[n] = load_done(OUT / f"{n}.jsonl")

    all_q = sorted({q for d in data.values() for q in d})
    lines = ["# 对照实验结果", "",
             f"题目 {len(all_q)} 道，链路 {len(names)} 条。", ""]

    # ---- 总体 ----
    lines += ["## 总体准确率", "",
              "| 链路 | 正确 | 准确率 | 95% CI |", "|---|---|---|---|"]
    scores: dict[str, list[float]] = {}
    for n in names:
        vals = [data[n][q]["score"] if q in data[n] else 0.0 for q in all_q]
        scores[n] = vals
        k = sum(1 for v in vals if v >= 0.999)
        lo, hi = S.wilson(k, len(vals))
        label = getattr(PIPELINES.get(n), "label", n) if n in PIPELINES else \
            ("①' 全语料" if n == "doc_rag_full" else n)
        lines.append(f"| {label} | {k}/{len(vals)} | {k/len(vals):.1%} "
                     f"| [{lo:.1%}, {hi:.1%}] |")

    # ---- 分层 ----
    lines += ["", "## 分层准确率", ""]
    layers = sorted({data[n][q]["layer"] for n in names for q in data[n]})
    head = "| 层 | 题量 | " + " | ".join(names) + " |"
    lines += [head, "|---|---|" + "---|" * len(names)]
    for ly in layers:
        qs = [q for q in all_q if data[names[0]].get(q, {}).get("layer") == ly]
        cells = []
        for n in names:
            k = sum(1 for q in qs if data[n].get(q, {}).get("score", 0) >= 0.999)
            cells.append(f"{k}/{len(qs)}" if qs else "-")
        lines.append(f"| {LAYER_NAME.get(ly, ly)} | {len(qs)} | "
                     + " | ".join(cells) + " |")

    # ---- 两两对比 ----
    lines += ["", "## 两两对比（配对检验）", "",
              "| 对比 | 差值 | 95% CI | 不一致对 | 显著 | 说明 |",
              "|---|---|---|---|---|---|"]
    base = "doc_rag" if "doc_rag" in names else names[0]
    for n in names:
        if n == base:
            continue
        c = S.compare(base, n, scores[base], scores[n])
        ci = f"[{c.ci_low:+.1%}, {c.ci_high:+.1%}]"
        lines.append(f"| {base} → {n} | {c.diff:+.1%} | {ci} "
                     f"| {c.b_only + c.a_only} | "
                     f"{'是' if c.significant else '否'} | {c.note} |")

    # ---- 样本量说明 ----
    md = S.min_detectable(len(all_q))
    lines += ["", "## 这个样本量能说明什么", "",
              f"共 {len(all_q)} 道配对问题。按 80% 功效估算，"
              f"能可靠检出的最小差距约 **{md:.1%}**。", "",
              "更小的差异测不出来，而**不显著不等于没用** —— "
              "样本量不足会同时造成假阳性和假阴性（这条在别处踩过，"
              "同一批数据从 102 条扩到 252 条之后两组结论都翻转了）。", "",
              "分层之后每层只有 20–40 道，功效更低，所以分层表只报准确率，"
              "不做显著性声称。", ""]

    # ---- 判定方式分布 ----
    lines += ["", "## 判定方式分布", "",
              "| 链路 | 数值 | 集合 | 排序 | 文本 | LLM 兜底 | 拒答 | 出错 |",
              "|---|---|---|---|---|---|---|---|"]
    for n in names:
        m = defaultdict(int)
        for r in data[n].values():
            m[r["method"]] += 1
        lines.append(f"| {n} | {m['number']} | {m['set']} | {m['tie']} "
                     f"| {m['text']} | {m['llm']} | {m['refuse']} | {m['error']} |")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="+", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-llm", action="store_true",
                    help="文档链路判分时不用 LLM 兜底")
    args = ap.parse_args()

    if not QA.exists():
        print(f"找不到 {QA}，先跑 python scripts/build_qa.py")
        return 1
    items = json.loads(QA.read_text(encoding="utf-8"))["items"]
    if args.limit:
        items = items[:args.limit]

    names = args.only or (list(PIPELINES) + ["doc_rag_full"])
    print("=" * 70)
    print(f"对照实验  {len(items)} 题 × {len(names)} 条链路")
    print("=" * 70)

    for n in names:
        run_one(n, items, use_llm=not args.no_llm)

    print()
    rep = report(names, use_llm=not args.no_llm)
    out = config.REPORT_DIR / "eval_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rep, encoding="utf-8")
    print(rep)
    print(f"\n报告 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
