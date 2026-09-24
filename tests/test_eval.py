"""判定与统计的测试。纯逻辑，不依赖数据库，也不调模型。

这两块是评测可信度的核心：判定错了，测出来的就是判定 bug 而不是系统差异；
统计错了，会把噪声当成结论。所以都要有测试锁住。

跑法：pytest tests/ -v
"""

import json

import pytest

from app.eval.judge import (REFUSE_MARKERS, Verdict, judge, values_from_rows,
                            _f1)
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


# ---------------- 回归：判定必须拿到完整结果集 ----------------
#
# 踩过的坑。trace 里的 sample_rows 是给日志看的**前 5 行样本**，
# 而倒置链路当时直接把它当答案交给了判定，于是
# 「43 台受影响设备」被截成 5 台 → 记成「命中 5/43」→ 判错。
#
# 后果不是小数目：B1 多跳 25 题全判 0，B3 补集 19 题大部分判 0，
# 共约 44 题被系统性压低。而且它伪装得很好 ——
# 报告上看起来就是「倒置链路多跳不行」，像个真结论。
#
# 日志要小、答案要全，这两个需求是冲突的，必须用两个字段分开。

def test_judge_scores_full_result_not_log_sample():
    """43 项全中要判对。截成 5 项就会退回 5/43。"""
    want = [f"dev-{i:03d}" for i in range(43)]
    it = _item(answer={"受影响设备": want})
    full = [{"受影响设备": v} for v in want]

    v_full = judge(it, _ans(rows=full), use_llm=False)
    v_sampled = judge(it, _ans(rows=full[:5]), use_llm=False)

    assert v_full.correct and v_full.score == 1.0
    assert not v_sampled.correct               # 截断确实会判错，这就是当初的症状
    # 5/43 的 F1 = 2*5/(5+43) ≈ 0.208，正是报告里那批「命中 5/N，多余 0」
    assert v_sampled.score == pytest.approx(10 / 48)
    assert "命中 5/43" in v_sampled.detail


def test_inverted_hands_judge_the_full_rows(monkeypatch):
    """倒置链路交给判定的 fields['rows'] 必须是完整结果集。"""
    from app.llm import text2cypher
    from app.llm.trace import QueryTrace
    from app.pipelines.inverted import InvertedPipeline

    full = [{"受影响设备": f"dev-{i:03d}"} for i in range(43)]

    def fake_query(question, **kw):
        t = QueryTrace(question=question)
        t.exec_ok = True
        t.cypher = "MATCH (e:Equipment)<-[:DEPENDS_ON*1..5]-(x) RETURN x.id AS 受影响设备"
        t.row_count = len(full)
        t.sample_rows = full[:5]      # 日志样本，故意只有 5 行
        t.result_rows = full          # 完整结果集
        return t.finish("answered")

    monkeypatch.setattr(text2cypher, "query", fake_query)
    ans = InvertedPipeline().answer("yuelong-001 停机会影响哪些设备？")

    assert len(ans.fields["rows"]) == 43
    assert ans.fields["row_count"] == 43
    assert len(ans.rows) == 43


def test_trace_log_does_not_carry_full_rows(monkeypatch, tmp_path):
    """完整结果集不落日志。落了的话一条 B1 题就能写几十行，trace 就废了。"""
    from app.llm import trace as trace_mod
    from app.llm.trace import QueryTrace

    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path)
    monkeypatch.setattr(trace_mod, "TRACE_FILE", tmp_path / "t.jsonl")

    t = QueryTrace(question="yuelong-001 停机会影响哪些设备？")
    t.sample_rows = [{"受影响设备": "dev-000"}]
    t.result_rows = [{"受影响设备": f"dev-{i:03d}"} for i in range(43)]
    trace_mod.append(t)

    rec = json.loads((tmp_path / "t.jsonl").read_text(encoding="utf-8"))
    assert "result_rows" not in rec
    assert rec["sample_rows"] == [{"受影响设备": "dev-000"}]


def test_trace_reload_has_no_full_rows(monkeypatch, tmp_path):
    """读回来的 trace 不该有 result_rows —— 落盘的字段和读回的字段要一致。"""
    from app.llm import trace as trace_mod
    from app.llm.trace import QueryTrace

    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path)
    monkeypatch.setattr(trace_mod, "TRACE_FILE", tmp_path / "t.jsonl")

    trace_mod.append(QueryTrace(question="?"))
    got = trace_mod.load_all()
    assert len(got) == 1 and "result_rows" not in got[0]


# ---------------- 回归：判定取值要和答案语义对齐 ----------------
#
# 判定的取值方式错了，测出来的就是判定 bug 而不是系统差异。
# 同一个坑踩了三次，成因都是「从查询结果里取值」这一步：
#   ① 取了日志用的 5 行样本，不是完整结果      → B1 全灭
#   ② 把所有列的值都当答案，排序键也混进来     → B4 全灭
#   ③ 只认 id，模型答 name 就判错              → B3 两道

