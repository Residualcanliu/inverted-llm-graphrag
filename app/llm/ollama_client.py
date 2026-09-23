"""Ollama 封装。

两个必须处理的现实问题：

1. **thinking 模型会把 token 预算烧在推理上。** 实测 qwen3.8:27b 和 deepseek-r1:14b
   在 220 token 预算内都没吐出 Cypher —— 一个输出英文碎碎念，一个一路  thinking 到截断。
   所以要显式剥离 think 块，并把预算给够。

2. **模型喜欢套 Markdown 代码块。** 即使 prompt 里说了不要，还是会包 ```cypher。
   抽取函数要能容忍。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests

from app import config

THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
FENCE = re.compile(r"```(?:cypher|sql)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


@dataclass
class GenResult:
    text: str               # 抽取后的 Cypher（找不到查询结构就是空串）
    plain: str              # 剥掉 think 块、但**不做 Cypher 抽取**的文本
    raw: str                # 模型原始输出，排查问题用
    model: str
    elapsed_s: float        # 墙钟总耗时 = 加载 + prefill + 生成 + 网络
    eval_count: int
    eval_duration_s: float
    prompt_eval_count: int
    prompt_eval_duration_s: float
    load_duration_s: float  # 把模型搬进显存的时间
    truncated: bool         # 是否撞到 num_predict 上限

    # text 和 plain 的分工：
    #   text  —— 给 Text2Cypher 用。抽不出 Cypher 结构就返回空串，
    #            让上游按「生成失败」处理，而不是把垃圾往下传。
    #   plain —— 给文档模式用。那边要的是自然语言答案，本来就不含 Cypher 关键字，
    #            用 text 会把答案清空。
    #
    # 踩过的坑：文档模式接了 text，所有答案都是空的，看起来像「模型什么都没说」。

    @property
    def tok_per_s(self) -> float:
        return self.eval_count / self.eval_duration_s if self.eval_duration_s else 0.0

    def timing_breakdown(self) -> str:
        """把墙钟耗时拆开，看清慢在哪一段。

        为什么需要这个：实测一道题耗时 3.81s，而生成速度 84 tok/s、
        输出只有 34 个 token —— 算下来对不上。真相是 Ollama 闲置 5 分钟后
        把模型卸了，这 3.3 秒全花在重新加载上。只看总耗时会误判成"模型变慢了"。
        """
        parts = []
        if self.load_duration_s > 0.05:
            parts.append(f"加载 {self.load_duration_s:.2f}s")
        parts.append(f"prefill {self.prompt_eval_duration_s:.2f}s")
        parts.append(f"生成 {self.eval_duration_s:.2f}s")
        return " + ".join(parts) + f"  =  {self.elapsed_s:.2f}s"


def strip_think(text: str) -> tuple[str, bool]:
    """剥离 think 块。返回 (去思考后的文本, 是否本来有 think 块)。"""
    had = bool(THINK_BLOCK.search(text))
    return THINK_BLOCK.sub("", text).strip(), had


# 查询的起点：子句关键字出现在串首、行首，或者冒号之后。
#
# 为什么要求位置，不能只判断「包含」：英文散文里的 with / set / call / return
# 会撞上 Cypher 子句名。实测 "I cannot help with that" 会被切成 "with that"，
# 看着还挺像一条 WITH 语句。
#
# 冒号那一条是为了兜住「好的，以下是查询：MATCH ...」这种同行的前缀说明。
_CYPHER_START = re.compile(
    r"(?:^|\n|[:：])\s*"
    r"(MATCH|OPTIONAL\s+MATCH|CREATE|MERGE|DELETE|DETACH|SET|REMOVE"
    r"|WITH|UNWIND|RETURN|CALL|FOREACH|LOAD\s+CSV)\b",
    re.IGNORECASE,
)


def extract_cypher(text: str) -> str:
    """从模型输出里抠出 Cypher。

    容忍三种形态：裸 Cypher、```cypher 围栏、``` 围栏，以及带前缀说明的输出。

    抠完如果找不到查询的起点，返回空串 —— 让上游按「生成失败」处理。
    实测模型偶尔会输出字面量 `Cypher：` 这种没内容的字符串，
    或者直接拒答，返回它们会让下游白跑一轮（静态校验放行、EXPLAIN 才报错）。
    """
    text = text.strip()
    m = FENCE.search(text)
    if m:
        text = m.group(1).strip()
    m = _CYPHER_START.search(text)
    if not m:
        return ""
    if m.start() > 0:
        text = text[m.start():]
    return text.lstrip(":： \n\t").strip().rstrip(";").strip()


def generate(prompt: str, *, model: str | None = None,
             temperature: float | None = None,
             num_predict: int | None = None,
             seed: int | None = None,
             timeout: int = 300) -> GenResult:
    """调 Ollama 生成。返回结构化结果，含耗时和 token 统计。

    seed 用来固定采样，让同一批评测可以复现。

    为什么需要它：实测同一个模型跑同一批 12 条题，四次结果是
    12/12、11/12、12/12、11/12 —— temperature 0.1 也不是确定性的。
    单跑一次得出的合格率会高估系统可靠性。正式评测应当固定 seed，
    另外单独报一组不固定 seed 的结果来体现真实波动。
    """
    model = model or config.GEN_MODEL
    options = {
        "temperature": config.GEN_TEMPERATURE if temperature is None else temperature,
        "num_predict": config.GEN_NUM_PREDICT if num_predict is None else num_predict,
    }
    if seed is not None:
        options["seed"] = seed

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": options,
    }

    t0 = time.time()
    resp = requests.post(f"{config.OLLAMA_BASE_URL}/api/generate",
                         json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    elapsed = time.time() - t0

    if "error" in data:
        raise RuntimeError(f"Ollama 报错：{data['error']}")

    raw = data.get("response", "")
    body, _ = strip_think(raw)
    cypher = extract_cypher(body)

    eval_count = int(data.get("eval_count", 0))
    num_pred = payload["options"]["num_predict"]
    return GenResult(
        text=cypher,
        plain=body.strip(),
        raw=raw,
        model=model,
        elapsed_s=elapsed,
        eval_count=eval_count,
        eval_duration_s=data.get("eval_duration", 0) / 1e9,
        prompt_eval_count=int(data.get("prompt_eval_count", 0)),
        prompt_eval_duration_s=data.get("prompt_eval_duration", 0) / 1e9,
        load_duration_s=data.get("load_duration", 0) / 1e9,
        truncated=eval_count >= num_pred,
    )


def list_models() -> list[str]:
    resp = requests.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=10)
    resp.raise_for_status()
    return [m["name"] for m in resp.json().get("models", [])]


def embed(texts: list[str], *, model: str | None = None,
          timeout: int = 120) -> list[list[float]]:
    """批量嵌入。传统 RAG 基线用。"""
    model = model or config.EMBED_MODEL
    out = []
    for t in texts:
        resp = requests.post(f"{config.OLLAMA_BASE_URL}/api/embeddings",
                             json={"model": model, "prompt": t}, timeout=timeout)
        resp.raise_for_status()
        out.append(resp.json()["embedding"])
    return out
