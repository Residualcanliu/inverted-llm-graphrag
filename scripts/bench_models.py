"""模型选型 bench。

用同一批固定题目跑几个候选模型，记录速度和质量，横向比，选出生成模型。
存在的意义：面试被问「为什么选这个模型」时能甩数据，而不是说「网上说这个好」。

不需要 Neo4j 就能跑。质量判定用两个确定性手段：
  1. app/graph/validate.py 的三道校验（标签/关系/属性是否存在、方向对不对）
  2. 题库里 must_have 关键词命中

连上 Neo4j 后会多两项：EXPLAIN 通过率、真实执行结果。

用法：
    python scripts/bench_models.py
    python scripts/bench_models.py --models qwen2.5-coder:7b qwen2.5-coder:14b
    python scripts/bench_models.py --no-examples     # 消融：关掉 few-shot
    python scripts/bench_models.py --repeat 2        # 每题跑两遍，看稳定性
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文会变乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                     # noqa: BLE001
    pass

from app import config                      # noqa: E402
from app.graph.validate import validate     # noqa: E402
from app.llm import ollama_client as oc     # noqa: E402
from app.llm import prompt as P             # noqa: E402

DEFAULT_MODELS = ["qwen2.5-coder:7b", "qwen2.5-coder:14b"]


def load_questions() -> list[dict]:
    path = config.DATA_DIR / "bench_questions.json"
    return json.loads(path.read_text(encoding="utf-8"))["questions"]


def run_one(model: str, q: dict, *, with_examples: bool, repeat: int) -> dict:
    """跑一个模型一道题。repeat 次取最好的一次（模型偶发输出异常不该判死）。"""
    prompt = P.build_prompt(q["question"], with_examples=with_examples)
    attempts = []
    for _ in range(repeat):
        try:
            g = oc.generate(prompt, model=model)
        except Exception as e:                       # noqa: BLE001
            attempts.append({"error": f"{type(e).__name__}: {e}"})
            continue

        v = validate(g.text)
        low = g.text.lower()
        hit = [k for k in q["must_have"] if k.lower() in low]
        attempts.append({
            "cypher": g.text,
            "raw_head": g.raw[:400],
            "elapsed_s": round(g.elapsed_s, 2),
            "tok_per_s": round(g.tok_per_s, 1),
            "eval_count": g.eval_count,
            "truncated": g.truncated,
            "valid": v.ok,
            "issues": [f"[{i.level}/{i.kind}] {i.detail}" for i in v.issues],
            "kw_hit": len(hit),
            "kw_total": len(q["must_have"]),
            "kw_missing": [k for k in q["must_have"] if k not in hit],
            "pass": v.ok and len(hit) == len(q["must_have"]),
        })

    ok = [a for a in attempts if "error" not in a]
    best = max(ok, key=lambda a: (a["pass"], a["kw_hit"], a["valid"])) if ok else \
        (attempts[0] if attempts else {})
    best = dict(best)
    best["attempts"] = len(attempts)
    return best


def bench_model(model: str, questions: list[dict], *, with_examples: bool,
                repeat: int, quiet: bool) -> dict:
    rows = []
    for q in questions:
        r = run_one(model, q, with_examples=with_examples, repeat=repeat)
        r["id"] = q["id"]
        r["cls"] = q["cls"]
        rows.append(r)
        if not quiet:
            mark = "OK  " if r.get("pass") else ("~   " if r.get("valid") else "FAIL")
            print(f"    {mark} {q['id']:<6} {r.get('elapsed_s', 0):>6.1f}s  "
                  f"{r.get('tok_per_s', 0):>6.1f} tok/s  "
                  f"{'截断 ' if r.get('truncated') else ''}"
                  f"{'' if r.get('pass') else '缺:' + ','.join(r.get('kw_missing', []))}")

    ok = [r for r in rows if "error" not in r]
    n = len(rows) or 1
    passed = sum(1 for r in rows if r.get("pass"))
    valid = sum(1 for r in rows if r.get("valid"))
    trunc = sum(1 for r in rows if r.get("truncated"))
    speeds = [r["tok_per_s"] for r in ok if r.get("tok_per_s")]
    times = [r["elapsed_s"] for r in ok if r.get("elapsed_s")]

    # 按题型分层，看模型在 B 类上是不是明显更差
    by_cls: dict[str, list[bool]] = {}
    for r in rows:
        by_cls.setdefault(r["cls"], []).append(bool(r.get("pass")))

    return {
        "model": model,
        "n": len(rows),
        "passed": passed,
        "valid": valid,
        "truncated": trunc,
        "errors": len(rows) - len(ok),
        "pass_rate": passed / n,
        "valid_rate": valid / n,
        "avg_tok_per_s": sum(speeds) / len(speeds) if speeds else 0.0,
        "avg_elapsed_s": sum(times) / len(times) if times else 0.0,
        "by_class": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cls.items())},
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument("--no-examples", action="store_true",
                    help="消融：关掉 few-shot，看示例值多少钱")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每题重复次数，取最好一次（默认 1）")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--out", default="eval_runs/bench_result.json")
    args = ap.parse_args()

    with_examples = not args.no_examples

    # 先确认 Ollama 活着，以及模型在不在地
    try:
        available = oc.list_models()
    except Exception as e:                            # noqa: BLE001
        print(f"连不上 Ollama（{config.OLLAMA_BASE_URL}）：{e}")
        return 1

    print(f"Ollama 在线，本地模型 {len(available)} 个")
    missing = [m for m in args.models if m not in available]
    if missing:
        print(f"\n以下模型本地没有，先 ollama pull：")
        for m in missing:
            print(f"    ollama pull {m}")
        args.models = [m for m in args.models if m not in available]
        if not args.models:
            return 1

    questions = load_questions()
    mode = "带 few-shot" if with_examples else "不带 few-shot（消融）"
    print(f"题目 {len(questions)} 条 | 候选模型 {len(args.models)} 个 | {mode}"
          f" | 每题 {args.repeat} 次\n")

    results = []
    for m in args.models:
        print(f"== {m} ==")
        r = bench_model(m, questions, with_examples=with_examples,
                        repeat=args.repeat, quiet=args.quiet)
        results.append(r)
        print(f"   -> 合格 {r['passed']}/{r['n']}  校验通过 {r['valid']}/{r['n']}  "
              f"{r['avg_tok_per_s']:.1f} tok/s  {r['avg_elapsed_s']:.1f}s/条"
              f"{'  截断 ' + str(r['truncated']) + ' 条' if r['truncated'] else ''}\n")

    # ---- 汇总表 ----
    print("=" * 78)
    print(f"{'模型':<26}{'合格率':>9}{'校验通过':>10}{'速度':>11}{'耗时':>9}{'截断':>6}")
    print("-" * 78)
    for r in results:
        print(f"{r['model']:<26}{r['passed']}/{r['n']:<7}"
              f"{r['valid']}/{r['n']:<8}"
              f"{r['avg_tok_per_s']:>8.1f} t/s{r['avg_elapsed_s']:>8.1f}s{r['truncated']:>6}")
    print("=" * 78)

    if len(results) > 1:
        print("\n分层合格率（看模型是不是只在 A 类上好看）：")
        classes = sorted({c for r in results for c in r["by_class"]})
        print(f"{'模型':<26}" + "".join(f"{c:>9}" for c in classes))
        for r in results:
            print(f"{r['model']:<26}" + "".join(
                f"{r['by_class'].get(c, '-'):>9}" for c in classes))

    out = Path(args.out)
    if not out.is_absolute():
        out = config.ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "with_examples": with_examples,
        "repeat": args.repeat,
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
