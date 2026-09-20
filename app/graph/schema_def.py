"""图谱 schema 定义（方案 C：作业知识 + 设备故障混合）。

这份定义有两个用途：
1. 渲染成 prompt 文本，注入给生成模型
2. 作为确定性校验的参照系

它是「开发期的参考副本」。生产路径应该从 Neo4j 实时读 schema
（见 app/graph/client.py 的 fetch_schema），这样改图不用改代码。
这份副本用于：Neo4j 还没起来时做离线校验、以及校验实时读到的 schema
是否走样。两者不一致时以库里的为准，并报警。
"""

# 节点：标签 -> (中文名, 属性列表)
NODES: dict[str, tuple[str, list[str]]] = {
    "Location": ("区域", ["name", "area_type"]),
    "Equipment": ("设备", ["name", "model", "criticality"]),
    "Operation": ("作业", ["name", "duration_min", "step_no"]),
    "Role": ("岗位", ["name", "cert"]),
    "SparePart": ("备件", ["name", "spec"]),
    "Supplier": ("供应商", ["name"]),
    "Hazard": ("风险", ["name", "level"]),
    "SafetyMeasure": ("安全措施", ["name"]),
    "WorkOrder": ("工单", ["id", "date", "downtime_h"]),
    "FailureMode": ("故障模式", ["name"]),
}

# 关系：(类型, 起点标签, 终点标签, 中文读法, 支撑哪类问题)
# 方向在这里是权威定义，校验层用它来抓「关系方向搞反」——
# 这是 Text2Cypher 里占比最高的一类错误（约 33%）
RELS: list[tuple[str, str, str, str, str]] = [
    ("CONTAINS", "Location", "Equipment", "区域包含设备", "基础"),
    ("DEPENDS_ON", "Equipment", "Equipment", "设备依赖设备", "B1 多跳 / B4 排序"),
    ("HAS_OPERATION", "Equipment", "Operation", "设备有作业", "B1 / B3"),
    ("REQUIRES_ROLE", "Operation", "Role", "作业需要岗位", "B1"),
    ("USES_PART", "Operation", "SparePart", "作业使用备件", "B1"),
    ("HAS_HAZARD", "Operation", "Hazard", "作业有风险", "B3"),
    ("MITIGATED_BY", "Hazard", "SafetyMeasure", "风险由措施缓解", "B1 / B3"),
    ("PRECEDES", "Operation", "Operation", "工序先后", "B1"),
    ("SUPPLIED_BY", "SparePart", "Supplier", "备件供应商", "基础"),
    ("EXPERIENCED", "Equipment", "WorkOrder", "设备经历过工单", "B2"),
    ("CAUSED_BY", "WorkOrder", "FailureMode", "工单由故障引起", "B5"),
    ("USED_PART", "WorkOrder", "SparePart", "工单用了备件", "B1"),
    ("PERFORMED_BY", "WorkOrder", "Role", "工单由岗位执行", "基础"),
    ("OCCURS_ON", "FailureMode", "Equipment", "故障发生于设备", "B5"),
    ("TRIGGERS", "FailureMode", "FailureMode", "故障连锁引发故障", "B5 / B1"),
]

LABELS = set(NODES)
REL_TYPES = {r[0] for r in RELS}
ALL_PROPS = {p for _, props in NODES.values() for p in props}


def rel_direction(rel_type: str) -> tuple[str, str] | None:
    """返回关系类型规定的 (起点标签, 终点标签)；未知类型返回 None。"""
    for t, frm, to, _, _ in RELS:
        if t == rel_type:
            return frm, to
    return None


def render_for_prompt() -> str:
    """渲染成紧凑的 schema 文本，注入 prompt。

    格式选择有讲究：调研显示 schema 的冗长度比格式影响更大，
    紧凑胜于详尽。这里用「标签，属性」和「A-[:REL]->B」两段式。
    """
    lines = ["## 节点（标签，属性）"]
    for label, (cn, props) in NODES.items():
        lines.append(f"- {label}（{cn}），属性：{', '.join(props)}")

    lines.append("")
    lines.append("## 关系（方向是固定的，不能反）")
    for t, frm, to, cn, _ in RELS:
        lines.append(f"- ({frm})-[:{t}]->({to})   # {cn}")

    return "\n".join(lines)


def render_direction_hints() -> str:
    """专门的方向提示。调研显示关系方向错误占全部错误的 33%，
    是占比最高的一类，所以在 prompt 里单独强调。"""
    lines = ["## 关系方向速查（写反了是最常见的错误）"]
    for t, frm, to, cn, _ in RELS:
        lines.append(f"- {frm} --{t}--> {to}    （{cn}）")
    return "\n".join(lines)
