"""把源数据渲染成自然语言文档。

这是对照实验的公平性关键。① 文档模式和 ③ 倒置LLM 喂的是**同一份源数据**，
区别只在组织方式：

    ① 把每条记录念成一句人话，摊平成文本，靠向量检索找相关片段
    ② 把同样的信息拆成节点和边，靠图查询精确取数

所以渲染必须把**信息完整保留**——不是「文档里没有所以传统 RAG 答不出」，
而是「信息都在，但扁平结构下算不出来」。这个区别决定了结论能不能站住。

渲染出来的文本相当于企业那边原始 PDF 的角色：人读得懂，但机器只能按相似度找。
"""

from __future__ import annotations

import json
from pathlib import Path


def render_equipment(eq: list[dict]) -> list[str]:
    out = []
    for e in eq:
        tools = "、".join(e.get("tool_groups", []))
        parts = [f"{e['id']} 是一台{e['function']}设备，位于{e['shop']}"]
        if tools:
            parts.append(f"使用{tools}")
        if e.get("uses_common_tools"):
            parts.append("也使用通用工具")
        if e.get("warehouse"):
            parts.append(f"备件存放在{e['warehouse']}")
        parts.append("该设备已完成检修" if e["inspected"] else "该设备尚未完成检修")
        if e.get("priority"):
            parts.append(f"标记为{e['priority']}")
        out.append("，".join(parts) + "。")
    return out


def render_spare_parts(sp: list[dict]) -> list[str]:
    out = []
    for p in sp:
        spec = f"，规格 {p['spec']}" if p.get("spec") else ""
        fits = p.get("fits_models") or []
        fit_txt = ("适用于全部机型" if "*" in fits
                   else "、".join(fits) if fits else "未标注适用机型")
        parts = [f"{p['id']} {p['name']}{spec}，属于{p['category']}"]
        if p.get("location"):
            parts.append(f"存放在{p['location']}")
        if p.get("stock") is not None:
            s = f"库存 {p['stock']}"
            if p.get("safety_stock"):
                s += f"，安全库存 {p['safety_stock']}"
            parts.append(s)
        parts.append(fit_txt)
        if p.get("hazard"):
            parts.append("属于高危物品")
        if p.get("consumable"):
            parts.append("一次性消耗件")
        out.append("，".join(parts) + "。")
    return out


def render_repairs(rp: list[dict]) -> list[str]:
    out = []
    for r in rp:
        parts = [f"{r['date']}，{r['device']} 发生{r['failure']}故障"]
        if r.get("parts"):
            parts.append(f"更换了{'、'.join(r['parts'])}")
        if r.get("tools"):
            parts.append(f"使用了{'、'.join(r['tools'])}")
        parts.append("检修已完成" if r["done"] else "检修尚未完成")
        out.append("，".join(parts) + "。")
    return out


def render_processes(fl: list[dict]) -> list[str]:
    out = []
    for f in fl:
        chain = " → ".join(s["operation"] for s in f["steps"])
        parts = [f"{f['id']} {f['name']}，适用于{f.get('part_type', '未标注零件类型')}",
                 f"工序链为 {chain}"]
        for s in f["steps"]:
            devs = s["devices"]
            shown = "、".join(devs[:6]) + (f" 等 {len(devs)} 台" if len(devs) > 6 else "")
            parts.append(f"第 {s['order']} 道{s['operation']}工序由 {shown} 承担")
        out.append("。".join(parts) + "。")
    return out


def render_dependencies(fl: list[dict]) -> list[str]:
    """渲染设备之间的流向。

    **这一段是对照实验的公平性关键。** 图那边有 650 条 DEPENDS_ON 边，
    如果文档这边不把同样的信息渲染出来，对比就变成「图有信息、文档没信息」，
    而不是「同样的信息、两种组织方式」——结论就不成立了。

    渲染方式选紧凑描述而不是逐条列 650 条边：逐条列会让语料暴涨到装不下上下文，
    而且丢失结构（变成一堆孤立的事实）。描述「哪一批设备流向哪一批」既完整又紧凑，
    模型仍然得自己做传递闭包才能回答「X 停机影响谁」——这正是要考的能力。
    """
    out = []
    for f in fl:
        steps = f["steps"]
        for a, b in zip(steps, steps[1:]):
            da = "、".join(a["devices"])
            db = "、".join(b["devices"])
            out.append(
                f"{f['id']} {f['name']} 的设备流向：第 {a['order']} 道{a['operation']}工序"
                f"（{da}）的产出，流向第 {b['order']} 道{b['operation']}工序（{db}）。"
                f"因此 {'、'.join(a['devices'][:3])} 等设备的产出中断，会波及 "
                f"{'、'.join(b['devices'][:3])} 等设备。"
            )
    return out


def render_manuals(man: list[dict]) -> list[str]:
    """手册本来就是文本，逐条故障念一遍。"""
    out = []
    for m in man:
        out.append(f"{m['title']}。")
        for f in m["failures"]:
            out.append(f"{m['model']} 设备出现{f['symptom']}时，"
                       f"判断依据是{f['cause']}，处理方式是{f['action']}。")
        if m.get("safety"):
            out.append(f"{m['model']} 设备的安全注意事项："
                       + "；".join(m["safety"]) + "。")
    return out


RENDERERS = {
    "equipment": render_equipment,
    "spare_parts": render_spare_parts,
    "repairs": render_repairs,
    "processes": render_processes,
    "manuals": render_manuals,
}


def build_documents(clean_dir: Path, chunk_size: int = 400) -> list[dict]:
    """把 clean/ 下的结构化数据渲染成文档块。

    按记录边界分块，不按固定字数切 —— 固定切会把一条记录劈成两半，
    检索到的片段就残缺了。攒够 chunk_size 字再切下一块。
    """
    docs: list[dict] = []
    for name, fn in RENDERERS.items():
        p = clean_dir / f"{name}.json"
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        for line in fn(data):
            if len(line) < 10:
                continue
            docs.append({"source": name, "text": line})
        # 工艺流程要再渲染一遍设备流向。图那边靠 650 条 DEPENDS_ON 边提供这个信息，
        # 文档这边靠这段话 —— 两边信息必须等价，对比才公平。
        if name == "processes":
            for line in render_dependencies(data):
                docs.append({"source": "dependencies", "text": line})

    # 合并成块
    chunks, buf, size = [], [], 0
    for d in docs:
        buf.append(d)
        size += len(d["text"])
        if size >= chunk_size:
            chunks.append({
                "id": f"chunk-{len(chunks):04d}",
                "source": buf[0]["source"],
                "text": "\n".join(x["text"] for x in buf),
            })
            buf, size = [], 0
    if buf:
        chunks.append({
            "id": f"chunk-{len(chunks):04d}",
            "source": buf[0]["source"],
            "text": "\n".join(x["text"] for x in buf),
        })
    return chunks
