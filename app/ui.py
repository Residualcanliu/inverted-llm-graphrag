"""Streamlit 前端。

先起后端，再起前端：

    uvicorn app.api:app --port 8010
    streamlit run app/ui.py

两个页面：查询（倒置LLM 链路）和数据管线（含待确认队列的人工确认）。
"""

from __future__ import annotations

import pandas as pd
import requests
import streamlit as st

API = "http://127.0.0.1:8010"

st.set_page_config(page_title="inverted-llm-graphrag", layout="wide")


# ---------------- 工具 ----------------

def api(path: str, method: str = "GET", **kw):
    try:
        r = requests.request(method, f"{API}{path}", timeout=600, **kw)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        st.error(f"连不上后端（{API}）。先跑：`uvicorn app.api:app --port 8010`")
        st.stop()
    except Exception as e:                            # noqa: BLE001
        st.error(f"接口出错：{e}")
        return None


def cypher_block(text: str) -> None:
    st.code(text or "（空）", language="cypher")


# ---------------- 页面：查询 ----------------

def page_query():
    st.header("查询")

    with st.sidebar:
        st.subheader("参数")
        use_examples = st.checkbox("few-shot 示例", value=True)
        use_dir = st.checkbox("方向提示", value=True)
        seed = st.number_input("随机种子（固定则结果可复现）", value=-1, step=1)
        seed_val = None if seed < 0 else int(seed)

    q = st.text_input("问题", placeholder="例如：huanmai-002 停机会影响哪些设备？")

    if st.button("提问", type="primary") and q.strip():
        with st.spinner("生成 Cypher 并执行 ..."):
            r = api("/query", "POST", json={
                "question": q, "use_examples": use_examples,
                "use_direction_hints": use_dir, "seed": seed_val})
        if r:
            st.session_state["last"] = r

    r = st.session_state.get("last")
    if not r:
        st.info("输入一个问题。示例：tenlong-001 属于哪个车间？／"
                "哪些备件库存最紧张？／主轴轴承磨损会导致哪些故障？")
        return

    # 别把 st.* 写成三元表达式。Streamlit 的 magic 会渲染表达式的返回值，
    # 而 st.success() 返回的对象带 _repr_html_()，会被当成要渲染的内容，
    # 报 StreamlitAPIException: _repr_html_() is not a valid Streamlit command。
    if r["outcome"] == "answered":
        st.success(f"结论：{r['outcome']}")
    else:
        st.warning(f"结论：{r['outcome']}")

    col1, col2 = st.columns([3, 2])
    with col1:
        st.subheader("生成的 Cypher")
        cypher_block(r["cypher"])

    with col2:
        st.subheader("校验")
        st.write("静态校验：", "通过" if r["validation_ok"] else "不通过")
        for i in r.get("validation_issues") or []:
            st.caption(f"- {i}")
        if r.get("explain_ok") is not None:
            st.write("EXPLAIN：", "通过" if r["explain_ok"] else "不通过")
            if not r["explain_ok"]:
                st.caption(r.get("explain_error", ""))

    if r.get("repairs"):
        st.subheader(f"自修复（{len(r['repairs'])} 轮）")
        for x in r["repairs"]:
            with st.expander(f"第 {x['round']} 轮：{x['reason']}"):
                st.caption("回喂给模型的错误")
                st.code(str(x.get("feedback", ""))[:400])
                st.caption("重写后的 Cypher")
                cypher_block(x.get("cypher", ""))

    if r.get("executed"):
        st.subheader("执行结果")
        if r["exec_ok"]:
            rows = r.get("sample_rows") or []
            st.caption(f"{r['row_count']} 行，{r['exec_ms']} ms"
                       + ("（仅展示前 5 行）" if r["row_count"] > 5 else ""))
            if rows:
                st.dataframe(pd.DataFrame(rows), use_container_width=True)
            else:
                st.info("查询执行成功，但没有匹配的数据。")
        else:
            st.error(r.get("exec_error", ""))

    with st.expander("耗时明细"):
        seg = []
        if r.get("load_duration_s", 0) > 0.05:
            seg.append(f"加载 {r['load_duration_s']:.2f}s")
        seg.append(f"prefill {r.get('prefill_s', 0):.2f}s")
        seg.append(f"生成 {r.get('eval_s', 0):.2f}s")
        st.write(" + ".join(seg) + f" = {r.get('gen_elapsed_s', 0):.2f}s")
        st.caption(f"{r.get('eval_count', 0)} tok / {r.get('gen_tok_per_s', 0):.1f} tok/s"
                   f"　总计 {r.get('total_ms', 0)} ms　trace {r.get('run_id')}")


# ---------------- 页面：数据管线 ----------------

