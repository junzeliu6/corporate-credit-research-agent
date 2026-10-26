# 企业授信研究Agent — V2.5 完整 Live 测试报告

> 测试日期：2026-10-23
> 测试对象：石家庄以岭药业股份有限公司（002603.SZ）
> 最终状态：**PARTIAL_SUCCESS（可交付的部分成功）**
> 执行次数：**1 次；未自动重试、未扩大预算**

## 1. 结论

本次测试已经真实跑通“DeepSeek 主 Agent → 两个 Skill → Python 数据与图表工具 → 六章 Markdown 报告”的完整主链路：

- DeepSeek 成功连接并完成 6 次模型请求；
- 模型自主调用 `ownership-structure` 与 `financial-analysis` 两个 Skill 高层 Tool；
- 两个 `SKILL.md` 均由运行时真实加载，随后分别执行既有股权与财务 Python Tool；
- 股权比例校验、Graphviz SVG/PNG、财务事实和指标校验均成功；
- DeepSeek 实际撰写六章研究正文与 15 个尽调问题，Python 没有用固定模板替换模型正文；
- 完整 Markdown、股权图、上下文、来源、模型原稿、交付状态和执行轨迹均已保存。

本次不能标记为 `SUCCESS`。百度完成 6 次真实搜索，但只得到 2 个候选 URL；唯一尝试读取的国家药监局页面返回 HTTP 412，因而没有成功取得外部网页/PDF 原文，也没有执行出有效的 `extract_evidence` 结果。行业与政策章节虽已由模型交付，但只使用年报、一般行业分析和明确标为未核验的检索线索，尚未形成外部政策原文证据链。

## 2. 实际执行命令

```powershell
.venv\Scripts\python.exe -m credit_research_agent.cli "002603.SZ" `
  --run-complete-research "请研究以岭药业的企业背景、股权结构、经营情况、行业政策及财务状况，并从银行授信角度撰写完整研究报告。" `
  --llm-mode live --confirm-live `
  --search-mode live --confirm-search-live `
  --max-agent-turns 30 `
  --max-tool-calls 40 `
  --max-search-rounds 6 `
  --max-search-calls 6 `
  --max-search-requests 6 `
  --max-source-fetches 10 `
  --max-extract-calls 10 `
  --max-output-tokens 16000 `
  --request-timeout-seconds 120 `
  --max-model-retries 0 `
  --complete-dir data/complete_research_live_v25 `
  --cache-dir data/cache `
  --parsed-dir data/parsed
```

运行配置为 `deepseek-flash`、`thinking=disabled`、单次请求超时 120 秒、模型自动重试 0 次。密钥仅由环境变量读取，日志和产物未发现密钥内容。

## 3. 模型与 Tool 使用量

| 项目 | 实际值 |
| --- | ---: |
| DeepSeek 模型请求 / Agent 轮次 | 6 |
| Input Tokens | 73,745 |
| Output Tokens | 9,983 |
| Total Tokens | 83,728 |
| Function Tool 调用 | 10 |
| 百度真实搜索 API 请求 | 6 |
| 候选 URL | 2 |
| 网页原文读取尝试 | 1 |
| 成功读取原文 | 0 |
| 有效证据提取 | 0 |

前 5 次模型响应的 `finish_reason` 为 `tool_calls`，最后一次为 `stop`。没有发生输出截断；本次结果不佳并非 Token 上限不足所致。

DeepSeek 轨迹没有返回输入缓存命中/未命中的拆分，也没有返回费用金额。按测试时 DeepSeek 官方非高峰价格，费用只能估算为约 **¥0.041—¥0.114**，实际以平台账单为准。百度搜索标准版公开单价为 **¥0.036/次**，6 次请求的名义金额为 **¥0.216**；若仍在每日前 100 次免费额度内，平台实际扣费可能为 0，以百度控制台为准。

- DeepSeek 官方价格：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
- 百度搜索计费：https://ai.baidu.com/ai-doc/AppBuilder/Imca18xc6

## 4. Function Calling 与 Skill 运行记录

### 4.1 两个专业 Skill

1. `run_ownership_research({"ticker":"002603.SZ"})`
   - Runtime 绑定来源 `S01`，模型不能覆盖来源；
   - 加载 `.agents/skills/ownership-structure/SKILL.md`；
   - 实际执行 `build_ownership_artifacts`；
   - 上市公司层面 `54.68% + 45.32% = 100%`，控股股东上层 `100%`，两分支校验均通过；
   - 生成并嵌入 Graphviz SVG/PNG。

2. `run_financial_research({"ticker":"002603.SZ"})`
   - Runtime 绑定主来源 `S01`、比较来源 `S10`；
   - 加载 `.agents/skills/financial-analysis/SKILL.md`；
   - 实际执行 `build_financial_artifacts`；
   - 生成 46 条财务事实、30 个变化指标、12 个比率和 8 个风险标记；
   - 结构化校验通过；FY2024/FY2025 短期借款缺失被保留为缺口，没有被解释为零。

### 4.2 搜索与原文工具

