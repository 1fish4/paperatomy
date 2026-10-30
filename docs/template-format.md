# 模板格式

模板是本项目最主要的扩展点：**加一套骨架 = 加一个 YAML 文件，不用改代码**。

---

## 一个最小模板

```yaml
id: meeting           # 必填。唯一标识，命令行用它选择模板
version: 1
name: 组会速记
name_en: Meeting brief
description: 用五分钟把一篇论文讲给组会听

estimated_minutes: 5

instruction: |
  你是一位帮助学生准备组会汇报的助手。
  读者要在五分钟内讲清这篇论文做了什么，以及为什么值得做。

blocks:
  - id: tl_dr
    title: 一句话结论
    group: 概览
    hint: 做了什么、结果如何，一句话说清。
    required: true
    max_chars: 140

  - id: why
    title: 为什么值得做
    group: 背景
    hint: 这个问题不解决会怎样，谁会受影响。
    required: true
    max_chars: 300
```

放到 `paperatomy/templates/` 下，`paperatomy templates` 就能看到它。

## 字段

### 模板级

| 字段 | 必填 | 说明 |
|------|:---:|------|
| `id` | ✅ | 唯一标识。小写字母加下划线 |
| `name` | ✅ | 中文名，命令行列表与页面标题用 |
| `name_en` | | 英文名。`--lang en` 时显示，缺省回落到 `name` |
| `version` | | 模板版本，默认 `1`。改结构时递增 |
| `description` / `description_en` | | 一句话说明适用场景 |
| `estimated_minutes` | | 用这个模板读一篇大约几分钟 |
| `instruction` / `instruction_en` | | 全局指令，拼进 prompt 开头。写清「读者是谁、要什么、不要什么」 |

### 板块级

| 字段 | 必填 | 说明 |
|------|:---:|------|
| `id` | ✅ | 板块唯一标识，同时是抽取结果的关联键 |
| `title` / `title_en` | ✅ | 显示名 |
| `group` / `group_en` | | 分组名，用于把板块聚成结构图上的几个区域 |
| `hint` / `hint_en` | | **抽取指令**——这一块写什么、不写什么。这是模板质量的关键 |
| `required` | | 默认 `true`。必填块缺失会在校验时报错 |
| `max_chars` | | 建议字数上限，默认 500。用于抑制长篇大论 |
| `evidence` | | `required`（默认）或 `optional`。要求给出页码与原文摘录 |

## 写 `hint` 的三条经验

**1. 写"不写什么"，比写"写什么"更有用。**

差的：`hint: 介绍研究背景`
好的：`hint: 只写本文直接针对的缺口。不要把整个领域的常识铺一遍，读者已经知道。`

模型天然倾向于"全面"，不约束就会写成教科书。

**2. 给可判断的标准，不给形容词。**

差的：`hint: 结果要有说服力`
好的：`hint: 写清「在哪个数据集上、比哪个基线、提升了多少」。没有数字的结论不算结果。`

**3. 明确空值怎么处理。**

差的：`hint: 总结局限`
好的：`hint: 若论文未讨论局限，写「论文未提及」，不要用常识补齐。`

这一条直接决定输出的可信度。

## 校验规则

加载模板时（`load_template`）：

- 缺 `id` 或 `blocks` → 报错
- 板块 `id` 重复 → 报错
- `evidence` 取值越界 → 报错

校验抽取结果时（`validate_extraction`）：

| 检查项 | 说明 |
|--------|------|
| 必填块齐全 | 缺失或内容为空都算问题 |
| 证据齐备 | `evidence: required` 的块必须给出处 |
| 板块 id 合法 | 结果里出现模板未定义的块 → 报错（模型会编） |
| 置信度合法 | 必须是 `high` / `medium` / `low` |

**这些检查不是洁癖。** 模型会漏块、会编出不存在的板块、会给不出证据，程序负责保证输出能被机器消费。

## 语言与模板的关系

语言**不是**模板的维度：

```bash
paperatomy extract paper.pdf --template skim --lang zh
paperatomy extract paper.pdf --template skim --lang en
paperatomy extract paper.pdf --template skim --lang zh+en
```

同一份模板，三种输出。模板只负责"有哪些板块"，不负责"用什么语言写"。

若把语言做成模板的维度，2 套模板 × 2 语言就是 4 份定义，维护必然失控。
