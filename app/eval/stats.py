"""统计检验。

配对设计，同一批题喂给多条链路，所以逐题的对错是可配对的。

  McNemar   二元结果（对/错）上两条链路有没有差异
  bootstrap 连续指标（F1 这类）的置信区间和差值分布

**样本量的硬限制，写在最前面**：配对 McNemar 的功效取决于**不一致对的数量**，
不是总题量。按标准公式推算，100 道左右只能可靠检出约 15 个百分点的差距，
150 道约 12 个。更小的差异测不出来 —— 而「测不出来」不等于「没差别」，
这是最容易误读的地方。

用法上：整体（153 道）和 A/B 大类可以谈显著性；分层之后每层只有 20–40 道，
只报准确率加置信区间，不做显著性声称。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# McNemar 用的卡方临界值（自由度 1，双侧 5%）
CHI2_CRIT_05 = 3.841


@dataclass
class CompareResult:
    a_name: str
    b_name: str
    n: int
    acc_a: float
    acc_b: float
    diff: float                 # b - a
    ci_low: float               # diff 的 95% CI
    ci_high: float
    b_only: int                 # a 错 b 对
    a_only: int                 # a 对 b 错
    both_right: int
    both_wrong: int
    chi2: float
    significant: bool
    note: str = ""

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.__dict__.items()}


def mcnemar(a: list[bool], b: list[bool]) -> tuple[int, int, float, bool]:
    """McNemar 检验。返回 (a独对, b独对, 卡方, 是否显著)。

    只关心不一致的两格。连续校正用在格子较小的时候。
    """
    a_only = sum(1 for x, y in zip(a, b) if x and not y)
    b_only = sum(1 for x, y in zip(a, b) if y and not x)
    n_disc = a_only + b_only
    if n_disc == 0:
        return a_only, b_only, 0.0, False
    # 连续性校正
    chi2 = (abs(a_only - b_only) - 1) ** 2 / n_disc
    return a_only, b_only, chi2, chi2 > CHI2_CRIT_05


def bootstrap_diff(a: list[float], b: list[float], n_boot: int = 2000,
                   seed: int = 42) -> tuple[float, float, float]:
    """配对 bootstrap。返回 (差值, CI 下界, CI 上界)。

    从已存的逐条结果重采样，不用重跑实验，成本几乎为零。
    """
    assert len(a) == len(b)
    n = len(a)
    if n == 0:
        return 0.0, 0.0, 0.0
    rnd = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        idx = [rnd.randrange(n) for _ in range(n)]
        sa = sum(a[i] for i in idx) / n
        sb = sum(b[i] for i in idx) / n
        diffs.append(sb - sa)
    diffs.sort()
    lo = diffs[int(0.025 * n_boot)]
    hi = diffs[int(0.975 * n_boot)]
    return sum(b) / n - sum(a) / n, lo, hi


def compare(a_name: str, b_name: str, a: list[float],
            b: list[float]) -> CompareResult:
    """比较两条链路。输入是逐题的得分（1.0 对 / 0.0 错，或 F1）。"""
    ab = [x >= 0.999 for x in a]
    bb = [x >= 0.999 for x in b]
    a_only, b_only, chi2, sig = mcnemar(ab, bb)
    diff, lo, hi = bootstrap_diff(a, b)

    n_disc = a_only + b_only
    note = ""
    if n_disc < 10:
        note = f"不一致对只有 {n_disc} 个，这个规模下检验基本没有功效"
    elif not sig:
        note = "不显著不等于没用 —— 样本量不足会同时造成假阳性和假阴性"

    return CompareResult(
        a_name=a_name, b_name=b_name, n=len(a),
        acc_a=sum(ab) / len(ab) if ab else 0.0,
        acc_b=sum(bb) / len(bb) if bb else 0.0,
        diff=diff, ci_low=lo, ci_high=hi,
        b_only=b_only, a_only=a_only,
        both_right=sum(1 for x, y in zip(ab, bb) if x and y),
        both_wrong=sum(1 for x, y in zip(ab, bb) if not x and not y),
        chi2=chi2, significant=sig, note=note)


def min_detectable(n: int, pi_d: float = 0.3) -> float:
    """给定题量，80% 功效下能检出的最小差距。

    用来给报告写清楚「这个样本量能说明什么」。是量级估计，不是精确值。
    """
    if n <= 0:
        return 1.0
    # n ≈ 7.84 × (pi_d − d²) / d²  反解 d
    # 7.84·pi_d − 7.84·d² = n·d²  →  d² = 7.84·pi_d / (n + 7.84)
    return math.sqrt(7.84 * pi_d / (n + 7.84))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """比例的 Wilson 置信区间。比标准正态近似稳，小样本也适用。"""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, center - half), min(1.0, center + half)
