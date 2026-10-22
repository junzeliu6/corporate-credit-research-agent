# V1.3B Fact-Grounded Generation 离线开发报告

> 日期：2026-10-22
> 范围：仅 `ownership-structure`；未调用 DeepSeek、未实现互联网检索、未启动 V1.4/V2。
> 结论：离线实现与全量回归通过，具备下一次受控 Live 复测条件；V1.3B 在 Live 通过前仍为“部分验证”。

## 1. 问题与根因判断

上一次 Live 运行已经完成真实模型连接、Function Calling、Skill 加载、Python 股权工具、比例校验和产物生成，但最终自由文本出现 Tool 事实中未登记的 `5%`，被严格校验器拒绝。

代码审计发现两个可能入口：

- `ownership-structure/SKILL.md` 将 `5%`定义为小机构股东的展示聚合阈值；
- 旧版压缩 Tool 结果曾把缺失信息原因“年报披露的是持股5%以上股东或前10名股东”发送给模型。

运行时只加载并记录 Skill 文件，没有把完整 Skill 正文放入模型上下文；旧 Tool 结果则确实包含后一段文字，因此它是最可能的直接诱因。不过当次未保存完整被拒绝回复，无法确认 `5%` 出现时的原始句子和语境，故不能把这一判断写成已证实根因。

## 2. 最小增量方案

没有替换 V0—V1.3A 数据链，也没有重做股权工具。新增一个只读适配层：

```text
FactRecord / EvidenceRecord / SourceRecord
ownership.json / control relationships / percentage validation
                    ↓
          FactGroundingCatalog
  ┌─────────────────┼──────────────────┐
披露事实        Python计算结果       资料缺口/未核实候选
  └─────────────────┼──────────────────┘
            response_contract
                    ↓
LLM选择Fact ID / Missing ID，并提交绑定依据的分析Claim
                    ↓
Python校验ID、状态、主体、期间、来源和Claim类型
                    ↓
Python渲染具体数值/来源 → 旧严格校验器纵深复核
```

统一事实字段包括：`fact_id`、`company`、`subject`、`metric`、`value`、`unit`、`period`、`as_of_date`、`source_id`、`source_type`、`source_location`、`derived_from`、`evidence_ids`、`provenance` 与 `verification_status`。

披露事实、Python计算、已核验外部事实、未核实候选和冲突事实具有不同状态。当前 ownership 正式说明只允许引用 `verified` 或 `calculated` 事实；Python 计算必须保留 `derived_from`。

## 3. LLM 与 Python 的职责边界

LLM 可以：

- 从 Tool 返回的白名单中选择事实和资料缺口；
- 基于事实撰写支持性判断或风险关注；
- 基于资料缺口生成尽调问题或明确标注的待核实假设。

LLM 不可以：

- 自行填写企业持股比例、财务数值、年份、来源编号或网页链接；
- 把 `5%`、`25%`等 Skill 阈值写成企业事实；
- 引用 `unverified` / `conflicting` 候选形成确定性结论；
- 在没有控制关系 Fact ID 时断言控股股东、实际控制人或一致行动关系；
- 自行添加 Tool 未返回的主体事实或计算结果。

Python 负责：

- 验证必需 Fact/Missing ID、未知ID、核验状态和计算依赖；
- 验证每个分析Claim所需的事实或资料缺口；
- 从所引用事实推导并追加来源标记；
- 渲染具体主体、比例、期间、来源和控制关系；
- 对最终文本再次执行百分比、主体、期间、来源和控制结论校验；
- 在分析文字被拒绝时保留已通过确定性校验的股权图、JSON及验证产物。

## 4. 文件变更

- `src/credit_research_agent/grounding.py`：新增统一可引用事实模型和两个只读适配器。
- `src/credit_research_agent/agent_runtime.py`：响应契约升级至2.0，增加 Fact/Missing ID、分析Claim校验、Python渲染、主体/期间/来源复核及轨迹字段。
- `src/credit_research_agent/__init__.py`、`pyproject.toml`：导出新接口；Runtime来源绑定修复后项目版本更新为1.3.5。
- `tests/test_grounding.py`：验证既有证据模型与股权结果的适配。
- `tests/test_agent_runtime.py`：增加阈值混淆、未核实网页候选、错误主体/年度/来源、Python计算、风险判断、尽调问题和产物保留测试。
- `.agents/skills/ownership-structure/SKILL.md`、`SKILLS_SPEC.md`、`PROJECT_BRIEF.md`、`ROADMAP.md`、`README.md`、`V1_3B_STATUS.md`：同步事实驱动生成边界与验收状态。

未修改 `financial-analysis/SKILL.md`，未注册新的财务或检索 Tool。

## 5. 离线测试结果

定向测试：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_agent_runtime.py tests/test_grounding.py -q `
  --basetemp .pytest-grounding-suite-tmp3
```

结果：`30 passed in 3.97s`。

完整回归：

```powershell
.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-full-grounding-tmp
```

Fact-Grounded增量实现结果：`127 passed in 12.96s`。后续Runtime来源绑定修复增加7项测试，完整回归为`134 passed in 10.42s`。

覆盖内容包括：

1. 正确引用披露的持股比例；
2. 正确引用Python计算值并保留计算依赖；
3. 拒绝模型编造不存在的比例；
4. 区分 `5%`业务阈值和未经证实的企业持股比例；
5. 拒绝真实数值配错年度、主体或来源；
6. 拒绝未核实网页候选形成确定性结论；
7. 接受绑定已核实事实的风险判断；
8. 接受绑定资料缺口的尽调问题；
9. 分析拒绝后仍保留确定性股权产物；
10. V0、V1.1、V1.2、V1.3A及既有V1.3B回归。

Windows受限沙箱会阻止 `asyncio` 创建本地 `socketpair`，因此两组 Agent 测试经授权在沙箱外执行；测试模型仍是 `ScriptedModel`，没有创建 DeepSeek 客户端或发出互联网请求。

## 6. 当前限制

- 统一事实接口第一版只接入 ownership；财务和行业检索仅预留类型，没有实现。
- 为避免数字错配，第一版禁止模型在分析Claim正文中自行写数字、百分比、年份、来源编号和URL；这些内容只能由Python从Fact ID渲染。约束较保守，但可降低金融事实风险。
- 结构化引用能证明“使用了哪些事实”，不能自动证明每个复杂因果判断在专业上必然正确；正式授信报告仍需人工审阅。
- 未核实网页数据即使格式完整也不能成为确定性事实；后续必须经过独立的检索、页面读取、日期/口径核验和来源登记流程。
- 新契约尚未经过真实 DeepSeek 输出验证，不能把Mock通过表述成Live成功。

## 7. 下一步

已按用户授权执行一次来源绑定修复后的受控Live：模型只提交ticker，Runtime绑定S01，Skill、比例校验和10个确定性产物成功；最终A002使用“控股股东”表述但未绑定CR001，被`ANALYSIS-CONTROL-001`拒绝。下一步应先针对该具体Claim绑定失败做最小分析，不自动重试或重构整条链路；只有后续Live最终文本通过，才将V1.3B标记为完成并进入V1.4。
