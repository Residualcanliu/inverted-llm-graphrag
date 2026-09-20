"""Text2Cypher 的 prompt 构造。

设计依据（来自公开调研）：
- 错误分布里关系方向写反占 33%、计数/聚合算错占 30%，幻觉 schema 元素只占 10%。
  所以 prompt 的重点是「方向提示 + few-shot 示例」，而不是防幻觉。
- schema 的冗长度比格式影响更大，紧凑胜于详尽。
- few-shot 的收益大于多轮自修复。Kuzu 的实验里，加上方向提示和 schema 剪枝后
  gpt-4.1 在 30 条问题上 30/30 全对。
"""

from __future__ import annotations

from app.graph import schema_def as S

SYSTEM_RULES = """你是图数据库专家。把用户的问题翻译成一条 Cypher 查询。

只使用 MATCH / OPTIONAL MATCH / WHERE / WITH / RETURN / ORDER BY / LIMIT / DISTINCT / NOT / EXISTS 这些只读子句。"""

OUTPUT_RULES = """## 输出要求

- 只输出一条 Cypher 查询。不要解释，不要 Markdown 代码块标记，不要前后缀
- 关系方向严格按上面的定义写。写反是最常见的错误
- 用中文别名（AS）让结果可读
- 问题里提到的实体名用 {name: '...'} 或 {id: '...'} 匹配
- 目标是 Neo4j 5.x。部分 4.x 写法在 5.x 已被移除，不要用：
  - 判断属性存在用 `n.prop IS NOT NULL`，不要用 `EXISTS(n.prop)`
  - 判断路径存在用 `EXISTS { (a)-[:REL]->(b) }`
  - 取节点标识用 `elementId(n)`，不要用 `id(n)`
- 返回的东西要对应问题问的，不要因为问题里提到了某个实体就返回那个实体的属性。
  问「资质」返回 `Role.cert`，问「备件」返回 `SparePart.name`，问「几次」用 `count`，
  问「多久」用 `avg` 或 `sum`。参考上面的示例。
- 路径要按 schema 的关系定义走，不要把不相关的关系串成一条链"""

# few-shot 示例。按 B 类五种问题各给一个，覆盖这个项目要考的全部题型。
# 这些示例同时是校验层的回归测试样本（见 tests/）。
EXAMPLES: list[tuple[str, str]] = [
    # A 类：单跳事实
    ("3号泵的检修需要哪些备件？",
     "MATCH (e:Equipment {name:'3号泵'})-[:HAS_OPERATION]->(o:Operation)"
     "-[:USES_PART]->(p:SparePart)\n"
     "RETURN DISTINCT p.name AS 备件"),

    # B1 多跳依赖。注意方向：A-[:DEPENDS_ON]->B 表示 A 依赖 B，
    # 所以「3号泵停机影响谁」要反过来找依赖它的设备
    ("3号泵停机会影响哪些设备？",
     "MATCH (e:Equipment {name:'3号泵'})<-[:DEPENDS_ON*1..5]-(affected:Equipment)\n"
     "RETURN DISTINCT affected.name AS 受影响设备"),

    # 问题里提到的实体不是查询目标 —— 这个模式要专门教。
    #
    # 实测踩过的坑：问「更换3号泵的机械密封需要什么资质」，模型咬住「机械密封」
    # 这个词（它在 schema 里是个 SparePart），一路走到供应商，把问的「资质」丢了；
    # 加了抽象规则后又反过来，硬把 SparePart 塞进路径，拼出 schema 非法的链。
    #
    # 所以这里给一个结构完全相同但**实体不同**的示例：备件名只是修饰语，
    # 查询目标在关系链的另一头。示例比规则文字有效得多。
    ("更换5号风机的轴承需要什么资质？",
     "MATCH (e:Equipment {name:'5号风机'})-[:HAS_OPERATION]->(o:Operation)"
     "-[:REQUIRES_ROLE]->(r:Role)\n"
     "RETURN DISTINCT r.cert AS 资质"),

    # B2 聚合统计
    ("3号泵一共发生过多少次故障，平均停机多久？",
     "MATCH (e:Equipment {name:'3号泵'})-[:EXPERIENCED]->(wo:WorkOrder)\n"
     "RETURN count(wo) AS 故障次数, avg(wo.downtime_h) AS 平均停机时长"),

    # B3 否定 / 补集。注意这条题在文档里没有直接答案，
    # 只有图能通过集合补集回答
    ("哪些作业没有识别出风险？",
     "MATCH (o:Operation)\n"
     "WHERE NOT (o)-[:HAS_HAZARD]->(:Hazard)\n"
     "RETURN o.name AS 作业"),

    # B4 排序 / 关键性。用入度做简化版中心性，不需要 GDS 插件
    ("按被依赖程度给设备排序，找出最关键的设备",
     "MATCH (e:Equipment)<-[:DEPENDS_ON]-(dependent:Equipment)\n"
     "RETURN e.name AS 设备, count(dependent) AS 被依赖数\n"
     "ORDER BY 被依赖数 DESC"),

    # B5 根因追溯 / 路径
    ("追溯工单 WO-2024-0042 的故障链",
     "MATCH path = (wo:WorkOrder {id:'WO-2024-0042'})-[:CAUSED_BY]->(f:FailureMode)\n"
     "              -[:TRIGGERS*0..4]->(chain:FailureMode)\n"
     "RETURN [n IN nodes(path) | coalesce(n.name, n.id)] AS 故障链"),
]


def render_examples(limit: int | None = None) -> str:
    rows = EXAMPLES if limit is None else EXAMPLES[:limit]
    parts = []
    for q, a in rows:
        parts.append(f"问题：{q}\nCypher：{a}")
    return "\n\n".join(parts)


def build_prompt(question: str, *, with_examples: bool = True,
                 with_direction_hints: bool = True,
                 schema_text: str | None = None) -> str:
    """组装完整 prompt。

    三个开关是为了做消融实验：few-shot 和方向提示各值多少钱，
    单独关掉跑一遍就知道了。这也是 README 里能写的一笔。
    """
    blocks = [SYSTEM_RULES, ""]

    blocks.append("## 图谱 schema")
    blocks.append(schema_text if schema_text else S.render_for_prompt())
    blocks.append("")

    if with_direction_hints:
        blocks.append(S.render_direction_hints())
        blocks.append("")

    blocks.append(OUTPUT_RULES)

    if with_examples:
        blocks.append("")
        blocks.append("## 示例")
        blocks.append(render_examples())

    blocks.append("")
    blocks.append("## 问题")
    blocks.append(question)
    blocks.append("")
    blocks.append("Cypher：")

    return "\n".join(blocks)
