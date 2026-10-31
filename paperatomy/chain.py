"""论证链：把一篇论文自己的推理链抽出来，并逐段演给你看。

为什么不是「流水线图」
----------------------
二期第一版做了三个固定原语（流程 / 例子 / 分类），问题在于**叙事结构也是固定的**：
每篇论文套上去都长一个样，模型只换了文字。那不是「大模型推理」，那是填空题。

正确的分工是：

    固定的是**渲染引擎**（怎么画、怎么动、怎么标注差异）
    不固定的是**叙事结构**（这篇论文凭什么、改了什么、效果在哪）

所以这里只有一个原语：``chain``——一条属于这篇论文的论证链。

节点是有类型的，类型决定画法
----------------------------
::

    problem   问题     论文要打的靶子，最好带量级
    insight   洞察     作者的关键判断：为什么这件事可能做到
    change    改动     具体改了什么 —— **必须给 before / after 对照**
    mechanism 机制     这个改动通过什么起作用
    effect    效果     可测的结果，必须带数字
    cost      代价     代价、失效条件、适用边界
    taxonomy  框架     分类体系（综述的骨架）
    question  待解     尚未解决的问题

``change`` 是整条链的眼：**「改进具体体现在哪里」只能靠前后对照看明白**。
所以它必须带 ``before`` / ``after`` 两份**图示规格**，由前端画出来并动画演示——
不是两段文字并排。

图示规格（diagram spec）
------------------------
四种，都是声明式的，前端手写渲染：

::

    {"kind": "tokens", "n": 8, "label": "逐 token 串行", "cadence": 1.4}
    {"kind": "blocks", "items": ["Attention", "FFN", "Norm"], "highlight": 0}
    {"kind": "bars",   "items": [{"label": "基线", "value": 1}, {"label": "本文", "value": 2.6}]}
    {"kind": "text",   "text": "无法图示时的退路"}

``tokens`` 的 ``cadence`` 是「每步间隔」的象征值：同一排 token，
间隔 1.4 和 0.45 播出来就是慢与快——**速度差异被看见了，而不只是被描述**。

三条硬约束
----------
1. 没有原文依据的节点不给写（关键节点必须有 ``evidence``）
2. 数字必须是原文的数字，不许外推、不许换算
3. 抽不出来就返回空链——**空链是合法结果**，不硬凑
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .llm import LLMConfig, chat, load_config
from .model import Evidence
from .parse import Paper

__all__ = [
    "NodeType",
    "NODE_TYPES",
    "ChainNode",
    "Chain",
    "list_node_types",
    "validate_chain",
    "chain_warnings",
    "build_chain_messages",
    "parse_chain_response",
    "extract_chain",
    "MAX_NODES",
    "MIN_NODES",
]

#: 一条链最多几个节点。超过就不是「一条链」，而是一份提纲。
MAX_NODES = 8

#: 少于这么多个节点，构不成论证。
MIN_NODES = 3


# ---------------------------------------------------------------------------
# 节点类型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeType:
    """一种节点的语义与它在图上的画法。"""

    name: str
    label: str
    """中文短标签，显示在节点头部。"""
    color: str
    """前端用的强调色。"""
    guidance: str
    """写进 prompt 的要求——这个类型到底该写什么、不该写什么。"""
    needs_before_after: bool = False
    needs_metric: bool = False

    def catalog_line(self) -> str:
        extra = ""
        if self.needs_before_after:
            extra = "（必须给 before / after 图示）"
        elif self.needs_metric:
            extra = "（必须给数字）"
        return f"- {self.name:10s} {self.label}：{self.guidance}{extra}"


NODE_TYPES: dict[str, NodeType] = {
    "problem": NodeType(
        "problem", "问题", "#E8836B",
        "论文要打的靶子。写清「不做会怎样」，能量化就量化（成本、延迟、精度损失）。",
    ),
    "insight": NodeType(
        "insight", "洞察", "#D9B45E",
        "作者的关键判断：为什么这条路可能走通。是前提假设，不是操作步骤。",
    ),
    "change": NodeType(
        "change", "改动", "#6FB3D9",
        "具体改了什么。**必须给出改动前后的对照**，这是整条链最重要的一环。",
        needs_before_after=True,
    ),
    "mechanism": NodeType(
        "mechanism", "机制", "#8FBF9F",
        "这个改动通过什么起作用：它改变了哪个量、消掉了哪一项开销、为什么等价。",
    ),
    "effect": NodeType(
        "effect", "效果", "#7ED0A8",
        "可测的结果。必须带原文数字（倍数、百分比、绝对值），不要写「显著提升」。",
        needs_metric=True,
    ),
    "cost": NodeType(
        "cost", "代价", "#C58FB0",
        "代价、失效条件、适用边界。论文自己承认的限制优先。",
    ),
    "taxonomy": NodeType(
        "taxonomy", "框架", "#9B9BE0",
        "分类体系：把一片工作按某个维度切开。综述常用，也用于多方案对比。"
        "必须给 root 树，不能只把分类写进 detail。",
    ),
    "question": NodeType(
        "question", "待解", "#93A4B8",
        "尚未解决的问题，或论文自己指出的未来方向。",
    ),
}

#: 图示规格支持的种类。
DIAGRAM_KINDS = ("tokens", "blocks", "bars", "text")


def list_node_types() -> list[NodeType]:
    """按目录顺序返回全部节点类型。"""
    return list(NODE_TYPES.values())


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class ChainNode:
    """论证链上的一个节点。"""

    id: str
    type: str
    label: str = ""
    detail: str = ""
    metric: dict[str, Any] | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    root: dict[str, Any] | None = None
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def type_label(self) -> str:
        spec = NODE_TYPES.get(self.type)
        return spec.label if spec else self.type

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "detail": self.detail,
            "evidence": [e.to_dict() for e in self.evidence],
        }
        for key in ("metric", "before", "after", "root"):
            value = getattr(self, key)
            if value:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChainNode":
        return cls(
            id=str(raw.get("id", "")),
            type=str(raw.get("type", "")),
            label=str(raw.get("label", "")),
            detail=str(raw.get("detail", "")),
            metric=raw.get("metric") if isinstance(raw.get("metric"), dict) else None,
            before=raw.get("before") if isinstance(raw.get("before"), dict) else None,
            after=raw.get("after") if isinstance(raw.get("after"), dict) else None,
            root=raw.get("root") if isinstance(raw.get("root"), dict) else None,
            evidence=[Evidence.from_dict(e) for e in (raw.get("evidence") or [])],
        )


@dataclass
class Chain:
    """一篇论文的论证链。"""

    thesis: str = ""
    """一句话主张：全文的论点。"""
    nodes: list[ChainNode] = field(default_factory=list)
    confidence: str = "medium"
    note: str = ""
    """为什么这样串 / 为何某些环节缺失。"""
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thesis": self.thesis,
            "confidence": self.confidence,
            "note": self.note,
            "nodes": [n.to_dict() for n in self.nodes],
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Chain":
        return cls(
            thesis=str(raw.get("thesis", "")),
            confidence=str(raw.get("confidence", "medium")),
            note=str(raw.get("note", "")),
            nodes=[
                ChainNode.from_dict(item) for item in (raw.get("nodes") or [])
                if isinstance(item, dict)
            ],
            evidence=[Evidence.from_dict(e) for e in (raw.get("evidence") or [])],
        )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    """尽量把 "2.6×" / "2–3" / 5 取成一个用于动画的数，取不到返回 None。"""
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(m.group()) if m else None


def _check_diagram(spec: Any, where: str) -> list[str]:
    """校验一份图示规格。"""
    if not isinstance(spec, dict):
        return [f"{where} 的图示规格不是对象"]
    kind = str(spec.get("kind", "")).strip()
    if kind not in DIAGRAM_KINDS:
        return [f"{where} 的图示 kind {kind!r} 不在 {list(DIAGRAM_KINDS)} 中"]

    issues: list[str] = []
    if kind == "tokens":
        n = int(_num(spec.get("n")) or 0)
        if not 1 <= n <= 12:
            issues.append(f"{where} 的 tokens.n 应为 1–12，实际 {n}")
        cadence = _num(spec.get("cadence"))
        if cadence is not None and not 0.15 <= cadence <= 3.0:
            issues.append(f"{where} 的 tokens.cadence 应为 0.15–3.0，实际 {cadence}")
    elif kind == "blocks":
        items = spec.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 6:
            issues.append(f"{where} 的 blocks.items 应为 1–6 项")
        elif not all(isinstance(i, str) and i.strip() for i in items):
            issues.append(f"{where} 的 blocks.items 里有空项")
    elif kind == "bars":
        items = spec.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 6:
            issues.append(f"{where} 的 bars.items 应为 1–6 项")
        else:
            for it in items:
                if not isinstance(it, dict) or _num(it.get("value")) is None:
                    issues.append(f"{where} 的 bars.items 有缺少数值的项")
                    break
    elif kind == "text":
        if not str(spec.get("text", "")).strip():
            issues.append(f"{where} 的 text 为空")
    return issues


def _check_taxonomy(root: Any, where: str) -> list[str]:
    if not isinstance(root, dict):
        return [f"{where} 缺少 root 节点"]
    if not str(root.get("name", "")).strip():
        return [f"{where} 的 root 缺少 name"]

    issues: list[str] = []
    total = 0
    max_depth = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal total, max_depth
        if not isinstance(node, dict):
            return
        total += 1
        max_depth = max(max_depth, depth)
        for child in node.get("children") or []:
            walk(child, depth + 1)

    walk(root, 1)
    if max_depth > 3:
        issues.append(f"{where} 的分类有 {max_depth} 层，最多 3 层")
    if total > 20:
        issues.append(f"{where} 的分类共 {total} 个节点，最多 20 个")
    return issues


def validate_chain(chain: Chain) -> list[str]:
    """校验一条论证链，返回问题列表（空表示通过）。

    只检查**结构性**问题——内容好不好由人判断，程序管不了。
    但结构错了，画出来一定是错的，那必须先拦住。
    """
    issues: list[str] = []

    # 空链是**合法结果**：纯理论推导、纯数据集论文本来就抽不出论证线。
    # 这里返回空问题列表，由调用方按「本文没有可演的链」处理。
    if not chain.nodes:
        return []

    if not MIN_NODES <= len(chain.nodes) <= MAX_NODES:
        issues.append(
            f"共 {len(chain.nodes)} 个节点，应在 {MIN_NODES}–{MAX_NODES} 之间"
        )

    types = [n.type for n in chain.nodes]
    for n in chain.nodes:
        if n.type not in NODE_TYPES:
            issues.append(f"节点 {n.id} 的类型 {n.type!r} 不在 {list(NODE_TYPES)} 中")

    if "problem" not in types:
        issues.append("缺少 problem 节点——一条论证链必须从要解决的问题开始")
    if not ({"change", "taxonomy"} & set(types)):
        issues.append("缺少 change 或 taxonomy 节点——没有「做了什么」，这条链没有内容")

    seen: set[str] = set()
    for n in chain.nodes:
        if not n.id:
            issues.append("有节点缺少 id")
        elif n.id in seen:
            issues.append(f"节点 id {n.id!r} 重复")
        seen.add(n.id)

        if not n.label.strip():
            issues.append(f"节点 {n.id} 缺少 label")
        elif len(n.label) > 16:
            issues.append(f"节点 {n.id} 的 label 有 {len(n.label)} 字，最多 16 字")

        spec = NODE_TYPES.get(n.type)
        if spec is None:
            continue

        if spec.needs_before_after:
            if not n.before or not n.after:
                issues.append(f"change 节点 {n.id} 必须同时给出 before 与 after")
            else:
                issues.extend(_check_diagram(n.before, f"{n.id}.before"))
                issues.extend(_check_diagram(n.after, f"{n.id}.after"))
            # 改动是本项目的核心画面，没有出处的改动不许画
            if not n.evidence:
                issues.append(f"change 节点 {n.id} 没有给出原文依据")

        if spec.needs_metric:
            if not n.metric or _num(n.metric.get("value")) is None:
                issues.append(f"effect 节点 {n.id} 缺少可读的数字（metric.value）")
            if not n.evidence:
                issues.append(f"effect 节点 {n.id} 没有给出原文依据")

        if n.type == "taxonomy":
            issues.extend(_check_taxonomy(n.root, f"taxonomy 节点 {n.id}"))

    if not chain.evidence and not any(n.evidence for n in chain.nodes):
        issues.append("整条链没有任何原文依据")

    if chain.confidence not in ("high", "medium", "low"):
        issues.append(f"confidence {chain.confidence!r} 不合法")

    return issues


def chain_warnings(chain: Chain) -> list[str]:
    """不阻止渲染、但值得提醒的问题。

    这些是「论文本身没写清」的痕迹，不是抽取错误。
    工具应该跟着论文一起不确定，而不是替它补一个听起来合理的理由。
    """
    warns: list[str] = []
    if not chain.nodes:
        return warns
    types = [n.type for n in chain.nodes]

    if "mechanism" not in types:
        warns.append("链条里没有「机制」环节：看不出改动为什么有效")
    if "effect" not in types:
        warns.append("链条里没有「效果」环节：结论缺少可核对的数字")
    if "cost" not in types:
        warns.append("链条里没有「代价」环节：论文可能没讨论适用边界")

    for n in chain.nodes:
        if n.type == "change":
            for side, spec in (("改动前", n.before), ("改动后", n.after)):
                if spec and spec.get("kind") == "text":
                    warns.append(f"「{n.label}」的{side}只能文字描述，无法图示")

    if chain.confidence == "low":
        warns.append("整条链低置信度：建议对照原文重新核对")

    return warns


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------

_LANG_HINT = {
    "zh": "节点文字用中文。方法名、指标名、数据集名保留英文原词，不要硬译。",
    "en": "Write node text in English.",
    "zh+en": "节点文字用中文（图上放不下双语，只写中文）。",
}

_SYSTEM = """\
你在做一件具体的事：把一篇论文的**论证链**抽出来。

