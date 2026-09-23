"""Neo4j 客户端。

三个职责：
  1. 实时读 schema —— prompt 里的 schema 不硬编码，改图不用改代码
  2. EXPLAIN 预检 —— 不执行就能验语法
  3. 只读执行 —— 所有查询走 READ_ACCESS 模式，服务端强制拒绝写入

**关于只读这道防线（踩过的坑，记下来）**

原本的设想是建一个只有读权限的数据库账号。走不通：Neo4j 社区版不支持
RBAC，`GRANT ROLE` 直接报 UnsupportedAdministrationCommand。实测建的
"只读账号"可以随意 CREATE / DELETE，形同虚设。

替代方案是 driver 的 READ_ACCESS 模式。这个在单实例上**服务端会强制**
（不是路由提示）。实测拒绝范围：

    CREATE / MERGE / SET / DELETE / DETACH DELETE / REMOVE  全部被拒
    apoc.create.node 等 APOC 写过程                          被拒
    报错码 Neo.ClientError.Statement.AccessMode

所以只读保证 = validate.py 的静态拦截 + 这里的 READ_ACCESS。
两道都在应用层，但第二道由数据库执行，绕过不了。
"""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import READ_ACCESS, GraphDatabase, NotificationMinimumSeverity
from neo4j.exceptions import ClientError, Neo4jError

from app import config


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict]
    counters: str = ""
    elapsed_ms: int = 0


def get_driver():
    """拿一个 driver。

    统一用管理员凭据 —— 社区版没有 RBAC，另建账号带不来任何权限约束。
    只读靠 session 的 READ_ACCESS 模式保证，见文件头的说明。

    关掉服务端通知：db.schema.nodeTypeProperties 有个弃用警告会刷屏，
    与我们的逻辑无关。
    """
    return GraphDatabase.driver(
        config.NEO4J_URI,
        auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
        notifications_min_severity=NotificationMinimumSeverity.OFF,
    )


def ping() -> tuple[bool, str]:
    """连通性检查。返回 (是否通, 说明)。"""
    try:
        with get_driver() as d:
            d.verify_connectivity()
        return True, f"连上 {config.NEO4J_URI}"
    except Exception as e:                            # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ---------------- schema 实时读取 ----------------

def fetch_schema() -> dict:
    """从库里读真实 schema。

    返回 {"nodes": {label: [props]}, "rels": [(type, from_label, to_label)]}
    """
    nodes: dict[str, list[str]] = {}
    rels: list[tuple[str, str, str]] = []

    with get_driver() as d, d.session() as s:
        for rec in s.run("CALL db.labels() YIELD label RETURN label"):
            nodes.setdefault(rec["label"], [])

        # 节点属性
        try:
            for rec in s.run(
                "CALL db.schema.nodeTypeProperties() "
                "YIELD nodeLabels, propertyName "
                "RETURN nodeLabels, propertyName"
            ):
                for lb in rec["nodeLabels"] or []:
                    if lb in nodes and rec["propertyName"] not in nodes[lb]:
                        nodes[lb].append(rec["propertyName"])
        except Neo4jError:
            pass    # 空库时这个过程会报错，忽略

        # 关系类型
        for rec in s.run(
            "CALL db.relationshipTypes() YIELD relationshipType "
            "RETURN relationshipType"
        ):
            rt = rec["relationshipType"]
            rels.append((rt, "?", "?"))

        # 关系两端的标签。用一次采样查询推断，比读过程输出更直观
        for rt, _, _ in list(rels):
            try:
                rec = s.run(
                    f"MATCH (a)-[:{rt}]->(b) "
                    "RETURN labels(a)[0] AS frm, labels(b)[0] AS to LIMIT 1"
                ).single()
                if rec:
                    rels[rels.index((rt, "?", "?"))] = (rt, rec["frm"], rec["to"])
            except Neo4jError:
                continue

    return {"nodes": nodes, "rels": rels}


