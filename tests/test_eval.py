"""判定与统计的测试。纯逻辑，不依赖数据库，也不调模型。

这两块是评测可信度的核心：判定错了，测出来的就是判定 bug 而不是系统差异；
统计错了，会把噪声当成结论。所以都要有测试锁住。

跑法：pytest tests/ -v
"""

from app.eval.judge import Verdict, judge, values_from_rows, _f1
from app.eval.stats import (bootstrap_diff, compare, mcnemar,
                            min_detectable, wilson)
from app.pipelines.base import Answer


def _item(qid="T-1", layer="B1", atype="set", answer=None):
    return {"id": qid, "layer": layer, "question": "?",
            "answer_type": atype, "answer": answer or {}}


def _ans(rows=None, text="", pipeline="inverted", error=""):
    return Answer(pipeline=pipeline, question="?", text=text,
                  rows=rows or [], fields={"rows": rows} if rows else {},
                  error=error)


# ---------------- 取值 ----------------

def test_values_from_rows_flattens():
    rows = [{"受影响设备": "a"}, {"受影响设备": "b"}, {"受影响设备": "c"}]
    assert values_from_rows(rows) == {"a", "b", "c"}


def test_values_from_rows_ignores_column_names():
    """列名不同不该扣分。模型写 AS 设备 还是 AS 受影响设备 都合法。"""
    a = values_from_rows([{"设备": "x"}])
    b = values_from_rows([{"受影响设备": "x"}])
    assert a == b == {"x"}


def test_values_from_rows_handles_list_values():
    """collect() 之类的聚合返回列表，要拍平。"""
    assert values_from_rows([{"xs": ["a", "b"]}]) == {"a", "b"}


def test_values_from_rows_skips_none():
    assert values_from_rows([{"v": None}, {"v": "x"}]) == {"x"}


# ---------------- F1 ----------------

def test_f1_exact():
    assert _f1({"a", "b"}, {"a", "b"}) == 1.0


def test_f1_partial():
    assert abs(_f1({"a", "b"}, {"a", "b", "c", "d"}) - 0.6667) < 0.001


def test_f1_empty_want():
    assert _f1(set(), set()) == 1.0
    assert _f1({"a"}, set()) == 0.0


# ---------------- 判定：集合 ----------------

def test_judge_set_correct():
    it = _item(answer={"设备": ["a", "b"]})
    v = judge(it, _ans(rows=[{"设备": "a"}, {"设备": "b"}]), use_llm=False)
    assert v.correct and v.score == 1.0 and v.method == "set"


def test_judge_set_partial_scores():
    """集合类要给部分分 —— 命中一半比全错有信息量。"""
    it = _item(answer={"设备": ["a", "b", "c", "d"]})
    v = judge(it, _ans(rows=[{"设备": "a"}, {"设备": "b"}]), use_llm=False)
    assert not v.correct and 0.4 < v.score < 0.8


def test_judge_set_extra_hurts():
    """多答也算错。多出来的项是错的。"""
    it = _item(answer={"设备": ["a"]})
    v = judge(it, _ans(rows=[{"设备": "a"}, {"设备": "z"}]), use_llm=False)
    assert not v.correct


# ---------------- 判定：数值 ----------------

def test_judge_number():
    it = _item(atype="number", answer={"检修次数": 5})
    v = judge(it, _ans(rows=[{"检修次数": 5}]), use_llm=False)
    assert v.correct and v.method == "number"


def test_judge_number_wrong():
    it = _item(atype="number", answer={"检修次数": 5})
    v = judge(it, _ans(rows=[{"检修次数": 4}]), use_llm=False)
    assert not v.correct


# ---------------- 判定：排序（并列区） ----------------

def test_judge_ranking_inside_tie_zone():
    """模型给的项落在并列区内就算对 —— 实测有 33 项并列同一分值。"""
    it = _item(atype="list",
               answer={"设备": ["a", "b", "c"],
                       "_tied": ["a", "b", "c", "d", "e", "f"]})
    v = judge(it, _ans(rows=[{"设备": "a"}, {"设备": "d"}, {"设备": "f"}]),
              use_llm=False)
    assert v.correct, "并列区内的任意选择都该算对"


def test_judge_ranking_outside_tie_zone_fails():
    it = _item(atype="list",
               answer={"设备": ["a", "b", "c"], "_tied": ["a", "b", "c"]})
    v = judge(it, _ans(rows=[{"设备": "a"}, {"设备": "b"}, {"设备": "z"}]),
              use_llm=False)
    assert not v.correct


def test_judge_ranking_wrong_count_fails():
    """问前 3 个只给 2 个，算错。"""
    it = _item(atype="list",
               answer={"设备": ["a", "b", "c"], "_tied": ["a", "b", "c"]})
    v = judge(it, _ans(rows=[{"设备": "a"}, {"设备": "b"}]), use_llm=False)
    assert not v.correct


