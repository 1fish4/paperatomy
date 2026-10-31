"""paperatomy —— 把一篇论文解剖成结构图与一条可播放的论证链。

不是「和论文聊天」，而是按固定骨架把单篇论文拆开：
研究问题、背景、方法、数据、结论、局限，每块都带原文出处。

在此之上，抽出**这篇论文自己的论证链**——问题 → 洞察 → 改动 → 机制 → 效果 → 代价。
链条的节点数与顺序由论文决定，不是模板；渲染引擎只负责把它演出来。

核心设计
--------
- **模板是数据**：``templates/*.yaml`` 定义板块，加模板不用改代码
- **模板与语言正交**：同一份模板可产出中文 / 英文 / 双语
- **每块可回溯**：结论必须带页码与原文摘录，能一键核对
- **固定渲染、不固定叙事**：画法手写，链条由模型按论文生成

一分钟上手
----------
::

    from paperatomy import parse, extract, list_templates, load_config, extract_chain

    paper = parse("paper.pdf")
    template = {t.id: t for t in list_templates()}["survey"]
    result = extract(paper, template, load_config(), lang="zh")
    chain = extract_chain(paper, load_config(), lang="zh")
"""

from .chain import (
    NODE_TYPES,
    Chain,
    ChainNode,
    NodeType,
    build_chain_messages,
    chain_warnings,
    extract_chain,
    list_node_types,
    parse_chain_response,
    validate_chain,
)
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
from .render import chain_from_json, to_html, to_json, to_markdown

__version__ = "0.3.0"

__all__ = [
    # 数据模型
    "Block", "Template", "Evidence", "Finding", "Extraction",
    "LANGUAGES", "CONFIDENCE_LEVELS", "EVIDENCE_MODES", "TEMPLATE_DIR",
    "load_template", "list_templates", "validate_extraction",
    # 解析
    "parse", "Paper", "Page", "Section",
    # 抽取
    "extract", "build_messages", "parse_response",
    # 论证链
    "Chain", "ChainNode", "NodeType", "NODE_TYPES", "list_node_types",
    "extract_chain", "build_chain_messages", "parse_chain_response",
    "validate_chain", "chain_warnings",
    # 大模型
    "LLMConfig", "LLMError", "load_config", "save_config", "chat", "CONFIG_PATH",
    # 渲染
    "to_markdown", "to_json", "to_html", "chain_from_json",
]
