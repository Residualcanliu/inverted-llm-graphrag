"""Streamlit 界面的冒烟测试。

用 Streamlit 官方的 AppTest 真的执行页面代码，比截图可靠 ——
截图只能证明「页面出来了」，AppTest 能抓出渲染异常。

需要后端在跑（uvicorn app.api:app --port 8010），不在就整体跳过。

跑法：pytest tests/ -v
"""

import pytest
import requests

from streamlit.testing.v1 import AppTest

API = "http://127.0.0.1:8010"


def _api_alive() -> bool:
    try:
        return requests.get(f"{API}/health", timeout=5).ok
    except Exception:                                  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _api_alive(), reason="后端没起，跳过界面测试")


def _app():
    return AppTest.from_file("app/ui.py", default_timeout=600)


def test_initial_render():
    """首次加载不能抛异常。"""
    at = _app()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert [h.value for h in at.header] == ["查询"]


def test_query_renders_without_exception():
    """提问后不能抛异常。

    这条抓过真 bug：把 st.success() 写成三元表达式的分支，
    Streamlit 的 magic 会去渲染返回值，报
    `_repr_html_() is not a valid Streamlit command`。
    只有「提问之后」才触发，所以初始渲染的测试抓不到。
    """
    at = _app()
    at.run()
    at.text_input[0].set_value("tenlong-001 属于哪个车间？")
    at.button[0].click()
    at.run()

    assert not at.exception, [str(e.value) for e in at.exception]
    # 结果区该有的块
    subs = [h.value for h in at.subheader]
    assert "生成的 Cypher" in subs
    assert "校验" in subs
    assert "执行结果" in subs


def test_pipeline_page_renders():
    """数据管线页也要能渲染。"""
    at = _app()
    at.run()
    at.radio[0].set_value("数据管线")
    at.run()

    assert not at.exception, [str(e.value) for e in at.exception]
    assert [h.value for h in at.header] == ["数据管线"]
    # 四步的状态卡片
    assert len(at.metric) >= 4


def test_pending_queue_renders():
    """待确认队列那部分不能崩。"""
    at = _app()
    at.run()
    at.radio[0].set_value("数据管线")
    at.run()

    captions = " ".join(c.value for c in at.caption)
    assert "待确认" in captions or "队列是空的" in captions
