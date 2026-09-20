"""问一个问题，看倒置LLM 整条链路怎么走。

这是手动验证模型生成效果的主要入口。每次运行都会落一条 trace 到
logs/query_trace.jsonl，可以事后追溯。

用法：

    # 问一句
    python scripts/ask.py "3号泵停机会影响哪些设备？"

    # 不带问题时进交互模式，连着问
    python scripts/ask.py

    # 只看模型生成和静态校验，不连数据库（Neo4j 没起来时用）
    python scripts/ask.py --no-db "哪些作业没有识别出风险？"

    # 把实际发给模型的 prompt 打出来，检查 prompt 本身
    python scripts/ask.py --show-prompt "3号泵的型号是什么？"

    # 消融：关掉 few-shot 看差多少
    python scripts/ask.py --no-examples "3号泵停机会影响哪些设备？"

    # 跑内置测试题（data/bench_questions.json）
    python scripts/ask.py --bench

    # 固定随机种子，同样的问题得到同样的结果
    python scripts/ask.py --seed 42 "3号泵的型号是什么？"
"""

from __future__ import annotations

import argparse
import json
import sys

# Windows 控制台默认 GBK，中文会变乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                     # noqa: BLE001
    pass

from app import config                                # noqa: E402
from app.graph import client                           # noqa: E402
from app.llm import prompt as P                        # noqa: E402
from app.llm.text2cypher import query                  # noqa: E402

BAR = "=" * 68


def show(t, *, show_prompt: bool = False) -> None:
    print(BAR)
    print(f"问题  {t.question}")
    print(f"模型  {t.model}    prompt 变体 {t.prompt_variant}")
    print("-" * 68)

    if t.raw_output and t.raw_output.strip() != t.cypher.strip():
        print("模型原始输出（提取前）：")
        for ln in t.raw_output.strip().split("\n")[:8]:
            print("    " + ln)
        print()

    print("提取出的 Cypher：")
    if t.cypher:
        for ln in t.cypher.split("\n"):
            print("    " + ln)
    else:
        print("    （空 —— 模型没吐出可识别的查询）")
    print()

    mark = "通过" if t.validation_ok else "不通过"
    print(f"静态校验   {mark}")
    for issue in t.validation_issues:
        print(f"    - {issue}")

    if t.explain_ok is not None:
        print(f"EXPLAIN    {'通过' if t.explain_ok else '不通过  ' + t.explain_error}")

    for r in t.repairs:
        print(f"自修复 第{r['round']}轮 —— {r['reason']}")
        print(f"    回喂给模型的错误：{str(r['feedback'])[:140]}")
        for ln in (r.get("cypher") or "（空）").split("\n"):
            print(f"    → {ln}")

    if t.executed:
        if t.exec_ok:
            print(f"执行       成功，{t.row_count} 行，{t.exec_ms} ms")
            for row in t.sample_rows:
                print(f"    {row}")
        else:
            print(f"执行       失败  {t.exec_error[:200]}")
    else:
        print("执行       未执行")

    print()
    print(f"结论   {t.outcome}")

    # 耗时拆开显示。只看总耗时会误判：实测有道题 3.81s，
    # 其实是 Ollama 闲置卸载模型后重新加载，不是生成慢。
    seg = []
    if t.load_duration_s > 0.05:
        seg.append(f"加载 {t.load_duration_s:.2f}s")
    seg.append(f"prefill {t.prefill_s:.2f}s")
    seg.append(f"生成 {t.eval_s:.2f}s")
    print(f"耗时   {' + '.join(seg)}  =  {t.gen_elapsed_s:.2f}s")
    print(f"       {t.eval_count} tok / {t.gen_tok_per_s:.1f} tok/s"
          f"   | 总计 {t.total_ms} ms   | trace {t.run_id}")
    print(BAR)


def load_bench_questions() -> list[str]:
    p = config.DATA_DIR / "bench_questions.json"
    return [q["question"] for q in json.loads(p.read_text(encoding="utf-8"))["questions"]]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="问一个问题，看倒置LLM 整条链路怎么走",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="*", help="要问的问题")
    ap.add_argument("--no-db", action="store_true", help="只生成和校验，不连数据库")
    ap.add_argument("--show-prompt", action="store_true", help="打印发给模型的完整 prompt")
    ap.add_argument("--no-examples", action="store_true", help="消融：关掉 few-shot")
    ap.add_argument("--no-direction", action="store_true", help="消融：关掉方向提示")
    ap.add_argument("--model", default=None, help=f"换模型（默认 {config.GEN_MODEL}）")
    ap.add_argument("--seed", type=int, default=None, help="固定随机种子")
    ap.add_argument("--repairs", type=int, default=None,
                    help=f"自修复上限（默认 {config.MAX_REPAIR_ROUNDS}）")
    ap.add_argument("--bench", action="store_true", help="跑内置测试题")
    args = ap.parse_args()

    if args.show_prompt:
        q = " ".join(args.question) or "3号泵停机会影响哪些设备？"
        p = P.build_prompt(q, with_examples=not args.no_examples,
                           with_direction_hints=not args.no_direction)
        print(f"（发给模型的完整 prompt，{len(p)} 字符）")
        print(BAR)
        print(p)
        print(BAR)
        return 0

    if not args.no_db:
        ok, msg = client.ping()
        if not ok:
            print(f"连不上 Neo4j：{msg}")
            print("要么把容器起起来（docker compose up -d），")
            print("要么加 --no-db 只看生成和校验。")
            return 1

    kw = dict(
        model=args.model,
        use_examples=not args.no_examples,
        use_direction_hints=not args.no_direction,
        max_repairs=args.repairs,
        seed=args.seed,
        dry_run=args.no_db,
        source="ask",
    )

    questions = load_bench_questions() if args.bench else args.question

    if questions:
        for q in questions:
            show(query(q, **kw))
        return 0

    # 交互模式
    print("交互模式。直接输入问题，空行或 Ctrl+C 退出。")
    print(f"模型 {config.GEN_MODEL}   库 {'不连' if args.no_db else config.NEO4J_URI}")
    try:
        while True:
            q = input("\n问> ").strip()
            if not q:
                break
            show(query(q, **kw))
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
