"""规范化：把原始文件的脏格式转成统一结构。

源数据里五种脏写法，都在这里处理：

  编号省略前缀    tenlong-001、002、005、006
  区间省略前缀    chengxin-001～006、025～030
  「通用」后缀    tenlong/yuelong通用
  分隔符不统一    、  ，  /  混用
  规格嵌在名称里  硬质合金车刀片（CNMG120408）

这里是纯规则，不涉及模型。能写成规则的交给规则，比交给模型可靠、可复现、零成本。
"""

from __future__ import annotations

import re
from datetime import date, datetime

# 全角半角都算分隔符
SEPARATORS = "、,，;；"

# 「通用」二字表示该条适用于它前面列出的所有型号
GENERIC = "通用"

# 型号前缀。源数据里就这五个
MODELS = ("tenlong", "yuelong", "chengxin", "huanmai", "heyue")


# ---------------- 编号展开 ----------------

def split_multi(text: str) -> list[str]:
    """按任意分隔符切分，去掉空白项。"""
    if text is None:
        return []
    s = str(text).strip()
    if not s or s.lower() == "nan":
        return []
    return [x.strip() for x in re.split(f"[{re.escape(SEPARATORS)}]", s) if x.strip()]


def expand_ids(text: str) -> list[str]:
    """展开设备/工具/备件的编号列表。

    两种省略写法都要处理：
        tenlong-001、002、005、006     后面的项省略了前缀
        chengxin-001～006、025～030    区间也省略前缀

    第二种最容易出错，正则必须维护「上一个出现的前缀」。第一版漏了这个分支，
    流程2 的 4 台被展开成 1 台，边数少一大截而且不报错。
    """
    out: list[str] = []
    prefix: str | None = None

    for part in split_multi(text):
        # 去掉「（4台）」这类标注
        part = re.sub(r"[（(]\s*\d+\s*[台个]\s*[)）]", "", part).strip()
        if not part:
            continue

        m = re.match(r"([A-Za-z]+)-(\d+)", part)        # 带前缀：tenlong-001
        if m:
            prefix, start, rest = m.group(1), int(m.group(2)), part[m.end():]
        else:
            m2 = re.match(r"(\d+)", part)                # 省略前缀：002
            if not m2 or not prefix:
                continue
            start, rest = int(m2.group(1)), part[m2.end():]

        m3 = re.match(r"\s*[～~]\s*(\d+)", rest)          # 区间：～006
        if m3:
            out += [f"{prefix}-{i:03d}" for i in range(start, int(m3.group(1)) + 1)]
        else:
            out.append(f"{prefix}-{start:03d}")

    return out


def parse_generic_models(text: str) -> list[str]:
    """解析「通用设备」这类字段，返回型号列表。

        tenlong/yuelong通用           -> ['tenlong', 'yuelong']
        tenlong/yuelong/chengxin通用  -> ['tenlong', 'yuelong', 'chengxin']
        全机型通用                     -> ['*']
        tenlong                       -> ['tenlong']
    """
    s = (text or "").strip()
    if not s or s.lower() == "nan":
        return []
    if s.startswith("全机型") or s == GENERIC:
        return ["*"]
    s = s.replace(GENERIC, "")
    return [x.strip() for x in re.split(r"[/、,，]", s) if x.strip()]


# ---------------- 名称与规格 ----------------

_NAME_SPEC = re.compile(r"^(?P<name>[^（(]+)[（(](?P<spec>[^）)]+)[)）]\s*$")


def split_name_spec(text: str) -> tuple[str, str]:
    """把嵌在名称里的规格拆出来。

        硬质合金车刀片（CNMG120408）  -> ('硬质合金车刀片', 'CNMG120408')
        立铣刀（Φ10）                 -> ('立铣刀', 'Φ10')
        三爪卡盘                      -> ('三爪卡盘', '')
    """
    s = (text or "").strip()
    m = _NAME_SPEC.match(s)
    if m:
        return m.group("name").strip(), m.group("spec").strip()
    return s, ""


def clean_text(text) -> str:
    """去空白、统一空值。pandas 读出来的 NaN 会变成字符串 'nan'。"""
    if text is None:
        return ""
    s = str(text).strip()
    return "" if s.lower() in ("nan", "none", "") else s


def parse_date(value) -> date | None:
    """解析日期。支持 datetime 对象和常见字符串格式。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ---------------- 自检 ----------------

class NormalizeError(Exception):
    """规范化过程中发现不一致，且无法自动修复。"""


def check_count(expected: int, got: list, label: str) -> str:
    """核对展开后的数量与源文件标注的台数。

    工艺流程图每格都标了「（4台）」，展开后数量对不上说明解析漏了。
    这条自检不是可选的：第一版解析器漏了省略前缀的分支，
    流程2 的 4 台被展开成 1 台，边数少一大截而且不报错。
    """
    n = len(got)
    if expected and n != expected:
        raise NormalizeError(f"{label}：源文件标注 {expected} 台，展开得到 {n} 台")
    return f"{label} {n} 台"


def extract_count(text: str) -> int | None:
    """从「tenlong-001～004（4台）」里取出标注的台数。"""
    m = re.search(r"[（(]\s*(\d+)\s*[台个]\s*[)）]", str(text or ""))
    return int(m.group(1)) if m else None
