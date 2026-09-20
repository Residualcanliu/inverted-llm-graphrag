"""图数据库客户端的测试。需要 Neo4j 在跑，不在就整体跳过。

跑法：pytest tests/ -v
"""

import pytest
from neo4j import READ_ACCESS

from app.graph import client

pytestmark = pytest.mark.skipif(
    not client.ping()[0], reason="Neo4j 没起来，跳过客户端测试"
)


# 每种写操作都必须被服务端拒绝。只测 CREATE 不够 ——
# 换一种写法能绕过的话，这道防线就是假的。
WRITE_PROBES = [
    ("CREATE", "CREATE (x:__T__) RETURN x"),
    ("MERGE", "MERGE (x:__T__ {id:'p'}) RETURN x"),
    ("SET", "MATCH (n) SET n.__t__ = 1 RETURN n"),
    ("DELETE", "MATCH (n:__T__) DELETE n"),
    ("DETACH DELETE", "MATCH (n) DETACH DELETE n"),
    ("REMOVE", "MATCH (n) REMOVE n.__t__ RETURN n"),
    ("APOC 写过程", "CALL apoc.create.node(['__T__'], {}) YIELD node RETURN node"),
]


@pytest.mark.parametrize("name,cypher", WRITE_PROBES)
def test_read_access_blocks_writes(name, cypher):
    """READ_ACCESS 模式下，任何写操作都必须被服务端拒绝。

    社区版没有 RBAC，这道是唯一的数据库级只读保证。
    """
    with client.get_driver() as d, d.session(default_access_mode=READ_ACCESS) as s:
        with pytest.raises(Exception) as ei:
            s.run(cypher).consume()
        assert "AccessMode" in str(getattr(ei.value, "code", "")), \
            f"{name} 报的不是权限错：{ei.value}"


def test_read_access_allows_reads():
    """只读查询不能被误伤。"""
    r = client.run_readonly("MATCH (n) RETURN count(n) AS c")
    assert "c" in r.columns


def test_run_readonly_rejects_write():
    """走应用的执行入口写，也必须被拒。"""
    with pytest.raises(Exception) as ei:
        client.run_readonly("CREATE (x:__T__) RETURN x")
    assert "AccessMode" in str(getattr(ei.value, "code", ""))


def test_explain_accepts_valid_and_rejects_garbage():
    ok, _ = client.explain("MATCH (n) RETURN n LIMIT 1")
    assert ok

    bad, msg = client.explain("MATCH (n RETURN n")     # 少了右括号
    assert not bad and msg


def test_explain_returns_message_not_just_code():
    """错误详情必须带具体原因，不能只给错误代号。

    踩过的坑：原本只返回 e.code（`Neo.ClientError.Statement.SyntaxError`），
    自修复把它回喂给模型，模型不知道错在哪，原样重写了两遍，两轮修复全白费。
    数据库其实给出了行号列号和原因。
    """
    bad, msg = client.explain("MATCH (f:FailureMode) RETURN EXISTS(f) AS x")
    assert not bad
    # 要有具体描述，不能只是一个代号
    assert "EXISTS" in msg or "pattern" in msg.lower(), f"详情太笼统：{msg}"
    assert len(msg) > 60, f"详情过短，可能只返回了错误码：{msg}"


def test_neo4j5_exists_forms_pass():
    """Neo4j 5 里 EXISTS 的两种正确写法都该通过。

    模型常写 4.x 的 EXISTS(n) 函数形式，在 5.x 会报语法错。
    """
    for q in ["MATCH (f:FailureMode) RETURN f IS NOT NULL AS x",
              "MATCH (f:FailureMode) RETURN EXISTS { (f)-[:TRIGGERS]->() } AS x"]:
        ok, msg = client.explain(q)
        assert ok, f"{q} 应该通过，实际报 {msg}"


def test_schema_fetch_shape():
    sc = client.fetch_schema()
    assert set(sc) == {"nodes", "rels"}
    assert isinstance(sc["nodes"], dict)
    assert isinstance(sc["rels"], list)
