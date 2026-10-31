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
from dataclasses import replace
from typing import Callable

from .llm import LLMConfig, LLMError, chat, load_config
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
        {"page": 3, "quote": "原文摘录，保持原语言不翻译",
         "quote_zh": "上面这句摘录的中文译文；原文是中文时留空"}
      ],
      "confidence": "high | medium | low"
    }
  ]
}

规则：
1. findings 必须覆盖所有必填板块，block_id 不能自创，也不能改字。
2. evidence.page 用材料里 [page N] 标记的数字；quote 从原文直接复制，不要翻译、不要改写、
   不要拼接两处不相邻的话。quote_zh 是这条摘录的中文译文，要完整通顺地译出，
   不要省略、不要加解释；原文本身就是中文时，quote_zh 写空字符串。
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
            if isinstance(ev, dict) or isinstance(ev, str):
                evidence.append(Evidence.from_dict(ev))

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


def _extract_once(
    paper: Paper,
    template: Template,
    cfg: LLMConfig,
    *,
    lang: str,
    max_chars: int,
    repair_rounds: int,
    say: Callable[[str], None],
    label: str,
) -> tuple[Extraction, list[str]]:
    """对一份（可能是板块子集的）模板跑「抽取 → 校验 → 修复」。"""
    messages = build_messages(paper, template, lang=lang, max_chars=max_chars)
    say(f"{label}调用 {cfg.model}（材料约 {min(paper.chars, max_chars):,} 字符）…")

    raw = chat(messages, cfg)
    result = parse_response(raw, template, lang=lang, model=cfg.model)
    issues = validate_extraction(result, template)
    say(f"  第 1 轮：{len(result.findings)} 块，校验问题 {len(issues)} 个")

    round_no = 1
    while issues and round_no <= repair_rounds:
        say("  修复第 %d 轮：%s%s" % (
            round_no, "；".join(issues[:3]), "…" if len(issues) > 3 else ""))
        messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": _REPAIR_TEMPLATE.format(
                issues="\n".join(f"- {i}" for i in issues))},
        ]
        raw = chat(messages, cfg)
        result = parse_response(raw, template, lang=lang, model=cfg.model)
        issues = validate_extraction(result, template)
        round_no += 1
        say(f"  第 {round_no} 轮：{len(result.findings)} 块，"
            f"校验问题 {len(issues)} 个")

    if issues:
        say("  ⚠ 仍未解决：" + "；".join(issues))
    return result, issues


def _missing_block_ids(issues: list[str]) -> list[str]:
    """从校验问题里抠出「缺少必填板块 xxx」的板块 id。"""
    ids = []
    for issue in issues:
        m = re.match(r"缺少必填板块 (\S+)", issue)
        if m:
            ids.append(m.group(1))
    return ids


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
    """按模板抽取一篇论文。

    **多板块模板按组分批抽取。** 12 块的 deep 模板连同中英对照证据
    一次输出会撞上模型 8k token 的输出上限，JSON 被截断后整轮作废
    ——实测一次 deep 抽取 12 块全空、连标题都没解析出来。
    按模板的分组分次调用，每组独立预算；论文材料作为公共前缀放在
    固定位置，DeepSeek 的上下文缓存让多次调用的输入成本几乎不变。

    全部板块都解析失败时抛 :class:`LLMError`：空结果对用户是比
    「任务失败」更糟的谎言，工作台必须把它当失败展示。

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
        每组校验不通过时的最大修复轮数。
    on_progress : callable, 可选
        进度回调，用于命令行输出与工作台进度条。

    返回
    ----
    Extraction
        各组合并后的结果。即使某组部分缺失也会返回，
        由调用方根据界面提示决定是否接受。
    """
    cfg = cfg or load_config()
    say = on_progress or (lambda _msg: None)

    groups = template.groups()
    findings: list[Finding] = []
    title = ""

    for gi, (gz, ge) in enumerate(groups, 1):
        blocks = [b for b in template.blocks if (b.group, b.group_en) == (gz, ge)]
        sub = replace(template, blocks=blocks)
        label = (f"抽取正文 第 {gi}/{len(groups)} 组「{gz}」（{len(blocks)} 块）："
                 if len(groups) > 1 else "抽取正文：")
        result, _ = _extract_once(
            paper, sub, cfg, lang=lang, max_chars=max_chars,
            repair_rounds=repair_rounds, say=say, label=label)
        findings.extend(result.findings)
        title = title or result.paper_title

    merged = Extraction(
        template_id=template.id, paper_title=title,
        lang=lang, model=cfg.model, findings=findings,
    )

    # 跨组补抽：某组仍有「缺少必填板块」时，把这些块单独再要一轮。
    issues = validate_extraction(merged, template)
    missing = [template.block(bid) for bid in _missing_block_ids(issues)]
    missing = [b for b in missing if b is not None]
    if missing and repair_rounds:
        sub = replace(template, blocks=missing)
        say(f"补抽 {len(missing)} 个缺失板块 …")
        result, _ = _extract_once(
            paper, sub, cfg, lang=lang, max_chars=max_chars,
            repair_rounds=1, say=say, label="补抽：")
        patched = {f.block_id: f for f in result.findings if not f.is_empty}
        seen = set()
        findings = []
        for f in merged.findings:
            if f.block_id in patched:
                findings.append(patched[f.block_id])
            else:
                findings.append(f)
            seen.add(f.block_id)
        for bid, f in patched.items():
            if bid not in seen:
                findings.append(f)
        merged = Extraction(
            template_id=template.id,
            paper_title=title or result.paper_title,
            lang=lang, model=cfg.model, findings=findings,
        )
        issues = validate_extraction(merged, template)

    if issues:
        say("⚠ 仍未解决的问题：" + "；".join(issues))
    else:
        say("✓ 校验通过")

    if not merged.findings or all(f.is_empty for f in merged.findings):
        raise LLMError(
            "模型输出全部无法解析（常见原因：单次输出超出模型上限被截断）。\n"
            "  已按组分批抽取仍失败——请换输出上限更高的模型后重试。")
    return merged
