"""paperatomy —— 把一篇论文解剖成一张结构图。

不是「和论文聊天」，而是按固定骨架把单篇论文拆开：
研究问题、背景、方法、数据、结论、局限，每块都带原文出处。

核心设计
--------
- **模板是数据**：``templates/*.yaml`` 定义板块，加模板不用改代码
- **模板与语言正交**：同一份模板可产出中文 / 英文 / 双语
- **每块可回溯**：结论必须带页码与原文摘录，能一键核对

一分钟上手
----------
::

    from paperatomy import parse, extract, list_templates, load_config

    paper = parse("paper.pdf")
    template = {t.id: t for t in list_templates()}["survey"]
    result = extract(paper, template, load_config(), lang="zh")
"""

from .extract import build_messages, extract, parse_response
from .llm import CONFIG_PATH, LLMConfig, LLMError, chat, load_config, save_config
from .model import (
    CONFIDENCE_LEVELS,
    EVIDENCE_MODES,
    LANGUAGES,
    TEMPLATE_DIR,
    Block,
    Evidence,
    Extraction,
    Finding,
    Template,
    list_templates,
    load_template,
    validate_extraction,
)
from .parse import Page, Paper, Section, parse
from .render import to_html, to_json, to_markdown, visuals_from_json
from .visuals import (
    PRIMITIVES,
    Visual,
    build_visual_messages,
    extract_visuals,
    list_primitives,
    parse_visual_response,
    validate_visual,
    validate_visuals,
    visual_warnings,
)

__version__ = "0.2.0"

__all__ = [
    # 数据模型
    "Block", "Template", "Evidence", "Finding", "Extraction",
    "LANGUAGES", "CONFIDENCE_LEVELS", "EVIDENCE_MODES", "TEMPLATE_DIR",
    "load_template", "list_templates", "validate_extraction",
    # 解析
    "parse", "Paper", "Page", "Section",
    # 抽取
    "extract", "build_messages", "parse_response",
    # 可视化
    "Visual", "PRIMITIVES", "list_primitives",
    "extract_visuals", "build_visual_messages", "parse_visual_response",
    "validate_visual", "validate_visuals", "visual_warnings",
    # 大模型
    "LLMConfig", "LLMError", "load_config", "save_config", "chat", "CONFIG_PATH",
    # 渲染
    "to_markdown", "to_json", "to_html", "visuals_from_json",
]
