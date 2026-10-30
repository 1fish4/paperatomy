"""可视化原语：让「方法怎么走」「问题卡在哪」「领域怎么分」被看见。

为什么不生成动画
----------------
用户想要的是排序算法演示视频那种效果。但那种动画是人写代码做出来的，
要从论文文字自动生成，中间缺一步——把自然语言方法描述翻译成可执行程序。
这是研究级难题，且不可能对所有论文成立。

换一个问法就有解：

    论文里的方法演示，形态是不是有限的？  —— 是
    演示所需的数据，论文里是不是本来就有？—— 是（toy example、收敛图、分类表）

所以真正的任务是**分类 + 填充**，不是**创作**。

分工
----
- **手写**：每个原语是一个前端组件，画法固定 → 样式统一、质量可控
- **LLM**：只做选型与填空 → 输出一份参数清单，不产出任何图形指令

三条硬约束
----------
1. **AI 不画图，只选型和填空。** 出问题时能定位：是选错原语，还是填错参数。
2. **没有原文依据，宁可不演。** 每个原语必须带 ``evidence``；
   论文没给具体例子、没给分类框架，对应原语直接跳过，不允许编数据凑图。
3. **一个原语只演一件事。** 观众一次只能跟一条线。
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
    "Visual",
    "PrimitiveSpec",
    "PRIMITIVES",
    "validate_visual",
    "validate_visuals",
    "visual_warnings",
    "build_visual_messages",
    "parse_visual_response",
    "extract_visuals",
    "list_primitives",
]


# ---------------------------------------------------------------------------
# 原语目录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrimitiveSpec:
    """一个可视化原语的说明。"""

    name: str
    label: str
    """中文短标签，用于前端徽章。"""
    purpose: str
    """这个原语回答什么问题，一句话。"""
    params_doc: str
    """写进 prompt 的参数格式说明。"""
    min_items: int = 0
    """参数里主要列表的最小条数。"""

    def catalog_text(self, index: int) -> str:
        """渲染成 prompt 里的一段。"""
        return (
            f"{index}. {self.name} —— {self.purpose}\n"
            f"   参数格式：{self.params_doc}"
        )


PRIMITIVES: dict[str, PrimitiveSpec] = {
    "pipeline": PrimitiveSpec(
        name="pipeline",
        label="方法流程",
        purpose="演「方法怎么走」：把方法拆成有先后顺序的阶段，逐段点亮",
        params_doc=(
            '{"stages": [{"name": "阶段名（不超过 10 字）", '
            '"what": "这一步做什么，具体到输入和输出", '
            '"why": "为什么必须有这一步；原文没讲清就填空字符串"}]}'
        ),
        min_items=2,
    ),
    "example": PrimitiveSpec(
        name="example",
        label="实例走查",
        purpose="演「问题卡在哪」：用论文里的具体例子走一遍，把卡点标出来",
        params_doc=(
            '{"scenario": "例子设定，一句话", '
            '"steps": [{"state": "这一步的局面", '
            '"issue": "卡在哪；这一步顺利就填 null"}]}'
        ),
        min_items=2,
    ),
    "taxonomy": PrimitiveSpec(
        name="taxonomy",
        label="分类体系",
        purpose="演「领域怎么分」：把综述的分类框架逐层展开",
        params_doc=(
            '{"root": {"name": "根节点", "children": '
            '[{"name": "一级类", "what": "一句话说明", "children": '
            '[{"name": "代表工作", "what": "一句话说明", "children": []}]}]}}'
        ),
        min_items=1,
    ),
}

#: 单篇论文最多出几个可视化。多了会稀释注意力，也会拉长生成时间。
MAX_VISUALS = 3


def list_primitives() -> list[PrimitiveSpec]:
    """按目录顺序返回全部原语。"""
    return list(PRIMITIVES.values())


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Visual:
    """一个可视化脚本。

    它不是图形，是**参数**。前端拿到 ``primitive`` 决定用哪个组件，
    再把 ``params`` 喂进去。同一份 params 永远画出同一张图。
    """

    primitive: str
    title: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    confidence: str = "medium"
    note: str = ""
    """为什么选它 / 或为什么跳过其他原语。用于自我说明，不是必需。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "primitive": self.primitive,
            "title": self.title,
            "params": self.params,
            "confidence": self.confidence,
            "note": self.note,
            "evidence": [{"page": e.page, "quote": e.quote} for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Visual":
        return cls(
            primitive=str(raw.get("primitive", "")),
            title=str(raw.get("title", "")),
            params=raw.get("params") or {},
            confidence=str(raw.get("confidence", "medium")),
            note=str(raw.get("note", "")),
            evidence=[
                Evidence(page=int(e.get("page", 0) or 0), quote=str(e.get("quote", "")))
                for e in (raw.get("evidence") or [])
                if isinstance(e, dict)
            ],
        )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return str(value or "").strip()


def _check_pipeline(params: dict) -> list[str]:
    issues: list[str] = []
    stages = params.get("stages")
    if not isinstance(stages, list):
        return ["pipeline 缺少 stages 列表"]
    if len(stages) < 2:
        issues.append(f"pipeline 的 stages 只有 {len(stages)} 个，至少需要 2 个")
    if len(stages) > 8:
        issues.append(f"pipeline 的 stages 有 {len(stages)} 个，最多 8 个")
    for i, st in enumerate(stages, 1):
        if not isinstance(st, dict):
            issues.append(f"pipeline 第 {i} 个 stage 不是对象")
            continue
        if not _text(st.get("name")):
            issues.append(f"pipeline 第 {i} 个 stage 缺少 name")
        if not _text(st.get("what")):
            issues.append(f"pipeline 第 {i} 个 stage（{_text(st.get('name'))}）缺少 what")
    return issues


def _check_example(params: dict) -> list[str]:
    issues: list[str] = []
    if not _text(params.get("scenario")):
        issues.append("example 缺少 scenario（例子的设定）")
    steps = params.get("steps")
    if not isinstance(steps, list):
        return issues + ["example 缺少 steps 列表"]
    if len(steps) < 2:
        issues.append(f"example 的 steps 只有 {len(steps)} 步，至少需要 2 步")
    if len(steps) > 6:
        issues.append(f"example 的 steps 有 {len(steps)} 步，最多 6 步")
    has_issue = False
    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict):
            issues.append(f"example 第 {i} 步不是对象")
            continue
        if not _text(st.get("state")):
            issues.append(f"example 第 {i} 步缺少 state")
        if st.get("issue"):
            has_issue = True
    if steps and not has_issue:
        # 没有卡点的「例子」等于复述流程，价值为零
        issues.append("example 的所有步骤 issue 都为空——没有卡点就不要用这个原语")
    return issues


