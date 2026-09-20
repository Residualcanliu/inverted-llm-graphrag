"""Cypher 抽取的测试。纯函数，不需要模型也不需要数据库。

跑法：pytest tests/ -v
"""

from app.llm.ollama_client import extract_cypher, strip_think


def test_bare_cypher_passes_through():
    c = "MATCH (e:Equipment) RETURN e.name"
    assert extract_cypher(c) == c


def test_strips_fenced_block():
    for fence in ["```cypher", "```sql", "```"]:
        text = f"{fence}\nMATCH (e:Equipment) RETURN e.name\n```"
        assert extract_cypher(text) == "MATCH (e:Equipment) RETURN e.name"


def test_strips_leading_explanation():
    """模型爱加前缀说明，要砍掉。"""
    text = "好的，以下是查询：\nMATCH (e:Equipment) RETURN e.name"
    assert extract_cypher(text) == "MATCH (e:Equipment) RETURN e.name"


def test_strips_trailing_semicolon():
    """Neo4j driver 不接受结尾分号。"""
    assert extract_cypher("MATCH (e:Equipment) RETURN e.name;") == \
        "MATCH (e:Equipment) RETURN e.name"


def test_returns_empty_for_non_cypher():
    """抠不出查询结构就返回空串，让上游按生成失败处理。

    实测踩过：模型输出字面量 `Cypher：`，返回它会让下游白跑一轮。
    """
    for bad in ["", "   ", "Cypher：", "I'm sorry, I can't help with that.",
                "```\n```"]:
        assert extract_cypher(bad) == "", f"{bad!r} 没被清空"


def test_strip_think():
    text = "<think>\n让我想想...\n</think>\nMATCH (n) RETURN n"
    body, had = strip_think(text)
    assert had and body == "MATCH (n) RETURN n"


def test_strip_think_without_block():
    body, had = strip_think("MATCH (n) RETURN n")
    assert not had and body == "MATCH (n) RETURN n"


def test_think_then_fence_combined():
    """两个动作串起来：先剥 think，再剥围栏。"""
    raw = "<think>推理中</think>\n```cypher\nMATCH (n) RETURN n\n```"
    body, _ = strip_think(raw)
    assert extract_cypher(body) == "MATCH (n) RETURN n"
