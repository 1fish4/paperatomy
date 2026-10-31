# 贡献指南

最欢迎的两类贡献：**新模板**（一个 YAML 就够）、**真实论文的失败案例**
（哪篇抽得不好，比夸奖有用）。

---

## 环境

```bash
git clone https://github.com/1fish4/paperatomy.git
cd paperatomy
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

不要提交任何 API key。配置走环境变量或 `~/.paperatomy/config.yaml`，
后者已在 `.gitignore` 里排除。

## 加一套模板

这是最简单的贡献方式，**不需要写代码**。

1. 复制 `paperatomy/templates/skim.yaml`
2. 改 `id`、`name`、`blocks`
3. 跑 `paperatomy templates` 确认能加载
4. 找一篇真实论文试：《`paperatomy extract paper.pdf -t 你的模板》`
5. 按实际效果改 `hint`，直到抽取质量满意

格式与写作经验见 [docs/template-format.md](docs/template-format.md)。

### 写 `hint` 的三条经验

**写"不写什么"，比写"写什么"更有用。**

差的：`hint: 介绍研究背景`
好的：`hint: 只写本文直接针对的缺口。不要把整个领域的常识铺一遍，读者已经知道。`

模型天然倾向"全面"，不约束就会写成教科书。

**给可判断的标准，不给形容词。**

差的：`hint: 结果要有说服力`
好的：`hint: 写清「在哪个数据集上、比哪个基线、提升了多少」。没有数字的结论不算结果。`

**明确空值怎么处理。**

好的：`hint: 若论文未讨论局限，写「论文未提及」，不要用常识补齐。`

这一条直接决定输出的可信度。

## 加一种节点类型，或一种图示

这是比加模板更高一层的贡献，需要写代码，但边界很清楚：

**加节点类型 = 一个 `NODE_TYPES` 条目 +（可选）一个校验分支 +（可选）一个渲染分支。**

模型只负责决定「用哪种节点、写什么内容」，画法永远由渲染引擎决定。
所以加类型不会让输出变得不可控。

### 加节点类型（`paperatomy/chain.py`）

```python
"limitation": NodeType(
    "limitation", "局限", "#C58FB0",
    "作者自己承认的方法局限。只写论文明确说的，不要替它推测。",
),
```

三步：

1. 往 `NODE_TYPES` 加一项（名字、中文标签、颜色、写进 prompt 的要求）
2. 需要特殊校验就在 `validate_chain` 里加分支（比如「必须有数字」用 `needs_metric=True`）
3. 前端 `detailHTML()` 加渲染分支——**不写也能跑**，会退化成「标题 + 说明」

### 加图示（`DIAGRAM_KINDS`）

四处：

1. `DIAGRAM_KINDS` 加名字
2. `_check_diagram()` 加校验（范围、必填字段）——**这一步不能省**，
   没有校验模型会填出画不出来的参数
3. `diagramHTML()` 加渲染分支
4. `report_template.html` 的样式加一段

### 加之前先回答三个问题

**一、它演的是哪一种「看懂」？**

现有的四种各自对应一种理解：
`tokens` 演速度、`blocks` 演结构替换、`bars` 演指标对比、`text` 承认画不出来。
答不上「演哪一种」就别加——多一种画法只会稀释注意力。

**二、没有原文依据时，能不能判断出来？**

每个类型都要能被校验判死。`change` 没有 before/after 直接拒绝，
`effect` 没有数字直接拒绝。**新类型也必须有一条「什么情况下不成立」的硬线。**

**三、缺东西时，该拒绝还是该警告？**

| 情况 | 处理方式 | 例子 |
|------|---------|------|
| 结构不成立，画出来一定是错的 | **拒绝**（进修复循环） | change 缺 before |
| 论文本身没写清，但画面仍然成立 | **警告**（照常渲染） | 链条缺「机制」环节 |

能写成警告的就别写成拒绝。工具应该跟着论文一起不确定，而不是拒绝工作。

## 代码贡献

### 结构

```
paperatomy/
├── parse.py          # PDF → 带页码的章节文本
├── model.py          # 模板 / 证据 / 抽取结果 + 校验
├── extract.py        # 模板抽取：prompt 构建、JSON 解析、修复循环
├── chain.py          # 论证链：节点类型 / 图示校验 / 抽取
├── llm.py            # OpenAI 兼容调用
├── render.py         # Markdown / JSON / HTML
├── report_template.html  # 报告前端（含深色舞台与播放器）
├── cli.py            # 命令行
└── templates/        # 模板（数据）
```

### 约定

- **Python ≥ 3.10**，用 `from __future__ import annotations`
- 注释与文档字符串写中文，代码标识符写英文
- **新增 Python 文件不需要特殊处理，但改动输出入口要保证中文不乱码**：
  英文 Windows 默认输出编码是 cp1252（无中文），
  入口处已调用 `enable_utf8()`，新增命令行脚本请照做
- 提交信息用语义化前缀：`feat:` / `fix:` / `docs:` / `test:` / `refactor:`

### 测试

```bash
pytest -q                    # 全部
pytest tests/test_parse.py   # 只跑解析
```

新增功能请带测试。特别是**解析逻辑**：用程序生成 PDF 来测
（见 `tests/test_parse.py` 的 `_make_pdf`），而不是依赖固定样本 ——
生成的样本能精确控制双栏位置、章节标题、页眉页脚，才测得到"解析器是否正确"。

### 报告问题

提 Issue 时最有用的信息：

- **哪篇论文抽得不好**（公开链接，或说明类型：实验类 / 综述 / 理论）
- **哪个板块错了**（内容与原文不符、该给证据没给、漏了一条）
- **用的是哪套模板**、什么模型

有这三条，比"结果不太准"能快十倍定位。

## 不接受的贡献

- 转载论文原文（只接受短引用）
- 内置 API key 或代理请求
- 让模型"用常识补齐"的改动 —— 缺失就标缺失，这是项目的底线