不是摘要，不是目录，不是方法步骤清单。是「作者凭什么说服读者」的那条推理线：
他看到了什么问题 → 有什么关键判断 → 具体改了什么 → 改动通过什么起作用 →
量出来的结果是什么 → 付出了什么代价。

要求：

1. **链是线性的**，从头到尾一条。不要分叉、不要并列。
2. **只写原文有的**。原文没给的数字不要推算；原文没解释的机制不要替它补。
3. **label 极短**（不超过 12 字），detail 用一到两句话说清。
4. **change 节点必须给 before / after 的图示规格**——这是整条链最重要的一环，
   要让读者一眼看出「改进具体体现在哪里」。
5. 抽不出有意义的链条时，返回空的 nodes 数组，并在 note 里说明原因。
   **空链不丢人，编一条假链才丢人。**
6. evidence.quote **必须逐字照抄原文**，不要改写、不要压缩、不要拼接两处不相邻的话。
   quote_zh 是同一条摘录的中文译文，完整通顺地译出，不加解释。
"""

_SCHEMA = """\
输出结构（只输出 JSON，不要解释，不要包代码块）：

{
  "thesis": "一句话说清全文主张",
  "confidence": "high | medium | low",
  "note": "为什么这样串，或缺了哪个环节（可选）",
  "nodes": [
    {
      "id": "n1",
      "type": "problem",
      "label": "不超过 12 字",
      "detail": "一到两句话",
      "metric": {"value": "1.7", "unit": "百万 GPU 小时", "label": "训练开销"},
      "evidence": [{"page": 2, "quote": "原文摘录，不翻译"}]
    },
    {
      "id": "n3",
      "type": "change",
      "label": "串行生成改为先猜后验",
      "detail": "把逐 token 生成替换为「草稿 k 个 + 一次并行验证」。",
      "before": {"kind": "tokens", "n": 8, "label": "逐 token 串行", "cadence": 1.4},
      "after":  {"kind": "tokens", "n": 8, "label": "草稿 + 并行验证", "cadence": 0.45},
      "evidence": [{"page": 13,
                    "quote": "Mamba seamlessly integrates selective SSMs into a streamlined architecture, eliminating attention and MLP blocks",
                    "quote_zh": "Mamba 把选择性 SSM 集成进精简架构，去掉了注意力和 MLP 块"}]
    },
    {
      "id": "n5",
      "type": "effect",
      "label": "解码提速 2–3 倍",
      "detail": "在 T5X 上报告 2–3× 提升。",
      "metric": {"value": "2.6", "unit": "×", "label": "解码吞吐"},
      "evidence": [{"page": 13,
                    "quote": "Mamba seamlessly integrates selective SSMs into a streamlined architecture, eliminating attention and MLP blocks",
                    "quote_zh": "Mamba 把选择性 SSM 集成进精简架构，去掉了注意力和 MLP 块"}]
    },
    {
      "id": "n6",
      "type": "taxonomy",
      "label": "按生命周期切三层",
      "detail": "把全部工作切成架构 / 算法 / 系统三层。",
      "root": {
        "name": "资源高效 FMs",
        "children": [
          {"name": "架构", "what": "改结构", "children": [
            {"name": "Efficient Attention"}, {"name": "MoE"}
          ]},
          {"name": "算法", "what": "改训练与推理", "children": [
            {"name": "Quantization"}, {"name": "Pruning"}
          ]}
        ]
      },
      "evidence": [{"page": 3,
                    "quote": "Section 3 investigates the architectural innovations",
                    "quote_zh": "第 3 节考察架构层面的创新"}]
    }
  ]
}

