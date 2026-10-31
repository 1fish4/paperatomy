"""数据模型：模板、论文、抽取结果。

三者的关系
----------
::

    模板（Template）  ——  定义「要抽出哪些板块」
        ↓  作为 prompt 的结构与校验的依据
    抽取（Extraction）——  LLM 按模板产出的结构化结果
        ↓  按模板渲染
    输出（结构图 / Markdown）

两条贯穿全项目的硬约束
----------------------
**一、模板与语言正交**

模板只定义「有哪些板块、每块要回答什么」，不定义用哪种语言输出。
``lang`` 是运行参数（``zh`` / ``en`` / ``zh+en``），同一份模板可产出三种语言。
若把语言做成模板的一个维度，2 套模板 × 2 语言就是 4 份定义，维护必然失控。

**二、每块必须可回溯**

:class:`Finding` 带 :class:`Evidence`（页码 + 原文摘录）。
LLM 抽取一定会混入编造内容，用户必须能一键跳回原文核对。
这也是本工具与「和 PDF 聊天」类产品的根本区别：
聊天给你一段话，你不知道它从哪来；这里给你每句话的出处。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Block",
    "Template",
    "Evidence",
    "Finding",
    "Extraction",
    "LANGUAGES",
    "CONFIDENCE_LEVELS",
    "EVIDENCE_MODES",
    "load_template",
    "list_templates",
    "validate_extraction",
    "TEMPLATE_DIR",
]

#: 支持的语言。``zh+en`` 表示中英并列输出。
LANGUAGES = ("zh", "en", "zh+en")

#: 置信度三档。``low`` 时前端应显著提示用户核对原文。
CONFIDENCE_LEVELS = ("high", "medium", "low")

#: 证据要求。``required`` 必须给出处，``optional`` 可不给。
EVIDENCE_MODES = ("required", "optional")

#: 内置模板目录。
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------

@dataclass
class Block:
    """模板中的一个板块。

    一个板块 = 结构图上的一个节点 = 一次抽取任务。
    """

    id: str
    """板块唯一标识，同时作为 ``Finding`` 的关联键。"""

    title: str
    """中文标题。"""

    title_en: str = ""
    """英文标题。为空时回落到 ``title``。"""

    group: str = ""
    """中文分组名，用于把板块聚成结构图上的几个区域。"""

    group_en: str = ""
    """英文分组名。"""

    hint: str = ""
    """中文抽取指令——告诉模型这一块要写什么、不要写什么。"""

    hint_en: str = ""
    """英文抽取指令。"""

    required: bool = True
    """是否为必填板块。必填块缺失会在校验时报错。"""

    max_chars: int = 500
    """建议字数上限。用于抑制模型的长篇大论。"""

    evidence: str = "required"
    """证据要求，取值见 :data:`EVIDENCE_MODES`。"""

    def title_in(self, lang: str) -> str:
        """按语言取标题。"""
        if lang.startswith("en") and self.title_en:
            return self.title_en
        return self.title

    def hint_in(self, lang: str) -> str:
        """按语言取抽取指令。"""
        if lang.startswith("en") and self.hint_en:
            return self.hint_en
        return self.hint

    def group_in(self, lang: str) -> str:
        """按语言取分组名。"""
        if lang.startswith("en") and self.group_en:
            return self.group_en
        return self.group


@dataclass
class Template:
    """一套抽取模板。

    模板是**数据**，不是代码：加一套模板 = 加一个 YAML 文件。
    这是本项目对外的主要扩展点，也是「可复用」的具体体现——
    别人可以只拿走模板，塞进自己的流程。
    """

    id: str
    name: str
    name_en: str = ""
    description: str = ""
    description_en: str = ""
    version: int = 1
    estimated_minutes: int = 0
    instruction: str = ""
    """全局指令，拼进 system prompt 的开头。"""
    instruction_en: str = ""
    blocks: list[Block] = field(default_factory=list)

    @property
    def required_ids(self) -> list[str]:
        """必填板块的 id 列表。"""
        return [b.id for b in self.blocks if b.required]

    @property
    def block_ids(self) -> list[str]:
        """全部板块 id，保持模板中的顺序。"""
        return [b.id for b in self.blocks]

    def block(self, block_id: str) -> Block | None:
        """按 id 取板块，不存在时返回 None。"""
        for b in self.blocks:
            if b.id == block_id:
                return b
        return None

    def groups(self) -> list[tuple[str, str]]:
        """返回 ``(中文分组名, 英文分组名)`` 列表，按首次出现顺序去重。"""
        seen: list[tuple[str, str]] = []
        for b in self.blocks:
            key = (b.group, b.group_en)
            if key not in seen:
                seen.append(key)
        return seen


# ---------------------------------------------------------------------------
# 抽取结果
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    """一条原文依据。

    字段分两组：**模型给的**（``page`` / ``quote`` / ``quote_zh``）与
    **程序算出来的**（``section`` / ``section_no`` / ``para`` / ``verified``）。

    为什么这么分
    ------------
    让模型直接报「第 4.2 节第 3 段」它数不准——它看到的文本没有段的边界。
    所以模型只负责抄一段原文，由 :func:`paperatomy.locate.annotate`
    拿引文回原文匹配，命中哪段就填哪段。
    ``verified`` 因此有三态：``None`` 未校验、``True`` 已在原文定位到、
    ``False`` 原文里找不到（**很可能被编造**）。
    """

    page: int = 0
    """页码，从 1 开始。0 表示模型未给出。"""

    quote: str = ""
    """原文摘录。保持与原文一致，不要翻译。"""

    quote_zh: str = ""
    """中文译文。原文即中文时留空，展示层自动回落到 ``quote``。"""

    section: str = ""
    """所在章节标题，由定位器填写。"""

    section_no: str = ""
    """章节编号，如 ``4.2``。"""

    para: int = 0
    """章节内第几段，由定位器填写。"""

    verified: bool | None = None
    """引文是否在原文中定位到。``None`` 表示还没跑过定位。"""

    def __str__(self) -> str:
        if not self.quote:
            return ""
        return f"[{self.locator}] {self.quote}"

    @property
    def locator(self) -> str:
        """人类可读的出处坐标，如 ``§4.2 · 第 3 段 · p.11``。"""
        parts: list[str] = []
        if self.section_no:
            parts.append(f"§{self.section_no}")
        elif self.section:
            parts.append(f"§ {_short(self.section, 26)}")
        if self.para:
            parts.append(f"第 {self.para} 段")
        if self.page:
            parts.append(f"p.{self.page}")
        return " · ".join(parts) or "出处未标注"

    def to_dict(self) -> dict:
        """序列化。空字段不写，JSON 干净一些。"""
        out: dict[str, Any] = {"page": self.page, "quote": self.quote}
        for key in ("quote_zh", "section", "section_no"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.para:
            out["para"] = self.para
        if self.verified is not None:
            out["verified"] = self.verified
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> "Evidence":
        """从 JSON 还原。缺字段一律走默认值，兼容旧结果文件。"""
        if isinstance(raw, str):
            return cls(quote=raw.strip())
        if not isinstance(raw, dict):
            return cls()
        verified = raw.get("verified")
        return cls(
            page=_as_int(raw.get("page")),
            quote=str(raw.get("quote", "") or "").strip(),
            quote_zh=str(raw.get("quote_zh", "") or "").strip(),
            section=str(raw.get("section", "") or "").strip(),
            section_no=str(raw.get("section_no", "") or "").strip(),
            para=_as_int(raw.get("para")),
            verified=None if verified is None else bool(verified),
        )


def _as_int(value: Any) -> int:
    """尽量从各种写法里取整数。模型有时把页码写成 ``p.3``。"""
    if isinstance(value, int):
        return value
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else 0


def _short(text: str, limit: int) -> str:
    """截断到 ``limit`` 字符，超出加省略号。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Finding:
    """一个板块的抽取结果。"""

    block_id: str
    content: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    confidence: str = "medium"

    @property
    def is_empty(self) -> bool:
        """内容为空或明确写了「未提及」。"""
        text = self.content.strip()
        return not text or text in {"论文未提及", "not reported", "N/A"}

    @property
    def needs_review(self) -> bool:
        """是否需要人工核对。低置信度或没有证据时建议核对。"""
        return self.confidence == "low" or not self.evidence


