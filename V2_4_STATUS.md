# 企业授信研究Agent — V2.4 行业与政策研究开发状态

> 更新时间：2026-10-23
> 当前状态：**离线实现与验证完成 / Live待用户单独授权**
> Live状态：**尚未执行V2.4 DeepSeek或百度搜索Live，不得视为真实行业研究验收通过**

## 1. 阶段目标

V2.4让DeepSeek根据目标企业主营业务自主制定行业与政策检索方向，调用V1.5已存在的百度搜索、原文获取和证据提取Tools，读取企业基本面及V2.3确定性财务上下文，并实际撰写以下内容：

1. 所属行业、细分赛道、发展阶段及趋势；
2. 竞争格局、主要竞争者和目标企业市场地位；
3. 政策与监管环境、发布时间、状态和适用范围；
4. 行业环境与政策对企业业务和财务表现的可能影响；
5. 行业相关授信风险与尽调问题。

数据不足时允许定性分析，不强制输出市场规模、市场份额或排名。一般行业政策不得直接描述为已经对目标企业造成确定影响；未确认的行业—企业—财务因果关系必须使用“可能”“未核验”“需进一步核实”等措辞。

## 2. 架构边界

V2.4**不是第三个Skill**，不新增或修改`SkillRegistry`中的领域技能。现有两个正式Skill仍是：

- `ownership-structure`
- `financial-analysis`

行业研究使用主Agent独立的轻量Instructions，复用既有基础设施：

```text
用户行业研究任务
  → DeepSeek读取企业基本面与V2.3财务紧凑上下文
  → DeepSeek自主规划行业、竞争和政策检索
  → search_web（V1.5）
  → fetch_source（V1.5，URL安全/缓存/无效正文识别）
  → extract_evidence（V1.5，来源与原文定位）
  → DeepSeek结合企业业务和财务数据撰写自然语言正文
  → Python最小结构解析及Markdown机械组装
  → 后台保存上下文、来源和执行轨迹
```

职责分工：

- **DeepSeek**：检索决策、行业/竞争/政策理解、跨资料分析、企业影响判断、授信风险、尽调问题和正文写作。
- **Python**：企业识别、Tool实现、URL与重定向安全、网络/Token/工具预算、缓存、来源与原文保存、V2.3财务数据加载、最小JSON解析和Markdown组装。
- **V1.5检索Tools**：继续使用`search_web`、`fetch_source`、`extract_evidence`，不再开发第二套搜索API。
- **V2.3财务数据**：继续由Python提供已经核验/计算的金额、同比和比率；DeepSeek不得徒手创造财务数字。

Python不编写固定模板行业正文，也不在模型失败后用模板冒充AI研究成果。只要最小输出结构可以解析且不含后台技术标识，模型正文应保持原样进入Markdown；模型整体失败时保留原始草稿、数据、来源和轨迹，并明确报告未完成。技术标识检查只约束报告格式，不逐句审判行业分析结论。

## 3. 计划/当前代码接口

| 文件或接口 | 作用 | 当前核验状态 |
| --- | --- | --- |
| `src/credit_research_agent/industry_agent_runtime.py` | V2.4 Instructions、紧凑上下文、Agents SDK运行、最小结果解析与报告组装 | 已完成并通过离线定向测试 |
| `src/credit_research_agent/retrieval_runtime.py` | 复用V1.5检索Tools、预算及执行追踪 | V2.4兼容性与全量回归通过 |
| `src/credit_research_agent/web_sources.py` | HTML/PDF获取、无效页面识别、来源/证据保存 | V2.4兼容性与全量回归通过 |
| `src/credit_research_agent/financial_agent_runtime.py` | 提供V2.3紧凑财务上下文 | 已在Mock中复用9项重点财务指标 |
| `src/credit_research_agent/cli.py` | `--run-industry-research`入口与双Live确认 | 已通过离线CLI测试 |
| `src/credit_research_agent/data/industry_search_mock_fixture.json` | 完全离线的行业/政策搜索与原文fixture | 仅用于测试，不是真实以岭药业外部披露 |

本表只记录当前代码位置，不以“文件存在”代替功能验收。最终状态必须以离线测试及产物核查为准。

## 4. 产物契约

核心交付：

| 产物 | 内容 |
| --- | --- |
| `industry_analysis.md` | DeepSeek实际撰写的行业趋势、竞争、政策、企业影响、授信风险与尽调问题；首页统一提示“AI生成研究初稿，需人工复核” |
| `industry_research_context.json` | 发送给模型的企业身份、紧凑基本面、V2.3财务快照和研究边界；不重复嵌入完整网页/PDF，不展示内部技术ID |
| `industry_sources.json` | 搜索候选、已获取来源、原始URL、页面状态、事实/候选/冲突和失败原因 |
| `industry_execution_trace.json` | 模型轮次、Tool调用、参数、预算计数、Token使用、降级原因和产物路径 |

诊断产物可包括`industry_llm_draft.json`与`industry_llm_delivery.json`，用于证明最终正文来自模型、记录最小结构解析和交付状态。完整网页/PDF继续保存在V1.5缓存目录，不反复发送给模型。

正式Markdown默认不展示`[Sxx]`、Fact ID或Metric ID。来源URL、原文和技术轨迹保存在后台独立文件中，用户需要时可另行导出研究依据。

## 5. 状态与降级