**taxonomy 节点必须给 root**，不能把分类写进 detail 就算完。
root 是树：`{"name": ..., "what": 可选一句话, "children": [...]}`，最多 3 层、总节点不超过 20 个。

图示规格只有四种，不要自创：

- {"kind":"tokens","n":8,"label":"说明","cadence":1.4}
  一排 token 逐个点亮。cadence 是象征性的「每步间隔」，1.4 很慢、0.4 很快。
  **用它把「速度差异」演出来**，比写「更快」有用。
- {"kind":"blocks","items":["A","B","C"],"highlight":0}
  一排模块方块，highlight 是要强调的那块（下标，可省略）。
  用于结构改动：某块被替换、被删掉、被新增。
- {"kind":"bars","items":[{"label":"基线","value":1},{"label":"本文","value":2.6}],"unit":"×"}
  指标对比条。value 必须是数字。
- {"kind":"text","text":"无法图示时的一句话"}

数字规则：
- metric.value 必须是**能被程序读出来的数字**（"2.6" 而不是 "两倍多"）；
  原文给的是区间（如 2–3×）时，取区间下界或中位数，并在 detail 里写明区间。
- 不要外推、不要换算单位、不要把别的论文的数字搬过来。
"""

_REPAIR = """\
上一轮输出有以下问题：