def page_pipeline():
    st.header("数据管线")

    status = api("/pipeline/status")
    if not status:
        return

    cols = st.columns(4)
    for c, s in zip(cols, status["steps"]):
        with c:
            st.metric(s["name"], "已完成" if s["done"] else "未跑")
            for k, v in (s["detail"] or {}).items():
                st.caption(f"{k} {v}")

    st.divider()
    tab1, tab2, tab3 = st.tabs(["待确认队列", "图谱统计", "建图基准"])

    with tab1:
        _pending_tab()
    with tab2:
        _stats_tab()
    with tab3:
        st.caption("第④步对账用这些基准值核对。任何一项对不上，说明管线有问题。")
        st.json(status.get("expected") or {})


def _pending_tab():
    d = api("/pipeline/pending") or {"pending": []}
    items = d["pending"]

    if not items:
        st.success("待确认队列是空的。")
        return

    st.caption(f"{len(items)} 组候选需要人工判断。"
               "AI 只能给出候选，该不该合并需要懂设备的人看一眼。")

    # 一键批量。逐条点 13 次太累，而且大部分组的结构信号是一致的。
    bc = st.columns([2, 2, 2, 5])
    for col, (label, act, tip) in zip(bc, [
            ("一键全部合并", "merge", "把全部候选按同义合并"),
            ("一键全部保留", "keep", "全部保持独立，不合并"),
            ("一键全部跳过", "skip", "先不处理，留待以后")]):
        if col.button(label, key=f"all-{act}", help=tip, use_container_width=True):
            r = api("/pipeline/resolve_all", "POST", json={"action": act})
            if r:
                st.success(f"合并 {r['merged']} 组，其余 {r['kept']} 组，"
                           f"还剩 {r['remaining']} 组")
                if r.get("need_rebuild"):
                    st.session_state["need_rebuild"] = True
                st.rerun()
    st.divider()

    for i, c in enumerate(items):
        imp = c.get("impact") or {}
        a, b = imp.get("a", {}), imp.get("b", {})
        shared, only_a, only_b = imp.get("shared", 0), imp.get("only_a", 0), imp.get("only_b", 0)

        with st.container(border=True):
            st.markdown(f"**{c['a']}　↔　{c['b']}**　`{c['kind']}`　"
                        f"置信度 {c['confidence']}")

            m = st.columns(4)
            m[0].metric("共同邻居", shared)
            m[1].metric(f"只属于 {c['a']}", only_a)
            m[2].metric(f"只属于 {c['b']}", only_b)
            m[3].metric("类型", f"{a.get('label','?')} / {b.get('label','?')}")

            if shared and only_a == 0 and only_b == 0:
                st.caption("邻居完全重合，很可能同一件东西记了两遍。")
            elif shared == 0:
                st.caption("没有共同邻居，多半是两回事。")
            else:
                st.caption(f"部分重合。共同邻居示例：{imp.get('sample_shared')}")

            st.caption(c.get("reason", ""))

            b1, b2, b3, _ = st.columns([1, 1, 1, 4])
            for col, (label, act, type_) in zip(
                    (b1, b2, b3),
                    [("合并", "merge", "primary"), ("保留独立", "keep", "secondary"),
                     ("跳过", "skip", "secondary")]):
                if col.button(label, key=f"{act}-{i}", type=type_):
                    res = api("/pipeline/resolve", "POST",
                              json={"a": c["a"], "b": c["b"], "action": act})
                    if res:
                        st.success(res["message"])
                        if res.get("need_rebuild"):
                            st.session_state["need_rebuild"] = True
                        st.rerun()

    if st.session_state.get("need_rebuild"):
        st.warning("归一表已改，图谱还没更新。")
        if st.button("重建图谱", type="primary"):
            with st.spinner("重建中 ..."):
                r = api("/pipeline/rebuild", "POST")
            if r and r["ok"]:
                st.success(f"重建完成，{r['elapsed_s']} 秒")
                st.session_state["need_rebuild"] = False
                st.rerun()
            else:
                st.error(f"重建失败：{(r or {}).get('tail')}")


def _stats_tab():
    d = api("/graph/stats")
    if not d:
        return
    c1, c2 = st.columns(2)
    with c1:
        st.metric("节点总数", d["total_nodes"])
        st.dataframe(pd.DataFrame(d["nodes"]), use_container_width=True,
                     hide_index=True)
    with c2:
        st.metric("关系总数", d["total_rels"])
        st.dataframe(pd.DataFrame(d["rels"]), use_container_width=True,
                     hide_index=True)


# ---------------- 入口 ----------------

def main():
    st.sidebar.title("inverted-llm-graphrag")
    page = st.sidebar.radio("页面", ["查询", "数据管线"], label_visibility="collapsed")

    health = api("/health")
    if health:
        db_ok = health["neo4j"]["ok"]
        model_ok = health["ollama"]["available"]
        st.sidebar.divider()
        st.sidebar.caption(
            f"{'✅' if db_ok else '❌'} Neo4j　"
            f"{'✅' if model_ok else '❌'} {health['ollama']['gen_model']}")
        st.sidebar.caption(f"schema 来源：{health['schema']['source']}")

    if page == "查询":
        page_query()
    else:
        page_pipeline()


if __name__ == "__main__":
    main()
