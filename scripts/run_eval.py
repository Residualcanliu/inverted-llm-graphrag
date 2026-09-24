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
from app.eval import aliases as A
from app.eval import stats as S
from app.pipelines import PIPELINES, all_pipelines, get

QA = config.DATA_DIR / "qa_set.json"
OUT = config.ROOT / "eval_runs"

# 实体别名表（备件的 id <-> name）。标准答案存 id，模型常答 name，
# 不归一的话答对了也判错。见 app/eval/aliases.py
ALIASES = A.build(config.DATA_DIR / "clean")

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


def capture(ans) -> dict:
    """把「判定要用的输入」存下来。

    `docs/评测框架.md` 第七节把流程拆成 ③跑 和 ⑤判定 两步，但实现把两步
    揉在一起了：落盘的是**判定结果**，不是判定的输入。于是每改一次判定逻辑
    就得把 153 道题连模型带数据库重跑一遍（十几分钟）。

    而判定恰恰是最容易改的一层 —— 这个项目里已经因为取值方式错过三次。
    存下输入之后，`--rejudge` 秒级就能重算。
    """
    return {
        "pipeline": getattr(ans, "pipeline", ""),
        "text": getattr(ans, "text", "") or "",
        "raw": getattr(ans, "raw", "") or "",
        "cypher": getattr(ans, "cypher", "") or "",
        "fields": getattr(ans, "fields", None) or {},
        "error": getattr(ans, "error", "") or "",
    }


def restore(pipe_name: str, question: str, d: dict):
    """把 capture 存下来的东西还原成一个能交给 judge 的对象。"""
    from app.pipelines.base import Answer

    return Answer(pipeline=d.get("pipeline") or pipe_name, question=question,
                  text=d.get("text", ""), fields=d.get("fields") or {},
                  cypher=d.get("cypher", ""), raw=d.get("raw", ""),
                  error=d.get("error", ""))


def rejudge(names: list[str], items: list[dict], *, use_llm: bool) -> int:
    """只重算判定，不重跑链路。前提是记录里有 capture 存下的输入。"""
    by_id = {it["id"]: it for it in items}
    n = 0
    for name in names:
        path = OUT / f"{name}.jsonl"
        if not path.exists():
            continue
        records = [json.loads(x) for x in
                   path.read_text(encoding="utf-8").splitlines() if x.strip()]
        out = []
        for r in records:
            it = by_id.get(r["qid"])
            if it is None or "captured" not in r:
                out.append(r)                        # 没存输入的原样留着
                continue
            ans = restore(name, r.get("question", ""), r["captured"])
            v = J.judge(it, ans, use_llm=use_llm, aliases=ALIASES)
            out.append({**v.to_dict(), "layer": it["layer"],
                        "question": r.get("question", ""),
                        "captured": r["captured"]})
            n += 1
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n"
                               for x in out), encoding="utf-8")
        tmp.replace(path)
        print(f"  {name:<14} 重算 {len(out)} 条")
    return n


def run_one(pipe_name: str, items: list[dict], *, use_llm: bool,
            with_posthoc: bool = False) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{pipe_name}.jsonl"
    done = load_done(path)

    todo = [it for it in items if it["id"] not in done]
    if not todo:
        print(f"  {pipe_name:<14} 已全部跑完（{len(done)} 题），跳过")
        return 0

    # include_posthoc 只有 ③ 认，别的链路没有这个开关，不能盲传给 get()
    kw = {"seed": 42}
    if pipe_name == "inverted":
        kw["include_posthoc"] = with_posthoc
    pipe = get(pipe_name, **kw)
    pipe.warmup() if hasattr(pipe, "warmup") else None
    print(f"  {pipe_name:<14} {len(todo)} 题待跑（已完成 {len(done)}）")

    t0 = time.time()
    with path.open("a", encoding="utf-8") as f:
        for i, it in enumerate(todo, 1):
            ans = None
            try:
                ans = pipe.answer(it["question"])
                v = J.judge(it, ans, use_llm=use_llm, aliases=ALIASES)
            except Exception as e:                    # noqa: BLE001
                v = J.Verdict(it["id"], pipe_name, False, 0.0, "error",
                              f"{type(e).__name__}: {e}")
            rec = {**v.to_dict(), "layer": it["layer"],
                   "question": it["question"],
                   "captured": capture(ans) if ans is not None else {}}
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
    ap.add_argument("--rejudge", action="store_true",
                    help="不重跑链路，只用已存下的答案重算判定（秒级）")
    ap.add_argument("--with-posthoc", action="store_true",
                    help="③ 启用评测集冻结后补的示例（会引入对评测集的过拟合，"
                         "默认关，见 app/llm/prompt.py 的 EXAMPLES_POSTHOC）")
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

    if args.rejudge:
        n = rejudge(names, items, use_llm=not args.no_llm)
        print(f"  重算 {n} 条判定（缺 captured 字段的记录跳过）")
    else:
        for n in names:
            run_one(n, items, use_llm=not args.no_llm,
                    with_posthoc=args.with_posthoc)

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