- `SUCCESS`：完成必要检索、取得有效原文、交付约定主题和尽调问题，没有需要导致部分完成的运行限制。
- `PARTIAL_SUCCESS`：存在单一网页403/404/超时、候选未核验、来源冲突、部分章节缺失或预算耗尽，但仍有可交付的DeepSeek正文或有效资料。
- `FAILED`：没有可交付的模型正文，或发生无法恢复的配置/结构错误。

单个来源失败不得终止整个研究；已经取得的正文、来源和轨迹应保留。模型整体失败或最小结构解析失败时可以输出“报告未完成”文件，但不能用Python固定模板替代DeepSeek行业分析。

## 6. 离线测试与结果

本轮已验证：

1. DeepSeek Mock路径自主选择`search_web`、`fetch_source`和`extract_evidence`；
2. Mock模式不会创建真实DeepSeek、百度或其他网络请求；
3. 企业基本面与V2.3财务产物被压缩为不含技术ID的上下文；
4. 五类约定行业主题和尽调问题进入最终Markdown，且正文保持模型原文；
5. `industry_analysis.md`、上下文、来源和轨迹四项核心产物正确生成；
6. 政策日期、状态、适用范围和一般政策影响的不确定性约束进入Instructions；
7. 单个来源失败、无效网页或预算耗尽不会丢失其他成果；
8. 缺少财务/基本面上下文时透明降级，不编造企业事实；
9. 两项Live确认缺少任意一项时都不发送真实请求；
10. V2.4 Tool模型视图不包含后台Fact/Source/Candidate等追踪ID，完整ID仍留在后台来源与轨迹；
11. 百度搜索结果日期只保留为来源元数据，不冒充已由网页原文核验的政策事实期间；
12. V2.3财务披露原因、支持性推断和待核实假设分开送入行业分析上下文；
13. Mock运行底座在预检阶段失败时也会生成报告未完成、来源、草稿、交付记录和轨迹；
14. V0—V2.3既有测试继续通过。

实际结果：

- 定向命令：`.venv\Scripts\python.exe -m pytest tests/test_industry_agent_runtime.py tests/test_cli_v24.py -q`
- 定向结果：`14 passed in 4.73s`
- 全量命令：`.venv\Scripts\python.exe -m pytest -q`
- 全量结果：`237 passed in 12.63s`
- 离线Mock生成目录：`examples/v2_4_industry_mock/`
- 网络边界：本轮未调用真实DeepSeek、百度或其他外部API；上述结果不能替代Live验收。

## 7. 建议运行命令

### 7.1 完全离线Mock

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-industry-research "研究以岭药业所属行业的发展趋势、竞争格局和政策监管，并结合公司业务及财务表现分析授信风险，提出尽调问题。" `
  --llm-mode mock `
  --search-mode mock `
  --industry-fundamentals-path "examples/S01_fundamentals_sample.json" `
  --industry-financial-dir "examples/v2_financial/CN-A-SZSE-002603" `
  --industry-dir "examples/v2_4_industry_mock" `
  --industry-trace-output "examples/v2_4_industry_mock/CN-A-SZSE-002603/industry_execution_trace.json"
```

### 7.2 后续受控Live（当前未授权、未执行）

先省略两个确认参数检查脱敏配置、预算和路径；离线验收完成并获得用户单独授权后，才可执行：

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-industry-research "请研究以岭药业所属行业的发展趋势、竞争格局和政策监管，结合公司主营业务与FY2024/FY2025财务表现分析潜在影响、授信风险并提出尽调问题。" `
  --llm-mode live --confirm-live `
  --search-mode live --confirm-search-live `
  --industry-fundamentals-path "examples/S01_fundamentals_sample.json" `
  --industry-financial-dir "examples/v2_financial/CN-A-SZSE-002603" `
  --industry-dir "data/industry_research/live_yiling" `
  --industry-trace-output "data/industry_research/live_yiling/CN-A-SZSE-002603/industry_execution_trace.json" `
  --max-agent-turns 70 --max-tool-calls 60 `
  --max-search-rounds 10 --max-search-calls 10 `
  --max-search-requests 10 --max-search-results 5 `
  --max-source-fetches 20 --max-extract-calls 20 `
  --max-output-tokens 50000 `
  --request-timeout-seconds 120 `
  --max-model-retries 0
```

DeepSeek与百度搜索的确认必须同时存在，任何一项缺少都应在发出外部请求前停止。Token、Tool与搜索预算是本地保护机制，不代表供应商账单的绝对硬上限；失败后不得自动放宽或重复付费调用。

## 8. 已知限制与后续边界

- 当前行业检索质量取决于百度结果、目标网页可访问性和正文提取质量；验证码、登录墙、反爬和付费墙不会被绕过。
- 公开资料可能不足以确认市场规模、份额、排名、单项政策适用性及企业具体受影响程度；这类内容应省略数字或标记未核验。
- 当前由最大Agent轮次、总Tool次数和单次响应Output Token限制控制模型开销，执行轨迹会累计实际Token；尚未提供跨全部模型请求的累计Token硬中断，因此本地限制不能视为供应商账单绝对上限。
- V2.4仅把既有V2.3财务快照作为分析上下文，不重新计算财务指标，也不开发新的财务Skill逻辑。
- V2.4不开发多Agent、复杂RAG、MCP、Streamlit或第三个Skill。
- 第一次真实以岭药业行业报告仍需用户单独授权的DeepSeek+百度双Live验收；通过前本阶段保持“离线实现与验证完成、Live待授权”，不标记最终验收完成。
- V2.5多研究模块集成尚未启动。