def render_live_schema() -> str:
    """把实时读到的 schema 渲染成和 schema_def.render_for_prompt() 一致的格式。

    两边格式一致，才能互相替换而不影响 prompt 效果。
    """
    sc = fetch_schema()
    lines = ["## 节点（标签，属性）"]
    for lb, props in sorted(sc["nodes"].items()):
        cn = ""
        from app.graph import schema_def as S
        if lb in S.NODES:
            cn = f"（{S.NODES[lb][0]}）"
        lines.append(f"- {lb}{cn}，属性：{', '.join(props) or '（无）'}")

    lines.append("")
    lines.append("## 关系（方向是固定的，不能反）")
    for rt, frm, to in sc["rels"]:
        lines.append(f"- ({frm})-[:{rt}]->({to})")
    return "\n".join(lines)


# ---------------- EXPLAIN 预检 ----------------

def explain(cypher: str) -> tuple[bool, str]:
    """用 EXPLAIN 预检语法，不执行。返回 (是否通过, 错误详情)。

    注意它能抓什么、抓不到什么：
      能抓：语法错误、不存在的函数、类型明显不对的用法
      抓不到：不存在的属性名（Neo4j 属性是动态的，查不存在的属性返回空而非报错）、
              关系方向反了、聚合算错
    后三类要靠 validate.py 和 prompt 工程。

    **返回的错误详情要带上 .message，不能只给 .code。**

    踩过的坑：这里原本返回 `e.code`，也就是
    `Neo.ClientError.Statement.SyntaxError` 这么一个代号。自修复循环把它
    回喂给模型，模型看到「语法错」三个字完全不知道错在哪，于是原样重写一遍，
    连写两次，两轮修复全白费。

    而数据库其实说得很清楚：`Argument to EXISTS(...) is not a pattern
    (line 2, column 15)` —— 带行号、带列号、带具体原因。这才是模型需要的。
    """
    try:
        with get_driver() as d, d.session(default_access_mode=READ_ACCESS) as s:
            s.run(f"EXPLAIN {cypher}").consume()
        return True, ""
    except (ClientError, Neo4jError) as e:
        detail = getattr(e, "message", None) or str(e)
        code = getattr(e, "code", "") or type(e).__name__
        return False, f"{detail}  [{code}]"


# ---------------- 执行 ----------------

def run_readonly(cypher: str, params: dict | None = None,
                 limit: int = 5000) -> QueryResult:
    """以只读模式执行。写操作会被服务端直接拒绝。"""
    import time
    t0 = time.time()
    with get_driver() as d, d.session(default_access_mode=READ_ACCESS) as s:
        result = s.run(cypher, params or {})
        rows = result.data()
        columns = list(result.keys())
    return QueryResult(
        columns=columns,
        rows=rows[:limit],
        elapsed_ms=int((time.time() - t0) * 1000),
    )


# ---------------- 写入 ----------------
#
# 整个项目只有建图脚本会调这里。查询链路一律走 run_readonly，
# 那条路径上服务端强制只读，是纵深防御的最后一道。
#
# 这里刻意不用 READ_ACCESS，因为建图本来就要写。边界靠调用方约束：
# 除了 scripts/build_graph.py，别的地方都不该 import 这个函数。
# 测试 test_client.py 会验证 run_readonly 拒绝写入，确保两条路径没有混。

def run_write(cypher: str, params: dict | None = None) -> QueryResult:
    """写入。仅供建图脚本使用。"""
    import time
    t0 = time.time()
    with get_driver() as d, d.session() as s:
        result = s.run(cypher, params or {})
        summary = result.consume()
        counters = summary.counters
    return QueryResult(
        columns=[],
        rows=[],
        counters=(f"节点 +{counters.nodes_created} ~{counters.nodes_created or 0}  "
                  f"关系 +{counters.relationships_created}"),
        elapsed_ms=int((time.time() - t0) * 1000),
    )


def wipe() -> None:
    """清空数据库。仅供建图脚本的 --reset 使用。"""
    with get_driver() as d, d.session() as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
        try:
            s.run("CALL apoc.schema.assert({}, {}, true)").consume()
        except Exception:                             # noqa: BLE001
            pass
