# V2.3 开发状态报告

> **后续状态更新（2026-10-23）：** 本文件保留复杂正文校验时期的开发记录。该交付门禁现已取消，既有 DeepSeek 草稿已离线恢复为 9 章正文和 9 个尽调问题，V2.3 简化后的最终状态见 `V2_3_SIMPLIFICATION_STATUS.md`。

> 日期：2026-10-23  
> 范围：`financial-analysis` 专业流程、DeepSeek财务分析正文、逐段事实校验与Markdown组装。  
> 当前状态：**离线实现及回归已完成；受控DeepSeek Live已生成完整正文，但全部章节被当前校验契约拒绝，V2.3尚未最终验收，V2.4暂不启动。**  
> 网络边界：本次架构调整只执行离线Mock和回归测试，不调用DeepSeek、百度或其他付费API。

## 1. 基线保护

- V2.1/V2.2 已提交为 `2d2fd1d feat: implement v2.1-v2.2 financial analysis foundation`。
- 继续复用现有公司识别、S01/S10年报、PDF解析、`FactRecord`、`EvidenceRecord`、`SourceRecord`、财务归一和确定性计算，不重做V0—V2.2。
- V2.3修改尚未自动提交或推送；V2.4行业与政策研究没有启动。

## 2. 已稳定的数据与工具基础

- `financial_explanation_seeds.json`继续通过人工审阅的原文锚点关联S01年报第52、54、58页。
- `extract_change_explanations`继续区分`disclosed_reason / supported_inference / unverified_hypothesis`。
- `assess_reasonableness`继续保存方向一致性、相反现象、证据缺口和待核查事项；这些结构化结果是DeepSeek的分析材料，不是Python应直接扩写成的正式正文。
- 真实以岭药业样例已有46条归一财务事实、30个逐年变动指标、12个基础比率、3条有证据解释、4条待核实假设和7项合理性评价。
- `run_financial_research`的LLM可见参数仍只有`ticker`；S01/S10、PDF路径和输出目录由Python Runtime绑定，模型不能覆盖来源或任意文件路径。
- Skill调用路径仍为`SkillRegistry → SkillLoader → SkillRuntime → build_financial_artifacts`，不复制第二套财务数据管线。

## 3. 先前实现及其验收缺口

V2.3第一版让模型只返回`metric_id / assessment_id / risk_flag_id`等结构化ID，随后由Python固定模板生成财务分析正文。这条链路验证了Tool Calling、Skill加载和数据计算，但没有证明DeepSeek实际完成财务研究和写作。

一次经用户授权的受控Live测试已验证：

- DeepSeek模型响应2次；
- Function Tool调用1次；
- `financial-analysis` Skill及`build_financial_artifacts`实际执行；
- Input Tokens为7,636，Output Tokens为150，合计7,786；
- 模型最终只返回结构化ID，报告正文仍由Python模板生成。

因此该Live结果只证明真实模型和工具链兼容，**不满足“正式财务报告正文由DeepSeek实际撰写”的最终验收标准**。这一历史结果保留，不再把旧ID-only Mock或Live标记为V2.3完成。

## 4. 当前正式架构

### 4.1 Python职责

- 获取、解析、标准化和保存年报及附注数据；
- 计算同比变化、财务指标和必要的交叉校验；
- 保存Fact/Metric/Explanation/Source/Missing ID、期间、单位及证据；
- 向模型提供受限且足够的分析上下文，避免重复发送完整PDF或大JSON；
- 对模型段落中的关键金额、比例、主体、期间、来源和不支持的事实陈述做轻量校验；
- 机械组装标题、章节状态、DeepSeek原文、未完成提示、数据表、图表和来源附录。

Python不是正式报告的主要作者，**不得把DeepSeek写好的合格段落改写成固定模板，也不得在模型失败时生成模板分析冒充研究成果**。

### 4.2 financial-analysis Skill职责

- 规定银行授信财务分析框架和重点科目；
- 要求按“上期金额 → 本期金额 → 变化金额及比例 → 原因 → 合理性 → 授信关注及尽调问题”展开；
- 指导跨指标比较、披露事实/推断/假设分层，以及重大变化和信息缺口的处理。

### 4.3 DeepSeek职责

- 理解财务事实、指标、年报附注和缺失信息；
- 判断重要变化并形成跨指标分析；
- 区分公司直接披露原因和有依据但仍需核实的推断；
- 识别经营、回款、资金占用和偿债风险，提出针对性尽调问题；
- 实际撰写完整自然语言财务分析，并在段落中引用支持它的Fact/Metric/Explanation/Source/Missing ID。

ID继续承担证据约束与审计作用，但不能替代分析文字。

## 5. 产物与失败策略

Tool阶段先生成并保留：

- `financial_facts.json`
- `financial_metrics.json`
- `financial_explanations.json`
- `financial_reasonableness.json`
- `financial_risk_flags.json`
- `financial_sources.json`
- `financial_validation.json`
- `financial_data_appendix.md`

此时`financial_analysis.md`和`financial_questions.md`只写“未完成”占位，不能包含Python模板分析。模型运行后另保存：

- `financial_llm_draft.json`：模型原始草稿及引用ID；
- `financial_llm_validation.json`：逐段通过/拒绝状态和原因；
- `financial_analysis.md`：通过校验的DeepSeek原始段落、未完成章节标记、数据/来源附录；
- `financial_questions.md`：通过校验的模型尽调问题，缺失部分明确标记未完成。

处理规则：

- 合格段落去除必要的外层空白后原样保留，不进行同义改写；
- 单段存在错误金额、期间、来源或不支持的确定性事实时，只拒绝该段并标记“本节未完成”；
- 其他通过段落、事实、指标、图表、数据附录和来源继续交付；
- 模型整体调用或解析失败时，保留全部确定性产物，但正式报告标记尚未完成；
- 第一版不自动发起付费修复调用。后续如需模型修正，必须在明确预算和用户授权下执行。

