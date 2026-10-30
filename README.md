# paperatomy · 论文解剖

> 把一篇论文拆成一张结构图 —— 不是和它聊天，是让它自己交代清楚。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Tests](https://github.com/1fish4/paperatomy/actions/workflows/tests.yml/badge.svg)](https://github.com/1fish4/paperatomy/actions)
[![在线演示](https://img.shields.io/badge/%E5%9C%A8%E7%BA%BF%E6%BC%94%E7%A4%BA-1fish4.github.io-brightgreen.svg)](https://1fish4.github.io/paperatomy/)

**在线演示**：<https://1fish4.github.io/paperatomy/>
（同一篇 35 页综述的 `survey` 与 `skim` 两种报告，可直接点开操作）

---

## 为什么需要它

读英文论文最耗时的不是查单词，是**看完之后说不清这篇到底讲了什么**：研究问题是什么、为什么之前没人解决、方法的关键在哪、结论靠不靠得住。

现有的工具分两类，都不解决这个问题：

| 类型 | 代表 | 缺什么 |
|------|------|--------|
| **和论文聊天** | ChatPDF、SciSpace、ExplainPaper | 你得先知道该问什么 —— 而「不知道该问什么」正是读不懂的状态 |
| **论文关系网络** | Connected Papers、ResearchRabbit | 给你一张相似论文的网，但不告诉你这篇本身讲了什么 |

paperatomy 走第三条路：**按固定骨架把单篇论文拆开**。

打开就是六块或十二块内容，不需要你会提问。每块都带页码和原文摘录，可以逐条核对。

![综述报告效果](docs/images/report-survey.png)

<sub>上图：一篇 35 页 ACM 综述的 `survey` 模板产出（节选）。每块底部是原文页码与摘录。</sub>

## 与聊天式工具的四个区别

**1. 不需要提问。** 骨架是预设的，打开即完整。问不出来的人也能用。

**2. 每句话有出处。** 每个板块都附 `页码 + 原文摘录`。LLM 一定会编，所以必须让用户能核对 —— 聊天工具给你一段话，你不知道它从哪来。

**3. 明确告诉你哪里不确定。** 低置信度的板块会打标记；材料被压缩过时会写「原文省略」，而不是拿常识补齐。**宁可标缺失，不要编。**

**4. 结构可计算。** 输出是标准 JSON，不是一段散文。可以拿去做对比、做统计、做多篇汇总。

## 动态演示：把「问题」和「方法」演出来

文字说清「方法分三步」很啰嗦，看图三秒就懂。所以报告里除了文字板块，还有可播放的演示。

**关键设计：AI 不画图，只选型和填空。**

如果让模型直接生成动画，等于要求它把论文方法翻译成可执行程序——研究级难题，且不可能对所有论文成立。但换个问法就有解：论文里方法演示的形态是**有限**的，演示所需的数据论文里**本来就有**（toy example、分类表、流程描述）。

所以：**画法是手写的组件，模型只负责判断该用哪个、把论文里的内容填进去。**

| 原语 | 演什么 | 典型论文 |
|------|--------|---------|
| `taxonomy` 分类体系 | 分类框架逐层展开，一眼看清领域怎么分 | 综述、体系性论文 |
| `pipeline` 方法流程 | 方法拆成阶段，逐段点亮，每段配「做什么 / 为什么必须」 | 方法类论文 |
| `example` 实例走查 | 用论文里的具体例子走一遍，卡点标红 | 有 running example 的论文 |

```bash
paperatomy primitives                        # 列出全部原语
paperatomy extract paper.pdf -t survey       # 抽取时自动挑原语
paperatomy visuals out/paper.json            # 给已有报告补演示，不动正文
```

打开报告点「▶ 演示」，阶段会依次点亮，下方同步显示当前这一步在做什么、为什么必须有。

**三条硬约束**，都写进了校验：

- **没有原文依据就不演。** 每个演示必须带页码与原文摘录，没有的直接判不合法。一个编出来的动画比没有动画更糟。
- **`example` 必须有卡点。** 所有步骤都没有 `issue` 时判不合法——没有卡点的「例子」只是复述流程。
- **`pipeline` 缺 `why` 要提示。** 某阶段的必要性在论文里没讲清时，报告里会打橙标提醒，而不是假装它很合理。

> 实测：一篇 35 页综述跑出 3 个演示，首轮校验通过。其中 `pipeline`（投机解码）的第三阶段被判「原文未说明为什么必须有」——这条提示是对的，原文只写了流程没解释必要性。

细节见 [docs/visual-primitives.md](docs/visual-primitives.md)。

## 快速开始

```bash
git clone https://github.com/1fish4/paperatomy.git
cd paperatomy
pip install -e ".[dev]"
```

配置模型（一次性）：

```bash
paperatomy config set --api-key sk-xxx \
  --base-url https://api.deepseek.com/v1 \
  --model deepseek-chat
```

跑一篇论文：

```bash
paperatomy extract paper.pdf -t survey -l zh -o out/paper
```

产出三个文件：

```
out/paper.json   # 结构化结果，供程序消费
out/paper.md     # Markdown，便于贴进笔记
out/paper.html   # 单文件报告，双击打开
```

## 模板：换骨架不用改代码

模板是 YAML，定义「有哪些板块、每块要回答什么」。内置三套：

| 模板 | 用途 | 板块 |
|------|------|------|
| `skim` 速读 | 五分钟判断值不值得细读 | 6 块 |
| `deep` 精读 | 完整解剖，含方法步骤与实验 | 12 块 |
| `survey` 综述 | 抓住分类框架与开放问题 | 8 块 |

```bash
paperatomy templates                  # 列出全部模板
paperatomy extract p.pdf -t deep      # 换模板
```

**语言是参数，不是模板的维度**：

```bash
paperatomy extract paper.pdf -l zh      # 中文
paperatomy extract paper.pdf -l en      # 英文
paperatomy extract paper.pdf -l zh+en   # 中英并列
```

自己加一套模板（法律论文、医学病例报告），只需要往 `paperatomy/templates/` 放一个 YAML，不用改任何代码。格式见 [docs/template-format.md](docs/template-format.md)。

## 用自己的模型

不内置任何 key，不代理请求，不上传数据。支持三种配置方式：

```bash
# 1. 环境变量（推荐）
export PAPERATOMY_API_KEY=sk-...
export PAPERATOMY_BASE_URL=https://api.deepseek.com/v1
export PAPERATOMY_MODEL=deepseek-chat

# 2. 配置文件
paperatomy config set --api-key sk-...

# 3. 每次命令行传入
paperatomy extract paper.pdf --api-key sk-...
```

任何兼容 OpenAI 接口的服务都能用：DeepSeek、通义千问、Kimi、OpenAI、本地 Ollama。

## 只重新渲染，不重新花钱

改了样式或模板标题，不需要再调一次模型：

```bash
paperatomy render out/paper.json
```

它从已有 JSON 重建 Markdown 与 HTML。

## 调试解析（不调用模型）

```bash
paperatomy parse paper.pdf
```

输出章节划分、页数、字符数，以及被跳过的参考文献占比。**先确认解析对不对，再花钱抽取** —— 这个命令的意义就在这。

## 架构

```
PDF
 ↓  parse.py      版面解析：双栏还原、章节切分、页码保留
带页码的结构化文本
 ↓  extract.py    LLM 按模板抽取 → JSON（每块带证据）
Extraction
 ↓  validate      校验：必填块是否齐、证据是否缺、块 id 是否编造
 ↓  修复循环      校验不通过 → 把具体问题回喂 → 重试（最多 2 轮）
 ↓  render.py     报告（HTML）/ Markdown / JSON
```

**校验与修复循环是核心，不是可选项。** 模型会漏块、会编出不存在的板块、会给不出证据。程序负责保证输出能被机器消费，并让模型自己把缺口补上。

## 当前进度

| 模块 | 状态 |
|------|:---:|
| PDF 版面解析（双栏 / 章节 / 页码 / 参考文献剔除） | ✅ |
| 模板加载与校验 | ✅ |
| 内置模板 `skim` / `deep` / `survey` | ✅ |
| LLM 抽取 + 校验 + 修复循环 | ✅ |
| 三种产物（JSON / Markdown / HTML 报告） | ✅ |
| **可视化原语 `taxonomy` / `pipeline` / `example`** | ✅ |
| **演示播放（逐步点亮 + 详情面板 + 警告提示）** | ✅ |
| 命令行（`templates` / `primitives` / `parse` / `extract` / `visuals` / `render` / `config`） | ✅ |
| 单元测试 | ✅ 65 项 |
| 引用与相似论文网络 | ⬜ 三期 |

**实测**：一篇 35 页的 ACM 综述（*Resource-efficient Algorithms and Systems of Foundation Models*），
解析 8 个章节全部正确，压缩后 155,700 → 89,090 字符（参考文献占 38%，已剔除），
正文抽取 **23–29 秒**，8 个板块全部带页码证据；原语选型 **14 秒**，一次通过校验。
单篇总成本约几分钱。

## 路线图

- **一期（已完成）**：PDF → 模板抽取 → 单文件报告
- **二期（已完成）**：可视化原语 —— `taxonomy`（分类逐层展开）、`pipeline`（方法流程逐段点亮）、`example`（具体例子走一遍）
- **三期**：引用关系与相似论文网络（接 OpenAlex / Semantic Scholar 免费接口）、多篇对比

关于「为什么不做自动动画」，见 [docs/visual-primitives.md](docs/visual-primitives.md)。

## 不做的事

- **不做算法动画**。把论文方法自动变成可执行动画需要先把它变成代码，这是研究级难题。这里做的是结构的渐进呈现，不是真动画。
- **不做全文翻译**。翻译工具已经够多，缺的是结构。
- **不转载论文内容**。只输出结构化摘要与短引用，原文留在用户本地。
- **不内置 key、不上传论文**。所有请求直连你配置的服务。

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。最欢迎的两类贡献：**新模板**（一个 YAML 就够）和**真实论文的失败案例**（哪篇抽得不好，比夸奖有用）。

## 许可

MIT，见 [LICENSE](LICENSE)。
