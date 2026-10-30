"""抽取：按模板把论文变成结构化结果。

这个模块是本项目的核心。三件事：
1. 把模板与论文拼成 prompt
2. 解析模型返回的 JSON
3. **校验不通过就把问题回喂给模型重试** —— 这是最关键的机制

为什么要有修复循环
------------------
不指望模型一次就对。实测里它常犯三类错：
- 漏掉必填板块
- 编出模板里没有的板块
- 该给证据的不给证据

这些都能被程序精确检测（``validate_extraction``）。
检测到之后把**具体问题**回喂给模型，让它自己补 —— 两轮之内基本能收敛。

这比"调更贵的模型"或"人工检查"都可靠，因为它有明确的终止条件：
校验通过为止，或轮次用尽。
"""

from __future__ import annotations

import json
import re
from typing import Callable

from .llm import LLMConfig, chat, load_config
from .model import (
    Block,
    Evidence,
    Extraction,
    Finding,
    Template,
    validate_extraction,
)
from .parse import Paper

__all__ = ["extract", "build_messages", "parse_response"]

#: 语言指令。evidence 的 quote 一律保留原文，不翻译。
_LANG_HINT = {
    "zh": "用中文撰写。专有名词、方法名、数据集名保留英文原词，不要硬译。",
    "en": "Write in English.",
    "zh+en": (
        "先写一段中文，另起一行写 `---` 分隔，再写一段内容相同的英文。"
        "两段都要完整，不要只给摘要。"
    ),
}

#: 输出契约。写在 system prompt 末尾，模型返回必须符合这个形状。
_SCHEMA_HINT = """\
必须返回一个 JSON 对象，不要包含任何解释文字，也不要包在代码块里。结构如下：

{
  "paper_title": "论文标题",
  "findings": [
    {
      "block_id": "板块 id，必须来自给定列表",
      "content": "这一块的内容",
      "evidence": [
        {"page": 3, "quote": "原文摘录，保持原语言不翻译"}
      ],
      "confidence": "high | medium | low"
    }
  ]
}

规则：
1. findings 必须覆盖所有必填板块，block_id 不能自创，也不能改字。
2. evidence.page 用材料里 [page N] 标记的数字；quote 从原文直接复制，不要翻译、不要改写。
3. confidence 反映你在材料中找到的直接支撑程度：
   high = 原文有明确陈述；medium = 需要归纳；low = 推断为主或材料不足。
4. content 只写这一块要求的范围，不要越界替别的板块作答。
5. 材料里确实没有的，content 写「论文未提及」，confidence 写 low，不要用常识补齐。
"""

_REPAIR_TEMPLATE = """\
上一轮输出存在以下问题：

{issues}

请修正后重新输出完整的 JSON。要求：
1. 补齐或重写有问题的板块，其余板块保持原样。
2. 不要新增模板里没有的 block_id。
3. 仍然只输出 JSON，不要解释。
"""


def _block_spec(block: Block, index: int) -> str:
    """把一个板块渲染成 prompt 里的一段说明。"""
    lines = [
        f"{index}. block_id: {block.id}",
        f"   标题：{block.title}",
    ]
    if block.hint.strip():
        lines.append(f"   要求：{block.hint.strip().replace(chr(10), ' ')}")
    lines.append(f"   字数上限：约 {block.max_chars} 字")
    lines.append(
        "   证据：" + ("必须给出页码与原文摘录" if block.evidence == "required"
                       else "可选")
    )
    if not block.required:
        lines.append("   选填：材料不足时可以省略这一块")
    return "\n".join(lines)


def build_messages(
    paper: Paper,
    template: Template,
    lang: str = "zh",
    max_chars: int = 90_000,
) -> list[dict]:
    """拼装对话消息。

    分成两条 user 消息（板块要求 / 论文材料），
    这样模型更容易把「写什么」和「依据什么」分开对待。
    """
    instruction = template.instruction.strip() if lang != "en" or not template.instruction_en \
        else template.instruction_en.strip()
    if not instruction:
        instruction = "你是一位帮助研究者读懂论文的助手，忠于原文，不臆测。"

    system = "\n\n".join([
        instruction,
        "你只能依据给定的论文材料作答，不得引入外部知识补充论文里没有的内容。",
        _LANG_HINT.get(lang, _LANG_HINT["zh"]),
        _SCHEMA_HINT,
    ])

    specs = "\n\n".join(
        _block_spec(b, i) for i, b in enumerate(template.blocks, 1)
    )
    overview = (
        f"模板：{template.name}（{template.id}），共 {len(template.blocks)} 块，"
        f"其中必填 {len(template.required_ids)} 块。"
    )

    material = paper.condensed(max_chars)
    truncated_note = ""
    if paper.chars > max_chars:
        truncated_note = (
            f"\n（说明：原文共 {paper.total_pages} 页、约 {paper.chars} 字符，"
            f"受篇幅限制，下面是按章节压缩后的材料。压缩不改变原文措辞，"
            f"只是每节只保留了开头部分。）"
        )

    user_specs = f"【需要抽取的板块】\n{overview}\n\n{specs}"
    user_body = f"【论文材料】{truncated_note}\n{material}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_specs},
        {"role": "user", "content": user_body},
    ]