## 6. 离线验收结果

本轮Mock与回归测试已经证明：

1. 所有重点科目即使未触发20%阈值也能进入模型分析上下文；
2. 营业收入、应收账款、存货、货币资金、应付账款、经营现金流及短期借款缺失信息均带正确ID和来源；
3. 模型返回的独特自然语言段落未经Python改写直接进入Markdown；
4. 收入—应收—经营现金流等跨指标分析可以通过校验；
5. 编造数字、错误来源、错误期间或把推断写成披露原因的段落只被局部拒绝；
6. 部分成功时保留有效段落并标记未完成章节；
7. 全部段落无效或模型输出错误时只交付数据产物和未完成报告，不回退到模板；
8. Mock不创建DeepSeek网络客户端，旧版本回归继续通过。

实际执行记录：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp ".pytest-tmp-v23-full-final"
```

最终结果：`227 passed in 12.11s`，无失败、无跳过，未发起任何真实DeepSeek或百度请求。此前完整回归发现V1.5搜索缓存临时文件名在Windows长路径下可能失败；经用户同意，将临时文件名前缀缩短为`.search-`后，相关`tests/test_cli_v15.py`为`4 passed in 4.00s`。最终审计又补充了正负号反转、跨科目串值以及“下降/减少”合法表达3项回归测试；财务Runtime定向结果为`16 passed in 4.44s`，以上均已纳入227项最终回归。

另外使用真实缓存S01/S10运行了一次离线ScriptedModel示例，成功生成完整数据上下文、模拟正文、草稿、逐段校验和执行轨迹。示例报告明确标记为“离线Mock、不是DeepSeek正式研究成果”；其哨兵文本原样保留，证明Python未用固定模板替换模型输出。示例产物位于`examples/v2_financial/CN-A-SZSE-002603/`。

## 7. 已知限制与下一步

- 当前原因证据只覆盖已人工核对的年报段落，不等于自动覆盖全部财务附注。
- FY2024/FY2025短期借款主表空白仍不能解释为零，必须作为待核查信息交给模型。
- 轻量校验重点约束企业事实、关键数值、期间和来源，不尝试以Python判断所有专业分析表述的唯一正确性。
- 数值校验对正负号和科目绑定采取保守策略；“分别为……”等无法安全定位到单一科目的复杂句式，或下降词与数字距离过远的表达，可能被标记为未完成，但不会静默放行错误财务事实。
- Mock只能验证运行机制和约束，不能证明DeepSeek真实输出质量。
- 当前已具备受控DeepSeek Live复测条件；只有模型实际生成有分析深度的正文、关键事实和来源通过校验，才将V2.3标记为最终完成。
- V2.4和V2财务以外的新功能在此之前保持暂停。

第三次受控Live经用户单独授权后，从项目根目录实际执行一次且仅一次：

```powershell
.\.venv\Scripts\python.exe -m credit_research_agent.cli 002603.SZ `
  --run-agent "请从银行授信角度分析以岭药业2025年的财务状况，重点分析收入、应收账款、存货、货币资金、短期借款及债务结构、应付账款和经营活动现金流的变化，结合年报附注说明已披露原因，进行跨指标分析，判断潜在风险，并提出尽调问题。" `
  --agent-source S01 `
  --financial-comparison-source S10 `
  --llm-mode live `
  --confirm-live `
  --max-agent-turns 4 `
  --max-tool-calls 3 `
  --max-output-tokens 50000 `
  --request-timeout-seconds 120 `
  --max-model-retries 0 `
  --financial-dir examples/v2_3_financial_live_narrative_50000_timeout120
```

首次正文Live在第二次模型响应恰好达到5000 Output Tokens，并以`finish_reason=length`截断于第九章，未形成闭合JSON。经用户再次明确授权，项目已将可配置上限提高到50000，并通过`227 passed`完整离线回归。

第二次正文Live确认运行时收到`max_output_tokens=50000`；第一轮模型成功选择`run_financial_research`，Skill和Python Tool均执行成功，但第二轮非流式长正文在30秒请求超时后失败，未返回可计量的正文Token，也没有生成可校验草稿。轨迹仅记录首轮`876 Input / 59 Output / 935 Total Tokens`，确定性数据产物全部保留。当前瓶颈已经从5000 Token长度转为`request_timeout_seconds=30`；V2.3仍未通过最终Live验收。未经再次授权，不提高超时、不发起后续测试。

第三次正文Live经用户明确授权，将单次请求超时设为120秒、SDK自动重试设为0，并继续使用50000 Output Token上限。两次模型请求均正常返回，第二次长正文请求约33.19秒，`finish_reason=stop`，候选输出未截断；实际用量为`17,722 Input / 6,063 Output / 23,785 Total Tokens`。模型自主调用`run_financial_research(ticker="002603.SZ")`一次，Runtime绑定S01/S10，加载`financial-analysis`并成功生成确定性财务产物。DeepSeek完整返回9个分析章节和9个尽调问题，但9个章节均被现行逐段校验拒绝；尽调问题全部保留，Python未用模板替换模型正文。拒绝原因以引用契约不匹配为主（例如已声明ID未逐一内联、计算指标要求显式列出全部`derived_from`事实），同时包含少量需要继续审查的推断与多值事实解析问题。该结果证明超时和Token边界已经解除，但正式Markdown正文仍未通过验收；完整诊断位于`examples/v2_3_financial_live_narrative_50000_timeout120/CN-A-SZSE-002603/`，不得自动重试。Live后再次执行完整离线回归，结果为`227 passed in 20.41s`。