{issues}

请修正后重新输出完整 JSON：
1. 补齐或改正有问题的节点，其余保持原样。
2. 如果某个节点确实找不到原文依据，就把它删掉——**不要编内容去满足校验**。
3. 仍然只输出 JSON。
"""


def build_chain_messages(
    paper: Paper,
    *,
    lang: str = "zh",
    max_chars: int = 90_000,
) -> list[dict]:
    """拼装论证链抽取的对话消息。"""
    catalog = "\n".join(t.catalog_line() for t in list_node_types())
    system = "\n".join([
        _SYSTEM,
        _LANG_HINT.get(lang, _LANG_HINT["zh"]),
        "【可用的节点类型】\n" + catalog,
        _SCHEMA,
    ])

    material = paper.condensed(max_chars)
    user = (
        "【论文材料】\n"
        f"（共 {paper.total_pages} 页）\n{material}\n\n"
        "请抽出这篇论文的论证链，按上面的结构输出 JSON。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json|JSON)?\s*(.*?)\s*```$", text, re.S)
    if m:
        return m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


def parse_chain_response(raw: str) -> Chain | None:
    """把模型返回的文本解析成 :class:`Chain`。

    返回 ``None`` 表示**输出根本解析不了**（要走修复循环）；
    返回一个 ``nodes`` 为空的 Chain 表示**模型明确说这篇论文没有可演的链**，
    这是合法结果，不该逼它重编一次。
    """
    try:
        data = json.loads(_strip_fence(raw))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return Chain.from_dict(data)


def extract_chain(
    paper: Paper,
    cfg: LLMConfig | None = None,
    *,
    lang: str = "zh",
    max_chars: int = 90_000,
    repair_rounds: int = 2,
    on_progress: Callable[[str], None] | None = None,
) -> Chain:
    """抽取一篇论文的论证链。

    返回空链是**合法结果**：说明这篇论文没有可演的论证线
    （纯理论推导、纯数据集论文常见）。

    参数
    ----
    repair_rounds : int
        校验不通过时的修复轮数。默认 2 轮——链是整份报告的门面，
        值得多给一次机会；再不行就承认抽不出来。
    """
    cfg = cfg or load_config()
    say = on_progress or (lambda _m: None)

    messages = build_chain_messages(paper, lang=lang, max_chars=max_chars)
    say("抽取论证链…")

    raw = chat(messages, cfg)
    chain = parse_chain_response(raw)
    issues = ["输出不是合法 JSON"] if chain is None else validate_chain(chain)
    say(f"  第 1 轮：{len(chain.nodes) if chain else 0} 个节点，问题 {len(issues)} 个")

    round_no = 1
    while issues and round_no <= repair_rounds:
        say(f"  修复第 {round_no} 轮：" + "；".join(issues[:2])
            + ("…" if len(issues) > 2 else ""))
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": _REPAIR.format(
                issues="\n".join(f"- {i}" for i in issues))},
        ]
        raw = chat(messages, cfg)
        chain = parse_chain_response(raw)
        issues = validate_chain(chain)
        round_no += 1
        say(f"  第 {round_no} 轮：{len(chain.nodes)} 个节点，问题 {len(issues)} 个")

    if chain is None:
        say("  ⚠ 模型输出无法解析，放弃")
        return Chain()

    if issues:
        say("  ⚠ 仍有问题：" + "；".join(issues[:3]))
    elif chain.nodes:
        kinds = " → ".join(n.type_label for n in chain.nodes)
        say(f"  ✓ 校验通过：{kinds}")
    else:
        say("  · 本文没有可演的论证链（正常结果）")

    return chain
