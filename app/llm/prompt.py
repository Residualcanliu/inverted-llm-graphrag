"""Text2Cypher 的 prompt 构造。

设计依据（来自公开调研）：
- 错误分布里关系方向写反占 33%、计数/聚合算错占 30%，幻觉 schema 元素只占 10%。
  所以 prompt 的重点是「方向提示 + few-shot 示例」，而不是防幻觉。
- schema 的冗长度比格式影响更大，紧凑胜于详尽。
- few-shot 的收益大于多轮自修复。Kuzu 的实验里，加上方向提示和 schema 剪枝后
  gpt-4.1 在 30 条问题上 30/30 全对。
"""

from __future__ import annotations

from app.graph import schema as schema_mod

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
  问「几次」用 count，问「多久」用 avg，问「哪些设备」返回 `Equipment.id`，
  问「怎么处理」返回 `SafetyMeasure.name`。参考上面的示例。
- 路径要按 schema 的关系定义走，不要把不相关的关系串成一条链
- 设备用 `{id:'型号-编号'}` 匹配，例如 `{id:'chengxin-013'}`。设备没有 name 属性"""

# few-shot 示例。
#
# 全部用真实数据的设备编号（tenlong-001 这种），不用「3号泵」那种虚构名字 ——
# 示例里写什么名字，模型就会照抄什么写法。之前用虚构名字，演示时问「3号泵」
# 返回 0 行，看起来像系统坏了。
#
# 这些示例同时是校验层的回归测试样本（见 tests/），改了要重跑测试。
EXAMPLES: list[tuple[str, str]] = [
    # A 类：单跳事实
    ("huanmai-002 属于哪个车间？",
     "MATCH (e:Equipment {id:'huanmai-002'})-[:IN_SHOP]->(l:Location)\n"
     "RETURN l.name AS 车间"),

    # B1 多跳依赖。方向是考点：A-[:DEPENDS_ON]->B 表示 A 依赖 B，
    # 所以「停机影响谁」要反过来找依赖它的设备
    ("huanmai-002 停机会影响哪些设备？",
     "MATCH (e:Equipment {id:'huanmai-002'})<-[:DEPENDS_ON*1..5]-(affected:Equipment)\n"
     "RETURN DISTINCT affected.id AS 受影响设备"),

    # B1 另一种形态：经工序和流程做多跳
    ("chengxin-013 参与哪条工艺流程？",
     "MATCH (o:Operation)-[:USES]->(e:Equipment {id:'chengxin-013'})\n"
     "MATCH (p:Process)-[:HAS_STEP]->(o)\n"
     "RETURN DISTINCT p.name AS 工艺流程, o.name AS 工序"),

    # B2 聚合统计
    ("chengxin-006 一共检修过几次？",
     "MATCH (e:Equipment {id:'chengxin-006'})-[:EXPERIENCED]->(w:WorkOrder)\n"
     "RETURN count(w) AS 检修次数"),

    # B3 否定 / 补集。
    #
    # 措辞要跟数据语义对齐。最初写的是「哪些设备从未检修过」，
    # 生成 `NOT (e)-[:EXPERIENCED]->(:WorkOrder)` 返回 0 行 —— 不是系统错，
    # 是数据里每台设备都有工单，那 8 台只是没完成。
    # 换成正确的问法，返回的正好是数据里标「未检修」的那 8 台。
    ("哪些设备还有未完成的检修？",
     "MATCH (e:Equipment)-[:EXPERIENCED]->(w:WorkOrder)\n"
     "WHERE w.done = false\n"
     "RETURN DISTINCT e.id AS 设备"),

    # B4 排序 / 关键性。
    #
    # 方向是这一类的坑，而且**校验器抓不到** —— DEPENDS_ON 两端都是 Equipment，
    # 自反关系任何方向在 schema 上都合法。实测模型把「影响面最大」写成
    # `(e)-[:DEPENDS_ON*1..5]->(affected)`，问的是「它依赖谁」，
    # 答案跟「谁依赖它」完全不重叠。
    #
    # 结构上判不了的事，只能用示例教。
    ("哪些设备被最多其他设备依赖？",
     "MATCH (e:Equipment)<-[:DEPENDS_ON]-(dep:Equipment)\n"
     "RETURN e.id AS 设备, count(dep) AS 被依赖数\n"
     "ORDER BY 被依赖数 DESC LIMIT 5"),

    # B4 阈值。库存余量是算出来的，文本里没有这个数
    ("哪些备件库存最紧张？",
     "MATCH (p:SparePart)\n"
     "WHERE p.safety_stock IS NOT NULL\n"
     "RETURN p.name AS 备件, p.stock - p.safety_stock AS 余量\n"
     "ORDER BY 余量 ASC LIMIT 5"),

    # B5 根因追溯
    ("主轴轴承磨损会导致哪些故障？",
     "MATCH (cause:FailureMode {name:'主轴轴承磨损'})-[:TRIGGERS*1..3]->(effect:FailureMode)\n"
     "RETURN DISTINCT effect.name AS 可能引发的故障"),

    # 问题里提到的实体不是查询目标 —— 这个模式要专门教。
    #
    # 实测踩过的坑：模型咬住问题里的某个名词当成查询目标（备件、成因都可能），
    # 一路走下去把真正问的东西丢了。示例比抽象规则有效得多：
    # 规则只说「要什么」，示例还说「路径怎么走」。
    ("主轴轴承过热这个故障怎么处理？",
     "MATCH (f:FailureMode {name:'主轴轴承过热'})-[:MITIGATED_BY]->(s:SafetyMeasure)\n"
     "RETURN s.name AS 处理措施"),
]


# 评测集冻结之后才加的示例。**默认不参与正式评测。**
#
# 加它的依据来自评测的失败样本（系列题 9 道），所以它带着对评测集的过拟合，
# 测出来的不是留出估计。run_eval.py 默认关掉，加 --with-posthoc 才启用 ——
# 这样头条数字可以由一条命令复现，不依赖某个历史遗留的跑分记录。
#
# 它本身是个合理修复：schema 里 `model` 只写了属性名，没交代它就是设备系列。
EXAMPLES_POSTHOC: list[tuple[str, str]] = [
    # 系列限定。tenlong / yuelong / chengxin 这些「系列」存在 Equipment.model 里，
    # 它既是设备型号也是 id 前缀，两条路等价。
    #
    # 实测模型三种错法，全都返回 0 行：
    #   把系列当区域名   (l:Location {name:'tenlong'})   schema 里 Location 确实有 name
    #   通配符塞进等值   {id:'tenlong-*'}                Cypher 的通配要写 STARTS WITH
    #   直接编个设备号   {id:'chengxin-006'}             问题里根本没有这一台
    #
    # 校验层拦不住：Cypher 合法、schema 合法，只是匹配不到东西，
    # outcome 还是 answered。这类静默返回 0 行只能靠示例教。
    #
    # 措辞刻意跟评测题错开：评测问的是「X 系列里哪些设备还有未完成的检修」
    # （带第二重条件），这里只问「有哪些设备」。结构和答案都不一样，
    # 学到的才是「系列 -> model」这条映射，而不是背下那道题的答案。
    ("yuelong 系列有哪些设备？",
     "MATCH (e:Equipment {model:'yuelong'})\n"
     "RETURN e.id AS 设备"),
]


def render_examples(limit: int | None = None, *,
                    include_posthoc: bool = False) -> str:
    rows = EXAMPLES + EXAMPLES_POSTHOC if include_posthoc else EXAMPLES
    if limit is not None:
        rows = rows[:limit]
    parts = []
    for q, a in rows:
        parts.append(f"问题：{q}\nCypher：{a}")
    return "\n\n".join(parts)


def build_prompt(question: str, *, with_examples: bool = True,
                 with_direction_hints: bool = True,
                 with_posthoc_examples: bool = False,
                 schema_text: str | None = None) -> str:
    """组装完整 prompt。

    三个开关是为了做消融实验：few-shot 和方向提示各值多少钱，
    单独关掉跑一遍就知道了。这也是 README 里能写的一笔。
    """
    blocks = [SYSTEM_RULES, ""]

    sc = schema_mod.get()
    blocks.append("## 图谱 schema")
    blocks.append(schema_text if schema_text else sc.render_for_prompt())
    blocks.append("")

    if with_direction_hints:
        blocks.append(sc.render_direction_hints())
        blocks.append("")

    blocks.append(OUTPUT_RULES)

    if with_examples:
        blocks.append("")
        blocks.append("## 示例")
        blocks.append(render_examples(include_posthoc=with_posthoc_examples))

    blocks.append("")
    blocks.append("## 问题")
    blocks.append(question)
    blocks.append("")
    blocks.append("Cypher：")

    return "\n".join(blocks)
