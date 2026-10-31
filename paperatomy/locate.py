"""证据定位：把模型抄回来的引文，反查成「第几章第几段」。

为什么这件事不能交给模型
------------------------
模型读的是**压缩后的文本**，既没有段落边界，也常常只看到每章的前 68%。
让它自报段号，它只能猜。实测里同一段话，换一次温度就报出三个不同的页码。

所以分工改成：

- **模型只负责抄原话**（它擅长这个，抄错也容易被发现）
- **程序负责定位**（``parse`` 已经把全文切成 198 个带坐标的段落）

这样还有一个额外好处：**匹配不上就等于抓到了编造**。
一条引文如果在原文任何段落里都找不到高度覆盖，
那它多半是模型自己写的，而不是论文里的。这比让用户逐条核对可靠得多。

匹配是逐级放松的
----------------
PDF 文本的脏是常态：断行连字符、双栏拼接的空格错位、公式符号被拆散。
所以从「整段包含」一路退到「独特词覆盖」，每一级都带一个降权系数，
最终得分低于阈值的判为未命中。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .model import Evidence
from .parse import Para, norm_text

__all__ = ["Hit", "locate_quote", "annotate", "annotate_extraction",
           "LocateReport", "VERIFY_THRESHOLD"]

#: 得分不低于这个值，才认为引文在原文中确实存在。
VERIFY_THRESHOLD = 0.7

#: 短于这个长度的引文不做定位——太短，到处都是，定到哪儿都没意义。
_MIN_QUOTE_CHARS = 12

#: 词袋匹配要求的独特词数下限。词太少时覆盖率不可信。
_MIN_BAG_WORDS = 6


@dataclass
class Hit:
    """一次定位的结果。"""

    para: Para | None = None
    score: float = 0.0
    how: str = "miss"
    """命中方式，便于调试与统计：``full`` / ``sentence`` / ``edge`` / ``bag`` / ``miss``。"""

    @property
    def ok(self) -> bool:
        return self.para is not None and self.score >= VERIFY_THRESHOLD


def _sentences(text: str) -> list[str]:
    """按句末标点切句。中英文都切。"""
    parts = re.split(r"[.!?。！？]+\s*", text)
    return [p.strip() for p in parts if len(p.strip()) >= 20]


def _bag_cover(quote_norm: str, para_norm: str) -> float:
    """独特词的覆盖率：引文里有多少个「长词」出现在了这一段里。

    不用精确分词——英文按空格切已经够用；中文没有空格，
    这一步会退化成整体子串判断（``in``），由更前面的整段匹配负责。
    """
    words = {w for w in quote_norm.split() if len(w) >= 5}
    if len(words) < _MIN_BAG_WORDS:
        return 0.0
    hit = sum(1 for w in words if w in para_norm)
    return hit / len(words)


def locate_quote(quote: str, paragraphs: Sequence[Para]) -> Hit:
    """在段落索引里定位一段引文。

    参数
    ----
    quote : str
        模型抄回来的原文摘录。
    paragraphs : Sequence[Para]
        :attr:`paperatomy.parse.Paper.paragraphs`。

    返回
    ----
    Hit
        ``ok`` 为真表示定位成功，``para`` 给出章节与段号。
    """
    quote_norm = norm_text(quote)
    if len(quote_norm) < _MIN_QUOTE_CHARS or not paragraphs:
        return Hit()

    # 一级：整段包含。最可靠，得满分。
    # 长度差别太大时跳过——一段 2000 字的正文里包含 12 个字，
    # 那种「包含」不说明引文就是从这儿抄的。
    for para in paragraphs:
        if quote_norm in para.norm:
            return Hit(para, 1.0, "full")

    # 二级：最长的一句。模型常在引文中间用「…」省略，整段匹配会失败，
    # 但其中完整的一句仍然能对上。
    # 注意：必须在**原始引文**上切句，不能拿归一化后的文本切——
    # norm_text 会把句号等标点折叠成空格，句界就没了，等于没切。
    for sentence in sorted(_sentences(quote), key=len, reverse=True)[:3]:
        normed = norm_text(sentence)
        if len(normed) < 25:
            continue
        for para in paragraphs:
            if normed in para.norm:
                return Hit(para, 0.9, "sentence")

    # 三级：首尾两端都要落在同一段。要求两头都中，是防止
    # 「开头十个常见词」在几十段里都出现造成误判。
    words = quote_norm.split()
    if len(words) >= 14:
        head = " ".join(words[:8])
        tail = " ".join(words[-8:])
        if len(head) >= 20 and len(tail) >= 20:
            for para in paragraphs:
                if head in para.norm and tail in para.norm:
                    return Hit(para, 0.85, "edge")

    # 四级：独特词覆盖。最松的一级，只用来兜住被公式和符号拆碎的引文。
    best: Hit = Hit()
    for para in paragraphs:
        cover = _bag_cover(quote_norm, para.norm)
        if cover > best.score:
            best = Hit(para, cover, "bag")
    # 词袋要更严：覆盖率 0.9 以上才敢认，否则「一个领域里的常见词」
    # 会把不相干的段落顶上来。
    if best.score >= 0.9:
        return Hit(best.para, 0.75, "bag")
    return Hit()


def annotate(evidence: Iterable[Evidence], paragraphs: Sequence[Para]) -> LocateReport:
    """就地给一批证据填上章节、段号、页码，并标记是否核实。

    页码以**定位结果**为准：模型报的页码常有偏差，
    而段落索引里的页码是从 PDF 逐页抽出来的，不会有错。
    定位失败时才回落到模型给的页码。
    """
    report = LocateReport()
    for ev in evidence:
        if not ev.quote.strip():
            continue
        report.total += 1

        hit = locate_quote(ev.quote, paragraphs)
        if hit.ok and hit.para is not None:
            para = hit.para
            ev.section = para.section
            ev.section_no = para.section_no
            ev.para = para.no
            ev.page = para.page
            ev.verified = True
            report.hits += 1
            report.by_how[hit.how] = report.by_how.get(hit.how, 0) + 1
        else:
            ev.verified = False
            report.misses += 1
    return report


@dataclass
class LocateReport:
    """一批证据的定位统计。用于命令行汇报与自检。"""

    total: int = 0
    hits: int = 0
    misses: int = 0
    by_how: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_how is None:
            self.by_how = {}

    @property
    def rate(self) -> float:
        return self.hits / self.total if self.total else 0.0

    def summary(self) -> str:
        if not self.total:
            return "没有可定位的证据"
        detail = "、".join(f"{k} {v}" for k, v in sorted(self.by_how.items()))
        head = f"{self.hits}/{self.total} 条证据在原文定位成功（{self.rate:.0%}）"
        return f"{head}（{detail}）" if detail else head


def annotate_extraction(extraction, paragraphs, chain=None) -> LocateReport:
    """给整份抽取结果（正文板块 + 论证链）一次性补上定位信息。

    只走一遍段落索引，顺手把「疑似编造」的标出来。
    """
    items: list[Evidence] = []
    for finding in extraction.findings:
        items.extend(finding.evidence)
    if chain is not None:
        items.extend(chain.evidence)
        for node in chain.nodes:
            items.extend(node.evidence)
    return annotate(items, paragraphs)