@dataclass
class Extraction:
    """整篇论文的抽取结果。"""

    template_id: str
    paper_title: str = ""
    lang: str = "zh"
    model: str = ""
    findings: list[Finding] = field(default_factory=list)

    def by_block(self) -> dict[str, Finding]:
        """以 ``block_id`` 为键的字典。"""
        return {f.block_id: f for f in self.findings}

    def missing(self, template: Template) -> list[str]:
        """模板要求但结果里缺失或为空的板块 id。"""
        got = {f.block_id for f in self.findings if not f.is_empty}
        return [bid for bid in template.required_ids if bid not in got]


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------

def _parse_blocks(raw: Any, template_id: str) -> list[Block]:
    if not raw:
        raise ValueError(f"模板 {template_id!r} 没有定义任何板块（blocks）。")
    if not isinstance(raw, list):
        raise ValueError(f"模板 {template_id!r} 的 blocks 应为列表。")

    blocks: list[Block] = []
    seen: set[str] = set()
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"模板 {template_id!r} 第 {i} 个板块应为字典。")
        bid = str(item.get("id") or "").strip()
        if not bid:
            raise ValueError(f"模板 {template_id!r} 第 {i} 个板块缺少 id。")
        if bid in seen:
            raise ValueError(f"模板 {template_id!r} 的板块 id {bid!r} 重复。")
        seen.add(bid)

        evidence = str(item.get("evidence", "required"))
        if evidence not in EVIDENCE_MODES:
            raise ValueError(
                f"模板 {template_id!r} 板块 {bid!r} 的 evidence {evidence!r} "
                f"不在 {list(EVIDENCE_MODES)} 中。"
            )

        blocks.append(Block(
            id=bid,
            title=str(item.get("title") or bid),
            title_en=str(item.get("title_en", "")),
            group=str(item.get("group", "")),
            group_en=str(item.get("group_en", "")),
            hint=str(item.get("hint", "")),
            hint_en=str(item.get("hint_en", "")),
            required=bool(item.get("required", True)),
            max_chars=int(item.get("max_chars", 500)),
            evidence=evidence,
        ))
    return blocks


