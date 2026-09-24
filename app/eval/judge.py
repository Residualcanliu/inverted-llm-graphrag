"""答案判定。

**能确定性判定的，绝不用 LLM。** 这是整个评测可信度的基础 ——
LLM judge 有已知的位置偏差、长度偏差、自我偏好，而且有实证表明
同一组对比换个裁判工具能翻转结论。

四类判定，按可靠性排：

  ① 数值比对     精确，零歧义
  ② 集合比对     F1 部分给分
  ③ 排序比对     看是否落在并列区内（实测有 33 项并列同一分值的情况）
  ④ 文本匹配     子串匹配为主，匹配不到的才交给 LLM 兜底

④ 只用在文档链路上 —— 它的答案是一段散文，必须从里面找值。
图链路的答案天生结构化，走 ①②③。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.llm import ollama_client as oc

# 拒答的标志。问到知识库里没有的东西时，回答该包含其中任意一个
REFUSE_MARKERS = ("查不到", "没有相关", "资料中没有", "无法回答", "没有找到",
                  "未找到", "不存在相关信息", "知识库中没有", "没有记录",
                  "无法确定", "不知道")


@dataclass
class Verdict:
    qid: str
    pipeline: str
    correct: bool
    score: float            # 0~1，集合类按 F1 给部分分
    method: str             # number | set | tie | text | refuse | llm | error
    detail: str = ""

    def to_dict(self) -> dict:
        return {"qid": self.qid, "pipeline": self.pipeline,
                "correct": self.correct, "score": round(self.score, 4),
                "method": self.method, "detail": self.detail[:200]}


# ---------------- 取值 ----------------

def values_from_rows(rows: list[dict] | None) -> set[str]:
    """把查询结果拍平成值集合。

    **不比列名，比值** —— 模型写 `AS 设备` 还是 `AS 受影响设备` 都行，
    两者都合法，列名不同不该扣分。
    """
    out: set[str] = set()
    for r in rows or []:
        for v in r.values():
            if v is None:
                continue
            if isinstance(v, (list, tuple, set)):
                out |= {str(x).strip() for x in v if x is not None}
            else:
                out.add(str(v).strip())
    return out


def _as_set(v) -> set[str]:
    if v is None:
        return set()
    if isinstance(v, (list, tuple, set)):
        return {str(x).strip() for x in v}
    return {str(v).strip()}


def _f1(got: set[str], want: set[str]) -> float:
    if not want:
        return 1.0 if not got else 0.0
    tp = len(got & want)
    if tp == 0:
        return 0.0
    p, r = tp / len(got), tp / len(want)
    return 2 * p * r / (p + r)


# ---------------- LLM 兜底 ----------------

TEXT_MATCH_PROMPT = """下面是一段系统回答，以及一组应当出现在其中的条目。

请指出这组条目里，哪些确实在回答中出现过。注意回答可能用了范围表述
（例如「chengxin-001 到 006」覆盖了 chengxin-001、chengxin-002……）、
别名、或者换了一种说法。

【应当出现的条目】
{items}

【系统回答】
{text}

输出 JSON 数组，只列出确实出现了的条目原文：

["条目1", "条目2"]