def _check_taxonomy(params: dict) -> list[str]:
    issues: list[str] = []
    root = params.get("root")
    if not isinstance(root, dict):
        return ["taxonomy 缺少 root 节点"]
    if not _text(root.get("name")):
        issues.append("taxonomy 的 root 缺少 name")

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
        issues.append(f"taxonomy 有 {max_depth} 层，最多 3 层")
    if total > 20:
        issues.append(f"taxonomy 共 {total} 个节点，最多 20 个")
    if total < 3:
        issues.append(f"taxonomy 只有 {total} 个节点，分类框架至少需要 3 个")
    return issues


_CHECKERS = {
    "pipeline": _check_pipeline,
    "example": _check_example,
    "taxonomy": _check_taxonomy,
}


def validate_visual(visual: Visual) -> list[str]:
    """校验一个可视化脚本，返回问题列表（空表示通过）。"""
    issues: list[str] = []
    spec = PRIMITIVES.get(visual.primitive)
    if spec is None:
        return [f"未知原语 {visual.primitive!r}，可用：{list(PRIMITIVES)}"]
    if not visual.title.strip():
        issues.append(f"{visual.primitive} 缺少 title（这个演示在演什么）")
    if not isinstance(visual.params, dict) or not visual.params:
        return issues + [f"{visual.primitive} 的 params 为空"]
    # 原则二：没有原文依据，宁可不演
    if not visual.evidence:
        issues.append(f"{visual.primitive}「{visual.title}」没有给出原文依据")
    if visual.confidence not in ("high", "medium", "low"):
        issues.append(f"{visual.primitive} 的 confidence {visual.confidence!r} 不合法")
    issues.extend(_CHECKERS[visual.primitive](visual.params))
    return issues


def validate_visuals(visuals: list[Visual]) -> list[str]:
    """校验一批可视化脚本。"""
    issues: list[str] = []
    if len(visuals) > MAX_VISUALS:
        issues.append(f"共 {len(visuals)} 个可视化，最多 {MAX_VISUALS} 个")
    seen: set[str] = set()
    for v in visuals:
        if v.primitive in seen:
            issues.append(f"原语 {v.primitive} 重复出现，同一原语只用一次")
        seen.add(v.primitive)
        issues.extend(validate_visual(v))
    return issues


def visual_warnings(visual: Visual) -> list[str]:
    """不阻止渲染、但要让用户知道的问题。

    最典型的是 pipeline 里某个阶段没有 ``why``——
    这往往说明论文没讲清「为什么必须有这一步」，
    应当回到「值得追问的问题」里，而不是让前端假装它很合理。
    """
    warns: list[str] = []
    if visual.primitive == "pipeline":
        for st in visual.params.get("stages") or []:
            if isinstance(st, dict) and not _text(st.get("why")):
                warns.append(f"阶段「{_text(st.get('name'))}」原文未说明为什么必须有")
    if visual.confidence == "low":
        warns.append("低置信度：建议对照原文核对参数")
    return warns


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------

_LANG_HINT = {
    "zh": "参数里的文字用中文。专有名词、方法名、数据集名保留英文原词，不要硬译。",
    "en": "Write the parameter text in English.",
    "zh+en": "参数里的文字用中文（英文并列会把图撑坏，只写中文）。",
}