模型随后围绕中药产业支持、集中带量采购、药品监管和企业经营资料自主发起 7 次搜索意图：

- 前 6 次真正发送至百度；其中 4 次为空结果，共返回国家医保局和国家药监局 2 个候选 URL；
- 对国家药监局页面执行 1 次 `fetch_source`，服务端返回 HTTP 412，按候选失败来源保留；
- 第 7 次搜索由 Python 预算守卫以 `search_call_limit_exceeded` 阻止；
- 因没有成功取得原文，模型没有可用对象继续完成 `extract_evidence`。

预算耗尽和单页失败没有丢弃已完成的两个 Skill、六章正文或图表，最终正确降级为 `PARTIAL_SUCCESS`。

## 5. 产物与作者边界

主产物目录：`data/complete_research_live_v25/CN-A-SZSE-002603/`

| 产物 | 结果 |
| --- | --- |
| `credit_research_report.md` | 42,068 bytes；六章齐全；DeepSeek 正文 |
| `credit_research_llm_draft.json` | 保存完整模型原稿与诊断信息 |
| `credit_research_delivery.json` | `authorship=live_llm`、六个主题全部交付、15 个尽调问题 |
| `credit_research_execution_trace.json` | 保存模型、Tool、Skill、预算、Token 与错误轨迹 |
| `credit_research_context.json` | 保存紧凑基本面、股权和财务上下文 |
| `credit_research_sources.json` | 保存官方来源、搜索候选与获取状态 |
| `assets/ownership_structure.svg/png` | 已生成；SVG/PNG 哈希与模块原始产物一致 |

交付文件明确记录：

- `llm_authored_report=true`；
- `python_generated_fallback_analysis=false`；
- `python_rewrote_llm_text=false`；
- `human_review_required=true`。

六个模型章节与保存的 DeepSeek 原稿逐段一致。Python 只插入股权图、财务表和运行限制，没有用模板重写专业正文。报告未显示 Fact ID、Source ID 等技术编号，也未发现 API Key。

## 6. 验收矩阵

| V2.5 验收项 | 结果 | 说明 |
| --- | --- | --- |
| 单次任务调用两个 Skill | 通过 | 两个 Skill 均由模型调用、Runtime 加载并执行 |
| 股权结构图正常生成 | 通过 | SVG/PNG 存在；比例、方向、布局和 100% 校验通过 |
| 财务数据与指标正确复用 | 通过 | 结构化校验通过；缺失短期借款未当作零 |
| 使用互联网结果开展行业分析 | **部分通过** | 真实搜索成功，但未取得外部原文和已核验外部事实 |
| 跨模块综合授信风险分析 | 通过 | 第五章实际关联经营、股权、行业和财务 |
| 正文由 LLM 撰写 | 通过 | 原稿与报告正文一致，无 Python 模板替代 |
| Markdown 正常输出 | 通过 | 六章、股权图、财务表和 15 个问题齐全 |
| 不编造缺失资料 | 基本通过、需人工复核 | 大多数缺口有审慎标记；仍发现若干过度推断 |
| 离线回归 | 通过（本次 Live 前） | 定向 69 passed；全量 263 passed |

## 7. 人工质量核查发现

核心股权比例、财务金额与结构化输入核对一致，但报告仍是 AI 研究初稿，以下表述需要在正式使用前修订：

1. 报告称流动资产增加“主要由货币资金上升贡献”，但货币资金只解释约 29% 的流动资产增量，结论在定量上不成立。
2. 45.32% 的其他股东未展开，不能据此断言“不存在国资背景”或“未发现国有股东参股”；最多只能表述为未发现国有控股证据。
3. “大单品驱动”“呼吸系统地位接近心脑血管”“呼吸品种高毛利”等判断缺少单品收入和分板块毛利数据，应降格为待核假设。
4. “营运资金占用总体收敛”不严谨；已列示科目不足以支撑该整体结论。
5. 仅有 FY2024/FY2025 两期销售收现比，不能写成“长期高于 100%”。
6. “创始人家族”、特定供应商依赖以及政策导致利润波动等部分表述超出现有结构化证据，应以审慎假设表述。
7. 报告末尾把内部限制项 `S05:fetch_http_error` 清理成了孤立的 `:fetch_http_error`，存在轻微展示瑕疵。

本次没有修改 DeepSeek 原稿，以保证 Live 产物和测试轨迹可复现。上述问题应通过人工编辑或后续有针对性的模型修订处理，不应把本次原始产物直接作为正式授信审批材料。

## 8. 最终判断与下一步

本次已经证明 V2.5 是一个真实的 LLM 驱动多 Skill Agent，而不是固定模板报告生成器；双 Skill、Function Calling、确定性数据工具、图表和 LLM 综合写作链路均通过。

但“百度候选 → 成功获取可信原文 → 证据提取 → 行业政策分析”的外部证据链未通过，因此 V2.5 继续保持未完成勾选，状态为 `PARTIAL_SUCCESS`。下一步应先针对检索命中与政府网页读取降级策略做小范围修复，并人工修订上述过度推断；未经再次授权不得重跑付费 Live。