只输出 JSON 数组，不要解释。"""


def _llm_match(missing: list[str], text: str) -> set[str]:
    """子串匹配不到时，让模型判断是否以其他形式出现。

    输入是**标准答案给的候选**，不是让模型自由抽取 —— 这样它只判「在不在」，
    比「从散文里抽出一组值」稳定得多，也不会因为抽取格式不同而误判。
    """
    if not missing or not text.strip():
        return set()
    prompt = TEXT_MATCH_PROMPT.format(
        items="\n".join(f"- {m}" for m in missing), text=text[:3000])
    try:
        g = oc.generate(prompt, num_predict=512)
    except Exception:                                 # noqa: BLE001
        return set()
    import json
    import re
    s = g.raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    a, b = s.find("["), s.rfind("]")
    if a == -1 or b < a:
        return set()
    try:
        got = json.loads(s[a:b + 1])
    except json.JSONDecodeError:
        return set()
    wanted = {str(x).strip() for x in missing}
    return {str(x).strip() for x in got} & wanted


# ---------------- 主判定 ----------------

def judge(item: dict, ans, *, use_llm: bool = True) -> Verdict:
    qid, layer = item["id"], item["layer"]
    pname = getattr(ans, "pipeline", "?")
    want_raw = item["answer"]

    # 拒答题：正确行为是说「查不到」
    if layer == "REFUSE":
        text = (getattr(ans, "text", "") or "") + (getattr(ans, "raw", "") or "")
        hit = any(m in text for m in REFUSE_MARKERS)
        # 编出答案来 = 幻觉，判错
        return Verdict(qid, pname, hit, 1.0 if hit else 0.0, "refuse",
                       "答了查不到" if hit else "没有明确说查不到，可能是编的")

    if getattr(ans, "error", ""):
        return Verdict(qid, pname, False, 0.0, "error", ans.error)

    atype = item["answer_type"]
    want = _as_set(want_raw.get(
        [k for k in want_raw if not k.startswith("_")][0]))

    # 图链路：答案天生结构化。
    #
    # 判据是 fields 里有没有结构化答案，**不是「有没有 rows」** —— 踩过的坑：
    # 文档链路的 rows 是检索到的 chunk 元信息（chunk id、来源、相似度），
    # 用它去比答案，文档链路会全军覆没，看起来像「传统 RAG 完全不行」。
    # 那是判定 bug，不是实验结果。
    fields = getattr(ans, "fields", None) or {}
    rows = fields.get("rows")
    if rows:
        got = values_from_rows(rows)
        if atype == "number":
            digits = {x for x in got if x.lstrip("-").replace(".", "", 1).isdigit()}
            ok = bool(digits & want)
            return Verdict(qid, pname, ok, 1.0 if ok else 0.0, "number",
                           f"得 {sorted(digits)[:3]} 期望 {sorted(want)[:3]}")
        if atype == "list":
            # 排序题：看模型给的项落没落在并列区里。
            #
            # 区 = 所有分值与「第 k 名」相同的项。无并列时区就是答案本身，
            # 这时判定退化成严格比对。
            # 判对要同时满足两条：给的每一项都在区内、给的数量跟题目要的一致。
            tied = _as_set(want_raw.get("_tied", []))
            zone = tied or want
            ok = bool(got) and got <= zone and len(got) == len(want)
            return Verdict(qid, pname, ok, 1.0 if ok else _f1(got, zone), "tie",
                           f"得 {len(got)} 项，{len(got & zone)} 项落在区内"
                           f"（区共 {len(zone)} 项）")
        score = _f1(got, want)
        return Verdict(qid, pname, score >= 0.999, score, "set",
                       f"命中 {len(got & want)}/{len(want)}，多余 {len(got - want)}")

    # 文档链路：答案是一段散文
    text = getattr(ans, "text", "") or ""
    hit = {v for v in want if v in text}
    missing = sorted(want - hit)
    # 标记 LLM 有没有真的被调用过。判据是「调用过没有」，不是「结果变了没有」——
    # 早期写成 `method = "llm" if missing != sorted(want) else "text"`，
    # 全命中的时候 missing 是空列表，跟 want 不等，于是被错标成 llm。
    used_llm = False
    if missing and use_llm:
        used_llm = True
        extra = _llm_match(missing, text)
        if extra:
            hit |= extra
            missing = sorted(want - hit)
    got = hit
    score = _f1(got, want) if want else 0.0
    return Verdict(qid, pname, score >= 0.999, score,
                   "llm" if used_llm else "text",
                   f"文本命中 {len(got)}/{len(want)}"
                   + (f"，缺 {missing[:3]}" if missing else ""))