_SYSTEM = """\
你是一位把论文的方法翻译成「可视化脚本」的助手。

你的任务不是画图，也不是写代码，而是从给定的可视化原语里**选型**，
再把论文里的内容**填空**进去。图由前端组件画，画法固定。

三条原则，必须遵守：

1. **只选材料里有依据的原语。** 如果这篇论文没有可演的方法流程、没有具体例子、
   没有分类框架，就返回空的 visuals 数组。**不要为了凑数而编造。**
   一个都演不出来，比演一个假的好。
2. **参数里的每一个事实都必须来自原文。** 材料里没有的，不要写，也不要推测补全。
3. **一个原语只演一件事。** 不要把「问题 + 方法 + 结果」塞进同一个演示。

"""

_RULES = """\
输出规则：
1. 只输出 JSON，不要解释文字，也不要包在代码块里。
2. 最多 {max_visuals} 个可视化，同一个原语只能用一次。
3. 每个可视化必须给 evidence：page 用材料里 [page N] 标记的数字，
   quote 从原文直接复制，不翻译、不改写。
4. 如果这篇论文确实没有可演的内容，返回 {{"visuals": []}}，
   并在 note 里用一句话说明原因。

输出结构：

{{
  "visuals": [
    {{
      "primitive": "原语名",
      "title": "这个演示在演什么，一句话",
      "params": {{ }},
      "evidence": [{{"page": 3, "quote": "原文摘录"}}],
      "confidence": "high | medium | low",
      "note": "为什么选它（可选）"
    }}
  ]
}}
"""


def build_visual_messages(
    paper: Paper,
    *,
    lang: str = "zh",
    max_chars: int = 90_000,
    max_visuals: int = MAX_VISUALS,
) -> list[dict]:
    """拼装可视化选型的对话消息。"""
    catalog = "\n\n".join(
        spec.catalog_text(i) for i, spec in enumerate(list_primitives(), 1)
    )
    system = "\n".join([
        _SYSTEM,
        _LANG_HINT.get(lang, _LANG_HINT["zh"]),
        "【可选原语】\n" + catalog,
        _RULES.format(max_visuals=max_visuals),
    ])

    material = paper.condensed(max_chars)
    user = (
        "【论文材料】\n"
        f"（共 {paper.total_pages} 页）\n{material}\n\n"
        "请判断这篇论文适合用哪几个原语来演，并按上面的结构输出 JSON。"
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


def parse_visual_response(raw: str) -> list[Visual]:
    """把模型返回的文本解析成 :class:`Visual` 列表。解析失败返回空列表。"""
    try:
        data = json.loads(_strip_fence(raw))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []

    out: list[Visual] = []
    for item in data.get("visuals") or []:
        if not isinstance(item, dict):
            continue
        visual = Visual.from_dict(item)
        if visual.primitive:
            out.append(visual)
    return out


_REPAIR = """\
上一轮的输出存在以下问题：

{issues}

请修正后重新输出完整的 JSON：
1. 补齐或改正有问题的部分，没有问题的可视化保持原样。
2. 如果某个原语实在没有原文依据，就把它删掉，不要编内容去满足校验。
3. 仍然只输出 JSON。
"""


def extract_visuals(
    paper: Paper,
    cfg: LLMConfig | None = None,
    *,
    lang: str = "zh",
    max_chars: int = 90_000,
    max_visuals: int = MAX_VISUALS,
    repair_rounds: int = 1,
    on_progress: Callable[[str], None] | None = None,
) -> list[Visual]:
    """为论文挑选并填充可视化脚本。

    返回空列表是**合法结果**——说明这篇论文没有可演的内容，
    前端会显示「本文没有可动演示的内容」而不是硬凑一张图。

    参数
    ----
    repair_rounds : int
        校验不通过时的修复轮数。默认 1 轮：可视化的容错要求比正文低，
        两轮还不对就该放弃而不是继续烧 token。
    """
    cfg = cfg or load_config()
    say = on_progress or (lambda _m: None)

    messages = build_visual_messages(
        paper, lang=lang, max_chars=max_chars, max_visuals=max_visuals
    )
    say("挑选可视化原语…")

    raw = chat(messages, cfg)
    visuals = parse_visual_response(raw)
    issues = validate_visuals(visuals)
    say(f"  第 1 轮：{len(visuals)} 个，问题 {len(issues)} 个")

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
        visuals = parse_visual_response(raw)
        issues = validate_visuals(visuals)
        round_no += 1
        say(f"  第 {round_no} 轮：{len(visuals)} 个，问题 {len(issues)} 个")

    if issues:
        say("  ⚠ 仍有问题：" + "；".join(issues[:3]))
    elif visuals:
        say("  ✓ 校验通过：" + "、".join(v.primitive for v in visuals))
    else:
        say("  · 本文没有可演的内容（正常结果）")

    return visuals