# ---------------- 判定：拒答 ----------------

def test_judge_refuse_says_dont_know():
    it = _item(layer="REFUSE", atype="refuse", answer={"期望": "查不到"})
    assert judge(it, _ans(text="资料中没有相关信息"), use_llm=False).correct


def test_judge_refuse_hallucinating_fails():
    """问到知识库里没有的东西却编了个答案 —— 这是幻觉，判错。"""
    it = _item(layer="REFUSE", atype="refuse", answer={"期望": "查不到"})
    v = judge(it, _ans(text="3号泵的检修周期是 30 天"), use_llm=False)
    assert not v.correct


# ---------------- 判定：文档链路 ----------------

def test_judge_doc_rag_uses_text_not_chunks():
    """文档链路的 rows 是检索到的 chunk 元信息，不是答案。

    踩过的坑：判定用「有没有 rows」区分链路，结果文档链路的 chunk id
    被当成答案值去比，四条链路里它全军覆没，看起来像「传统 RAG 完全不行」。
    那是判定 bug，不是实验结果。
    """
    it = _item(answer={"车间": "一车间"})
    ans = Answer(pipeline="doc_rag", question="?", text="这台设备位于一车间。",
                 rows=[{"chunk": "chunk-0001", "source": "equipment",
                        "score": 0.9}],
                 fields={})                     # 文档链路的 fields 是空的
    v = judge(it, ans, use_llm=False)
    assert v.correct and v.method == "text"


def test_judge_doc_rag_missing_fails():
    it = _item(answer={"车间": "一车间"})
    v = judge(it, Answer(pipeline="doc_rag", question="?", text="查不到",
                         fields={}), use_llm=False)
    assert not v.correct


# ---------------- 判定：错误传播 ----------------

def test_judge_error_answer():
    it = _item(answer={"设备": ["a"]})
    v = judge(it, _ans(error="ServiceUnavailable"), use_llm=False)
    assert not v.correct and v.method == "error"


# ---------------- 统计：McNemar ----------------

def test_mcnemar_all_agree():
    """两边完全一致 —— 没有可检验的差异。"""
    a = [True, True, False, False]
    assert mcnemar(a, a) == (0, 0, 0.0, False)


def test_mcnemar_clear_difference():
    """b 全面胜出。"""
    a = [False] * 20
    b = [True] * 20
    a_only, b_only, chi2, sig = mcnemar(a, b)
    assert a_only == 0 and b_only == 20 and sig


def test_mcnemar_small_sample_not_significant():
    """不一致对太少时检验没有功效 —— 这是最容易误读的地方。"""
    a = [True] * 100 + [False] * 3
    b = [True] * 100 + [True] * 3
    a_only, b_only, chi2, sig = mcnemar(a, b)
    assert b_only == 3 and not sig, "3 个不一致对检不出差异"


# ---------------- 统计：bootstrap ----------------

def test_bootstrap_ci_contains_zero_when_same():
    a = [1.0, 0.0] * 50
    diff, lo, hi = bootstrap_diff(a, a)
    assert diff == 0.0 and lo <= 0 <= hi


def test_bootstrap_ci_excludes_zero_when_different():
    a = [0.0] * 100
    b = [1.0] * 100
    diff, lo, hi = bootstrap_diff(a, b)
    assert diff == 1.0 and lo > 0


def test_bootstrap_is_deterministic():
    """固定种子，同样输入给同样的结果 —— 不然报告没法复现。"""
    a = [1.0, 0.0, 1.0, 0.0] * 25
    b = [1.0, 1.0, 0.0, 1.0] * 25
    assert bootstrap_diff(a, b) == bootstrap_diff(a, b)


# ---------------- 统计：compare ----------------

def test_compare_reports_direction():
    base = [0.0] * 30
    other = [1.0] * 30
    c = compare("base", "other", base, other)
    assert c.diff == 1.0 and c.acc_b == 1.0 and c.significant


def test_compare_warns_on_few_discordant():
    base = [1.0] * 50 + [0.0] * 3
    other = [1.0] * 50 + [1.0] * 3
    c = compare("base", "other", base, other)
    assert "功效" in c.note


# ---------------- 统计：辅助 ----------------

def test_min_detectable_shrinks_with_n():
    assert min_detectable(50) > min_detectable(150) > min_detectable(400)


def test_min_detectable_magnitude():
    """100 题大约能检出 15 个百分点 —— 写报告时要引用这个量级。"""
    assert 0.10 < min_detectable(100) < 0.20


def test_wilson_bounds():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0 < hi < 0.4
    lo, hi = wilson(10, 10)
    assert 0.6 < lo < 1.0 and hi == 1.0