def test_judge_ranking_ignores_sort_key_column():
    """排序题返回「答案列 + 排序列」，排序列不是答案。

    实测：问前 3 台，模型答对 3 台并附带被依赖数 22，
    值集合成了 4 项，卡在 len(got) == len(want) 上判错。
    """
    it = _item(atype="list", answer={"设备": ["a", "b", "c"],
                                     "_tied": ["a", "b", "c"]})
    rows = [{"设备": "a", "被依赖数": 22},
            {"设备": "b", "被依赖数": 22},
            {"设备": "c", "被依赖数": 22}]
    v = judge(it, _ans(rows=rows), use_llm=False)
    assert v.correct, v.detail


def test_judge_ranking_still_rejects_extra_entities():
    """丢掉的只能是排序键，多答的实体照样算错。"""
    it = _item(atype="list", answer={"设备": ["a", "b"], "_tied": ["a", "b"]})
    rows = [{"设备": "a", "被依赖数": 3},
            {"设备": "b", "被依赖数": 3},
            {"设备": "z", "被依赖数": 3}]
    v = judge(it, _ans(rows=rows), use_llm=False)
    assert not v.correct


def test_judge_set_accepts_name_when_truth_stores_id():
    """标准答案存 id、模型答 name —— 是同一批对象，该判对。

    实测 B3-088：模型给的 39 项与标准答案交集 39/39，只因写法不同判 0/39。
    """
    it = _item(answer={"备件": ["A-001", "A-002"]})
    rows = [{"备件": "刀架"}, {"备件": "寻边器"}]
    aliases = {"A-001": {"A-001"}, "刀架": {"A-001"},
               "A-002": {"A-002"}, "寻边器": {"A-002"}}

    assert not judge(it, _ans(rows=rows), use_llm=False).correct   # 不传表就判错
    v = judge(it, _ans(rows=rows), use_llm=False, aliases=aliases)
    assert v.correct, v.detail


def test_judge_ambiguous_name_matches_any_of_its_ids():
    """重名的备件，答名字要能跟它的任意一个 id 配上。

    实测 B4-104：「主轴轴承」有两台（B-013 和 B-015），模型答的那台确实
    在并列区内，但表里只留了一个 id，映射到区外的同名备件，判成错。
    """
    it = _item(answer={"备件": ["B-015", "A-018"]})
    rows = [{"备件": "主轴轴承"}, {"备件": "伺服驱动器"}]
    aliases = {"A-018": {"A-018"}, "伺服驱动器": {"A-018"},
               "B-013": {"B-013"}, "B-015": {"B-015"},
               "主轴轴承": {"B-013", "B-015"}}       # 一个名字两个 id
    v = judge(it, _ans(rows=rows), use_llm=False, aliases=aliases)
    assert v.correct, v.detail


def test_doc_rag_text_matches_name_for_id_answer():
    """散文里写的是 name，标准答案存 id —— 两种写法都要认。

    不然文档链路会因为「答了名字没答编号」被判错，凭空放大图链路的优势。
    """
    it = _item(answer={"备件": ["A-001"]})
    aliases = {"A-001": {"A-001"}, "刀架": {"A-001"}}
    ans = _ans(text="资料显示，刀架 没有设置安全库存。",
               pipeline="doc_rag")
    v = judge(it, ans, use_llm=False, aliases=aliases)
    assert v.correct, v.detail


def test_chain_empty_result_text_counts_as_refusal():
    """链路查空时生成的那句话，判定必须认得出是拒答。

    挂了就说明 _to_text 的措辞和 REFUSE_MARKERS 脱节了 —— 链路明明拒答，
    却会被判成幻觉。实测栽过：文案是「没有查到匹配的数据。」，
    词表里只有「查不到」，10 道拒答题全判 0。
    """
    from app.pipelines.inverted import _to_text

    empty = _to_text([], [])
    assert any(m in empty for m in REFUSE_MARKERS), \
        f"链路空结果文案「{empty}」不在 REFUSE_MARKERS 里"


def test_set_of_numbers_is_not_stripped_as_sort_key():
    """标准答案本身就是数值集合时不能丢 —— 丢了就成了空集比对。"""
    it = _item(answer={"次数": ["5", "3"]})
    rows = [{"次数": "5"}, {"次数": "3"}]
    assert judge(it, _ans(rows=rows), use_llm=False).correct


def test_aliases_build_only_uses_tables_with_both_fields(tmp_path):
    """只有同时有 id 和 name 的表才产生别名。"""
    from app.eval import aliases as A

    (tmp_path / "spare_parts.json").write_text(
        json.dumps([{"id": "A-001", "name": "刀架"}]), encoding="utf-8")
    # 只有 id 的表不产生别名（Equipment 就是这种）
    (tmp_path / "equipment.json").write_text(
        json.dumps([{"id": "tenlong-001", "model": "T-1"}]), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{ 不是 json", encoding="utf-8")

    t = A.build(tmp_path)
    assert t["刀架"] == {"A-001"} and t["A-001"] == {"A-001"}
    assert "tenlong-001" not in t


def test_aliases_build_keeps_every_id_for_a_duplicate_name(tmp_path):
    """重名要把所有 id 都收进集合，不能只留最后一个。"""
    from app.eval import aliases as A

    (tmp_path / "spare_parts.json").write_text(json.dumps([
        {"id": "B-013", "name": "主轴轴承"},
        {"id": "B-015", "name": "主轴轴承"},
    ]), encoding="utf-8")

    t = A.build(tmp_path)
    assert t["主轴轴承"] == {"B-013", "B-015"}