def load_template(path: str | Path) -> Template:
    """从 YAML 加载一套模板。

    异常
    -----
    ValueError
        缺少必填字段、板块 id 重复、或取值越界。
    """
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"模板文件 {path} 的内容应为字典。")

    tid = str(raw.get("id") or "").strip()
    if not tid:
        raise ValueError(f"模板文件 {path} 缺少 id。")

    return Template(
        id=tid,
        name=str(raw.get("name") or tid),
        name_en=str(raw.get("name_en", "")),
        description=str(raw.get("description", "")),
        description_en=str(raw.get("description_en", "")),
        version=int(raw.get("version", 1)),
        estimated_minutes=int(raw.get("estimated_minutes", 0)),
        instruction=str(raw.get("instruction", "")),
        instruction_en=str(raw.get("instruction_en", "")),
        blocks=_parse_blocks(raw.get("blocks"), tid),
    )


def list_templates(directory: str | Path | None = None) -> list[Template]:
    """加载目录下的全部模板，按文件名排序。"""
    directory = Path(directory) if directory else TEMPLATE_DIR
    return [load_template(p) for p in sorted(directory.glob("*.yaml"))]


def validate_extraction(extraction: Extraction, template: Template) -> list[str]:
    """校验抽取结果是否满足模板要求，返回问题描述列表（空表示通过）。

    检查项：

    1. 必填板块是否齐全
    2. 要求证据的板块是否给出了证据
    3. 板块 id 是否都在模板中（防止模型编出不存在的块）
    4. 置信度取值是否合法
    """
    issues: list[str] = []
    known = set(template.block_ids)
    got = extraction.by_block()

    unknown = sorted(set(got) - known)
    if unknown:
        issues.append(f"结果中出现模板未定义的板块：{unknown}")

    for bid in template.required_ids:
        finding = got.get(bid)
        if finding is None:
            issues.append(f"缺少必填板块 {bid}")
        elif finding.is_empty:
            issues.append(f"必填板块 {bid} 内容为空")

    for bid, finding in got.items():
        block = template.block(bid)
        if block is None:
            continue
        if block.evidence == "required" and not finding.evidence:
            issues.append(f"板块 {bid} 要求给出原文证据，但结果中没有")
        if finding.confidence not in CONFIDENCE_LEVELS:
            issues.append(
                f"板块 {bid} 的置信度 {finding.confidence!r} 不合法，"
                f"应为 {list(CONFIDENCE_LEVELS)} 之一"
            )
    return issues