def _strip_fence(text: str) -> str:
    """去掉 ```json ... ``` 包裹。"""
    text = text.strip()
    m = re.match(r"^```(?:json|JSON)?\s*(.*?)\s*```$", text, re.S)
    if m:
        return m.group(1).strip()
    # 有的模型会在 JSON 前后写一句话，取第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


def parse_response(
    raw: str,
    template: Template,
    *,
    lang: str = "zh",
    model: str = "",
) -> Extraction:
    """把模型返回的文本解析成 :class:`Extraction`。

    解析失败不抛异常，而是返回一个空结果 ——
    让上层走校验与修复流程，比直接崩掉更有用。
    """
    try:
        data = json.loads(_strip_fence(raw))
    except Exception:
        return Extraction(template_id=template.id, lang=lang, model=model)

    if not isinstance(data, dict):
        return Extraction(template_id=template.id, lang=lang, model=model)

    findings: list[Finding] = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        block_id = str(item.get("block_id") or item.get("id") or "").strip()
        if not block_id:
            continue

        evidence: list[Evidence] = []
        for ev in item.get("evidence") or []:
            if isinstance(ev, dict):
                evidence.append(Evidence(
                    page=_as_int(ev.get("page")),
                    quote=str(ev.get("quote", "") or "").strip(),
                ))
            elif isinstance(ev, str) and ev.strip():
                evidence.append(Evidence(page=0, quote=ev.strip()))

        confidence = str(item.get("confidence", "medium") or "medium").lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"

        findings.append(Finding(
            block_id=block_id,
            content=str(item.get("content", "") or "").strip(),
            evidence=evidence,
            confidence=confidence,
        ))

    return Extraction(
        template_id=template.id,
        paper_title=str(data.get("paper_title", "") or ""),
        lang=lang,
        model=model,
        findings=findings,
    )


def _as_int(value) -> int:
    """尽量从各种写法里取页码。"""
    if isinstance(value, int):
        return value
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else 0


def extract(
    paper: Paper,
    template: Template,
    cfg: LLMConfig | None = None,
    *,
    lang: str = "zh",
    max_chars: int = 90_000,
    repair_rounds: int = 2,
    on_progress: Callable[[str], None] | None = None,
) -> Extraction:
    """抽取一篇论文。

    参数
    ----
    paper : Paper
        :func:`paperatomy.parse.parse` 的结果。
    template : Template
        板块定义。
    cfg : LLMConfig, 可选
        为空时按环境变量 / 配置文件加载。
    lang : str
        ``zh`` / ``en`` / ``zh+en``。
    max_chars : int
        送给模型的材料上限。超出时按章节压缩。
    repair_rounds : int
        校验不通过时的最大修复轮数。0 表示不修复。
    on_progress : callable, 可选
        进度回调，用于命令行输出。

    返回
    ----
    Extraction
        即使修复失败也会返回最后一轮结果，由调用方决定是否接受。
    """
    cfg = cfg or load_config()
    say = on_progress or (lambda _msg: None)

    messages = build_messages(paper, template, lang=lang, max_chars=max_chars)
    say(f"调用 {cfg.model} 抽取（材料约 {min(paper.chars, max_chars):,} 字符）…")

    raw = chat(messages, cfg)
    result = parse_response(raw, template, lang=lang, model=cfg.model)
    issues = validate_extraction(result, template)
    say(f"第 1 轮：{len(result.findings)} 块，校验问题 {len(issues)} 个")

    round_no = 1
    while issues and round_no <= repair_rounds:
        say(f"修复第 {round_no} 轮：" + "；".join(issues[:3])
            + ("…" if len(issues) > 3 else ""))
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": _REPAIR_TEMPLATE.format(
                issues="\n".join(f"- {i}" for i in issues))},
        ]
        raw = chat(messages, cfg)
        result = parse_response(raw, template, lang=lang, model=cfg.model)
        issues = validate_extraction(result, template)
        round_no += 1
        say(f"第 {round_no} 轮：{len(result.findings)} 块，"
            f"校验问题 {len(issues)} 个")

    if issues:
        say("⚠ 仍有未解决的问题：" + "；".join(issues))
    else:
        say("✓ 校验通过")

    return result
