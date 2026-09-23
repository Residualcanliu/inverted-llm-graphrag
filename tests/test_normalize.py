"""规范化的测试。纯函数，不依赖数据文件。

用例全部取自实际源数据，不是编的。

跑法：pytest tests/ -v
"""

import pytest

from app.ingest.normalize import (
    NormalizeError,
    check_count,
    clean_text,
    expand_ids,
    extract_count,
    parse_date,
    parse_generic_models,
    split_multi,
    split_name_spec,
)


# ---------------- 编号展开 ----------------

def test_expand_full_ids():
    assert expand_ids("tenlong-001") == ["tenlong-001"]


def test_expand_omitted_prefix():
    """省略前缀的写法。源数据里大量出现。"""
    assert expand_ids("tenlong-001、002、005、006") == [
        "tenlong-001", "tenlong-002", "tenlong-005", "tenlong-006"]


def test_expand_omitted_prefix_with_range():
    """区间也省略前缀。这是最容易出错的一类。"""
    assert expand_ids("chengxin-001～006、025～030") == [
        "chengxin-001", "chengxin-002", "chengxin-003", "chengxin-004",
        "chengxin-005", "chengxin-006",
        "chengxin-025", "chengxin-026", "chengxin-027", "chengxin-028",
        "chengxin-029", "chengxin-030"]


def test_expand_range_with_count_annotation():
    assert expand_ids("tenlong-001～004（4台）") == [
        "tenlong-001", "tenlong-002", "tenlong-003", "tenlong-004"]


def test_expand_mixed_forms():
    assert expand_ids("yuelong-001～004、015～020（10台）") == [
        "yuelong-001", "yuelong-002", "yuelong-003", "yuelong-004",
        "yuelong-015", "yuelong-016", "yuelong-017", "yuelong-018",
        "yuelong-019", "yuelong-020"]


def test_expand_handles_halfwidth_tilde():
    assert expand_ids("a-001~003") == ["a-001", "a-002", "a-003"]


def test_expand_empty_and_nan():
    assert expand_ids("") == []
    assert expand_ids(None) == []
    assert expand_ids("nan") == []


# ---------------- 通用后缀 ----------------

def test_parse_generic_suffix():
    assert parse_generic_models("tenlong/yuelong通用") == ["tenlong", "yuelong"]
    assert parse_generic_models("tenlong/yuelong/chengxin通用") == [
        "tenlong", "yuelong", "chengxin"]


def test_parse_all_models():
    assert parse_generic_models("全机型通用") == ["*"]
    assert parse_generic_models("tenlong/yuelong/chengxin/huanmai/heyue通用") == [
        "tenlong", "yuelong", "chengxin", "huanmai", "heyue"]


def test_parse_plain_model():
    assert parse_generic_models("tenlong") == ["tenlong"]


# ---------------- 名称与规格 ----------------

def test_split_name_spec_chinese_parens():
    assert split_name_spec("硬质合金车刀片（CNMG120408）") == ("硬质合金车刀片", "CNMG120408")


def test_split_name_spec_greek():
    assert split_name_spec("立铣刀（Φ10）") == ("立铣刀", "Φ10")


def test_split_name_spec_degree():
    assert split_name_spec("外圆车刀杆（90°）") == ("外圆车刀杆", "90°")


def test_split_name_spec_absent():
    assert split_name_spec("三爪卡盘") == ("三爪卡盘", "")


def test_split_name_spec_with_trailing_note():
    """括号里是说明而非规格，也要拆出来。"""
    assert split_name_spec("可循环使用（可刃磨）") == ("可循环使用", "可刃磨")


# ---------------- 日期 ----------------

def test_parse_date_forms():
    import datetime
    d = datetime.date(2026, 5, 8)
    assert parse_date("2026-05-08") == d
    assert parse_date("2026/05/08") == d
    assert parse_date(datetime.datetime(2026, 5, 8, 13, 30)) == d


def test_parse_date_invalid():
    assert parse_date("") is None
    assert parse_date(None) is None
    assert parse_date("nan") is None
    assert parse_date("不是日期") is None


# ---------------- 其它 ----------------

def test_clean_text():
    assert clean_text("  abc  ") == "abc"
    assert clean_text("nan") == ""
    assert clean_text(float("nan")) == ""
    assert clean_text(None) == ""


def test_split_multi_mixed_separators():
    assert split_multi("平衡轴、砂轮") == ["平衡轴", "砂轮"]
    assert split_multi("a，b/c") == ["a", "b/c"]   # 斜杠在编号里有意义，不当分隔符


def test_extract_count():
    assert extract_count("tenlong-001～004（4台）") == 4
    assert extract_count("abc") is None


def test_check_count_passes():
    assert check_count(4, ["a", "b", "c", "d"], "流程1") == "流程1 4 台"


def test_check_count_raises_on_mismatch():
    """台数对不上必须抛错，不能静默放过。

    这条自检存在的理由：第一版解析器漏了省略前缀的分支，
    流程2 的 4 台被展开成 1 台，边数少一大截而且不报错。
    """
    with pytest.raises(NormalizeError):
        check_count(4, ["a"], "流程2")
