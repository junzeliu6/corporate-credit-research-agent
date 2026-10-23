# V2.1/V2.2 开发状态报告

> 日期：2026-10-23  
> 范围：可比财务事实层、确定性财务 Tools、`financial-analysis` 确定性 Skill Runtime。  
> 明确不含：DeepSeek 财务 Function Calling、全面附注原因抽取、行业研究和完整 V2 报告集成。

## 1. 完成状态

| 项目 | 状态 | 说明 |
|---|---|---|
| S10 2024年报登记/下载/解析 | 已完成并验证 | 巨潮资讯官方PDF，233页均提取成功 |
| S11 2023年报登记/下载/解析 | 已完成并验证 | 巨潮资讯官方PDF，224页均提取成功；留作扩大独立复核 |
| FY2023—FY2025可比事实 | 已完成并验证 | S01/S10形成61条单来源事实、46条归一事实 |
| 来源和页码追踪 | 已完成并验证 | 复用`SourceRecord`、`FactRecord`、`EvidenceRecord`及PDF物理页码 |
| 财务归一与一致性校验 | 已完成并验证 | 主体、期间、币种、单位、口径和跨报告重复值均校验 |
| 同比与比率计算 | 已完成并验证 | `Decimal`确定性计算；30个逐年变动指标、12个基础比率 |
| 重大变动排序 | 已完成并验证 | `abs(yoy)>=20%`，恰好20%触发 |
| 财务Skill Runtime | 已完成并验证 | `ready`；调用`build_financial_artifacts`并校验七个产物 |
| LLM财务Function Tool | 尚未实现 | 属于V2.3，当前主Agent仍只开放股权Function Tool |

## 2. 模块协作

```text
CompanyResolver
  → SourceCatalog（S01/S10）
  → OfficialPdfDownloader / PDF逐页解析
  → financial_seeds.json 精确原文锚点
  → EvidenceBuilder（FactRecord/EvidenceRecord）
  → normalize_financial_items
  → validate_financial_facts
  → calculate_financial_metrics / 比率 / 风险观察
  → build_financial_artifacts
  → SkillRegistry / SkillLoader / SkillRuntime
```

事实层保存年报原始人民币元精度。报告中的千分位和百分比只是展示，不回写事实。重复披露的FY2024数据在S01与S10间一致才归一；不一致则以`cross_report_value_conflict`拒绝继续计算。

## 3. 主要文件

- `src/credit_research_agent/financial.py`：财务Pydantic模型、归一、校验、指标、风险信号、Markdown及产物写入。
- `src/credit_research_agent/data/financial_seeds.json`：经人工回看原文的财务报表行、事实编号和页码锚点。
- `src/credit_research_agent/data/sources.json`：新增S10、S11官方年报来源。
- `src/credit_research_agent/skills.py`、`skill_runtime.py`：财务Skill升级为确定性运行时`ready`并注册白名单Tool。
- `src/credit_research_agent/cli.py`：新增`--build-financial`和财务Skill运行参数分派。
- `.agents/skills/financial-analysis/SKILL.md`：记录V2.2已实现边界、Tool和仍未实现事项。
- `tests/test_financial.py`、`tests/integration/test_yiling_financial.py`、`tests/integration/test_yiling_skill_runtime.py`：边界、真实年报及Skill调用测试。

## 4. 以岭药业真实结果摘要

已核对并输出货币资金、应收账款、预付款项、存货、固定资产、应付账款、流动资产/负债、资产/负债总额、营业收入、营业成本、净利润、销售收现额和经营活动现金流量净额。

FY2025触发20%重点分析阈值的项目包括：

- 净利润：由FY2024负值转为FY2025正值，按统一公式同比274.58%，同时标记为符号切换；
- 经营活动现金流量净额：同比190.75%；
- 预付款项：同比34.21%；
- 货币资金：同比29.53%；
- 应付账款：同比-20.49%；
- 营业收入：同比20.24%；
- 流动资产合计：同比20.10%。

上述内容只是确定性量化观察。当前没有用LLM或无证据文字补造变化原因。

## 5. 数据缺口

- S01/S10合并资产负债表中短期借款行未形成FY2024、FY2025数值事实。空白单元格没有被当作零，已进入待核查清单。
- 尚未全面抽取年报附注中的重大变动原因、受限资金、账龄、借款期限和担保信息。
- S11已完成来源登记和解析，但本轮归一事实仍以S01/S10为主；后续可把S11 FY2023现值加入第二层跨报告复核。
- 风险信号是筛查线索，不构成违约判断或授信结论。

## 6. 测试

定向命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_financial.py tests/test_skills.py tests/test_skill_runtime.py tests/integration/test_yiling_financial.py -q
```

结果：`16 passed`。

完整回归命令：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

结果：`206 passed in 11.48s`。测试无真实DeepSeek或百度请求。

## 7. 运行方式

直接构建财务产物：

```powershell
.venv\Scripts\python.exe -m credit_research_agent.cli 002603.SZ `
  --build-financial S01 `
  --financial-comparison-source S10 `
  --financial-dir data/financial
```

通过确定性Skill Runtime运行：

```powershell
.venv\Scripts\python.exe -m credit_research_agent.cli 002603.SZ `
  --run-skill "分析财务报表、应收账款和经营现金流" `
  --skill-source S01 `
  --financial-comparison-source S10 `
  --financial-dir data/financial
```

示例产物位于`examples/v2_financial/CN-A-SZSE-002603/`。

## 8. 下一步

V2.3应增量实现：

1. 从财务附注和管理层讨论中抽取重大变动的已披露原因，并保留页码；
2. 将披露原因、支持性推断和未核实假设分层；
3. 增加`assess_reasonableness`和财务章节生成；
4. 在保持Fact ID和Python计算约束的前提下，向主Agent开放单个高层财务Function Tool；
5. 通过Mock后再由用户决定是否进行付费Live测试。
