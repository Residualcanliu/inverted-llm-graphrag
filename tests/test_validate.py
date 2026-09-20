"""校验层的测试。

跑法：pytest tests/ -v
"""

from app.graph.validate import validate
from app.llm.prompt import EXAMPLES


# ---------- 正向：不该误伤 ----------

def test_few_shot_examples_all_pass():
    """prompt 里的 few-shot 示例必须全部通过校验。

    这条挂了说明两件事之一：示例写错了，或者校验器误伤。
    两种都要立刻修 —— 示例会直接教坏模型。
    """
    for q, cypher in EXAMPLES:
        r = validate(cypher)
        assert r.ok, f"示例「{q}」没通过校验：{r.summary()} " \
                     f"{[i.detail for i in r.errors]}"


def test_legit_queries_pass():
    cases = [
        "MATCH (e:Equipment)-[:HAS_OPERATION]->(o:Operation) RETURN o.name",
        "MATCH (e:Equipment)-[:HAS_OPERATION]->(o:Operation) RETURN e.name, o.name",
        "MATCH (e:Equipment)-[:EXPERIENCED]->(wo:WorkOrder) "
        "RETURN count(wo) AS n, avg(wo.downtime_h) AS d",
        "MATCH (o:Operation) WHERE NOT (o)-[:HAS_HAZARD]->(:Hazard) RETURN o.name",
        "MATCH (e:Equipment)-[:DEPENDS_ON*1..5]->(x:Equipment) RETURN x.name",
        "MATCH (e:Equipment)<-[:DEPENDS_ON]-(d:Equipment) "
        "RETURN e.name, count(d) AS c ORDER BY c DESC LIMIT 5",
        "MATCH (l:Location {name:'A区泵房'})-[:CONTAINS]->(e:Equipment) RETURN e.name",
    ]
    for c in cases:
        r = validate(c)
        assert r.ok, f"合法查询被误伤：{c}\n{[i.detail for i in r.errors]}"


def test_undirected_relation_skips_direction_check():
    """无向关系不判方向，应放行。"""
    r = validate("MATCH (e:Equipment)-[:HAS_OPERATION]-(o:Operation) RETURN o.name")
    assert r.ok


# ---------- 反向：该拦的必须拦下 ----------

def test_blocks_write_operations():
    for c in [
        "MATCH (e:Equipment {name:'3号泵'}) SET e.model = 'X' RETURN e",
        "MATCH (e:Equipment) DETACH DELETE e",
        "CREATE (e:Equipment {name:'新设备'}) RETURN e",
        "MATCH (e:Equipment) REMOVE e.model RETURN e",
        "MERGE (e:Equipment {name:'X'}) RETURN e",
    ]:
        r = validate(c)
        assert not r.ok, f"写操作没拦住：{c}"
        assert any(i.kind == "write_clause" for i in r.errors)


def test_blocks_unknown_schema_elements():
    cases = [
        ("MATCH (x:Machine) RETURN x.name", "unknown_label"),
        ("MATCH (a:Equipment)-[:FEEDS]->(b:Equipment) RETURN a", "unknown_rel"),
        ("MATCH (e:Equipment) RETURN e.temperature", "unknown_property"),
        ("MATCH (e:Equipment {pressure: 5}) RETURN e.name", "unknown_property"),
    ]
    for c, kind in cases:
        r = validate(c)
        assert not r.ok, f"没拦住：{c}"
        assert any(i.kind == kind for i in r.errors), \
            f"{c} 期望 {kind}，实际 {[i.kind for i in r.errors]}"


def test_blocks_reversed_direction():
    """跨标签的关系写反了必须拦下。

    注意 DEPENDS_ON 两端都是 Equipment，自己指向自己，
    任何方向都合法 —— 那种关系判不出方向，只能靠语义检查。
    """
    cases = [
        "MATCH (o:Operation)-[:HAS_OPERATION]->(e:Equipment) RETURN e.name",
        "MATCH (wo:WorkOrder)-[:EXPERIENCED]->(e:Equipment) RETURN e.name",
        "MATCH (f:FailureMode)-[:CAUSED_BY]->(wo:WorkOrder) RETURN wo.id",
        "MATCH (s:SafetyMeasure)-[:MITIGATED_BY]->(h:Hazard) RETURN h.name",
    ]
    for c in cases:
        r = validate(c)
        assert not r.ok, f"方向写反没拦住：{c}"
        assert any(i.kind == "wrong_direction" for i in r.errors), \
            f"{c} 报的是 {[i.kind for i in r.errors]}"


def test_correct_reverse_arrow_passes():
    """用 <- 反着写但方向语义正确，应放行。"""
    r = validate("MATCH (o:Operation)<-[:HAS_OPERATION]-(e:Equipment) RETURN o.name")
    assert r.ok, [i.detail for i in r.errors]


def test_blocks_multi_statement_and_injection():
    r = validate("MATCH (e:Equipment) RETURN e; MATCH (o:Operation) RETURN o")
    assert not r.ok and any(i.kind == "multi_statement" for i in r.errors)


def test_blocks_empty():
    r = validate("   ")
    assert not r.ok and any(i.kind == "empty" for i in r.errors)


def test_comment_is_warning_not_error():
    r = validate("MATCH (e:Equipment) // 取所有设备\nRETURN e.name")
    assert r.ok
    assert any(i.kind == "comment" for i in r.warnings)


def test_blocks_non_cypher_garbage():
    """垃圾输入不能被放行。

    实测踩过的坑：模型偶尔会输出字面量 `Cypher：`（啥内容都没有）。
    三道检查对它全部放行 —— 没有标签、没有关系、没有写操作，所以「没发现错误」。
    结果是静态校验通过、EXPLAIN 才报语法错，白白浪费一轮自修复。
    """
    for c in ["Cypher：", "好的，以下是为您生成的查询：", "SELECT * FROM users",
              "```", "I cannot help with that"]:
        r = validate(c)
        assert not r.ok, f"没拦住垃圾输入：{c!r}"
        assert any(i.kind == "not_cypher" for i in r.errors), \
            f"{c!r} 报的是 {[i.kind for i in r.errors]}"