## 9. Live 后离线修复（2026-10-23）

本次原始 Live 目录保持不变，继续作为可复现审计证据。随后完成以下离线代码修复，**没有再次调用 DeepSeek 或百度 API**：

1. 区分局部搜索预算和总 Tool 预算：`search_web`额度用完后仍允许对已有候选执行`fetch_source`与`extract_evidence`；只有总Tool预算耗尽才停止全部工具。
2. 搜索返回可信候选后优先读取，已有候选尚未尝试时不再继续消耗付费搜索请求；单页403/404/412或解析失败时返回剩余候选并继续。
3. HTTP 412明确归类为访问受限，不尝试绕过；基础法律和长期政策允许`recency=null`，空结果时逐项放宽条件，栏目页继续检索具体详情页。
4. 给基本面、股权和财务Tool结果加入确定性的`reporting_constraints`，明确未知股东残差、单品数据缺口、两期数据、流动资产增量归因、营运资金桥接和行业基准的分析边界。
5. Python确定性计算货币资金仅解释本次流动资产增量的28.92%，模型不得再写成“主要贡献”；未覆盖差额不能自行归因。
6. 更新`ownership-structure/SKILL.md`第3.6节：证据ID继续在内部保存，但正式正文不再强制返回ID，也不由Python模板替写。
7. 正式Markdown把模型内部标题机械降级为章内标题；运行时错误码在读者版报告中转换为中文限制，后台轨迹仍保留原始诊断。

离线验收结果：

```text
本轮专项回归：105 passed in 6.79s
全量回归：275 passed in 15.78s
compileall：通过
git diff --check：通过（仅既有CRLF提示）
```

这些测试证明修复后的预算、候选降级、结构化边界和Markdown组装行为符合预期，但不能替代真实模型行为验证。下一次Live必须由用户重新授权，并输出到新的目录；不得覆盖本次原始产物。

## 10. 第二次完整 Live 复测与离线恢复（2026-10-26）

经用户重新授权，仅执行一次新的 DeepSeek + 百度完整 Live 任务，输出目录为
`data/complete_research_live_v25_retest_20261026/CN-A-SZSE-002603/`，没有自动重试或扩大预算。

本次真实链路结果：

- DeepSeek 实际请求 28 次，`finish_reason` 为 27 次 `tool_calls`、1 次 `stop`；Input 704,148、Output 12,006、Total 716,154 Tokens。SDK轨迹不含可核对的DeepSeek账单金额，实际费用以供应商控制台为准。
- Function Tool 共 29 次：`run_ownership_research` 1 次、`run_financial_research` 1 次、`search_web` 8 次、`fetch_source` 12 次、`extract_evidence` 7 次。
- Python Runtime 实际加载 `ownership-structure` 1.3.2 与 `financial-analysis` 2.3.0；两个模块均成功，股权两个展开分支通过100%校验，财务结构化校验通过。
- 百度实际搜索请求 6 次，返回 24 个候选 URL，本地估算搜索费用 0.216 元；成功取得 5 份政府网页原文（DOC001—DOC005），形成 20 条原文精确匹配事实、44 条证据和 24 条未核验候选。另有 5 个国家卫健委/国家药监局页面因访问限制未读取，系统保留失败原因并继续研究。
- DeepSeek 完整输出 6 个章节、18 个尽调问题和 13 条研究限制，正文约 1.29 万字符；此前关于未知股东性质、产品集中度、两期趋势和流动资产增量归因的四项约束均得到遵守。

原 Live 交付状态为 `failed`，但不是模型、Skill、检索或正文失败。唯一阻碍是模型在
`《中医药振兴发展"十五五"规划》` 中输出了未转义的半角双引号，使严格 JSON 解析在
`limitations[10]` 失败。原始草稿完整结束，不是 Token 截断。

为避免再次付费调用，随后仅做离线恢复：解析器增加保守的“JSON字符串内部未转义双引号”修复，只处理可确定的格式错误，截断或其他结构错误仍拒绝；原始模型文字和原 Live 失败产物均不覆盖。恢复版报告为：

- `credit_research_report_recovered.md`：6章、股权图、重点财务表、18个尽调问题，无技术性 Fact/Source ID；
- `credit_research_recovery.json`：记录原始草稿 SHA-256、恢复范围、0次网络请求及“LLM文字未重写”。

新增的3项解析/离线恢复定向测试通过，`compileall`通过。全量回归在当前 Windows 沙箱执行到既有 SDK Runtime 测试时长时间无进展，已人工中止；本次不得把该情况写成全量回归通过。此前离线修复前的275项全量回归记录继续保留。

结论：本次已真实验证双 Skill、外部原文读取、证据提取、跨模块写作和完整正文质量；但原始一次性运行仍因 JSON 转义问题返回失败，恢复版是同一份已保存 DeepSeek 正文的离线机械组装。V2.5 暂不标记为“单次运行无故障完成”。下一阶段应优先减少重复上下文和无效工具轮次，因为 716,154 个总 Token 明显偏高。
