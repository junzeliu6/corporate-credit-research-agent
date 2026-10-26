# 企业授信研究Agent — V2.5 完整集成开发状态

> 更新时间：2026-10-26
> 当前状态：**第二次完整Live已验证双Skill、外部原文、证据提取和六章写作；保存草稿已离线恢复为完整Markdown**
> Live说明：**5份政府网页形成20条事实和44条证据；原始交付只因一处未转义JSON内部引号失败。解析器已保守修复且恢复过程未再次联网，但仍不把该结果标记为单次运行无故障完成**

## 1. 阶段目标

V2.5把企业基本面、股权结构、财务分析、行业政策研究和完整Markdown交付整合到一个主Agent任务中。首测输入为：

> 请对以岭药业（002603.SZ）进行完整的企业授信研究。

预期由同一个DeepSeek主Agent完成：

1. 读取已核验企业基本面；
2. 调用`ownership-structure` Skill及其确定性Python Tool；
3. 调用`financial-analysis` Skill及其确定性Python Tool；
4. 按需调用V1.5检索Tools开展行业与政策研究；
5. 撰写企业背景与股权、经营、行业政策、财务、综合授信风险和尽调问题六章正文；
6. 由Python插入股权图、财务表格并机械组装Markdown。

V2.5不决定授信额度，不输出具有审批效力的结论，也不开发Streamlit、Word导出或多Agent系统。

## 2. 架构边界

V2.5坚持以下分工：

- **DeepSeek主Agent**：理解完整任务、选择工具、阅读模块结果、开展跨模块分析、识别授信风险、提出尽调问题并撰写全部专业正文。
- **`ownership-structure` Skill**：股权关系、控制关系、逐分支100%比例校验及股权图规则。
- **`financial-analysis` Skill**：财务重点科目、变化原因、合理性、偿债与授信风险分析框架。
- **行业与政策研究**：主Agent独立Instructions加V1.5的`search_web`、`fetch_source`、`extract_evidence`，不是第三个Skill。
- **Python Runtime**：统一主体/来源/期间绑定、PDF和网页获取、结构化数据、确定性计算、图表、缓存、安全与预算、后台来源/轨迹、最小JSON结构解析及Markdown组装。

`SkillRuntime`返回本次实际加载的`SKILL.md` instructions；两个高层Skill Tool只向DeepSeek投影紧凑的`skill_method`，既能证明专业研究方法进入了应用运行上下文，又避免反复发送整份Skill文本。该投影来自实际Loader结果，不在主Agent Instructions中硬编码另一份方法副本。

Python不得把DeepSeek正文替换为固定模板，不得把旧模块Markdown机械拼接后冒充综合研究。模型失败时只能保留结构化数据、图表、来源、草稿和轨迹，并明确报告尚未完成。

## 3. 单主Agent执行流程

```text
用户完整授信任务
  → CompanyIdentity统一上市主体
  → 多Skill计划：ownership-structure + financial-analysis
  → 紧凑企业基本面上下文
  → DeepSeek调用run_ownership_research
  → SkillRegistry / SkillLoader / 股权Python Tool / 比例校验 / 股权图
  → DeepSeek调用run_financial_research
  → SkillRegistry / SkillLoader / 财务Python Tool / 确定性指标
  → DeepSeek按需调用search_web → fetch_source → extract_evidence
  → DeepSeek撰写六章完整报告及针对性尽调问题
  → Python插入股权图和财务表，组装Markdown
  → 后台保存上下文、来源、模型草稿和根执行轨迹
```

两个Skill高层Tool只允许模型提交已解析ticker。`source_id`、官方年报、比较年度、PDF/JSON路径、Skill名称和输出目录由Runtime绑定；模型无权覆盖或请求任意文件、Shell和Python执行。搜索超时、鉴权失败、参数错误等检索Tool异常会转换为结构化失败结果，供模型继续使用其他有效资料生成部分报告，而不是直接打断整个Agent Loop。

## 4. 主体、期间与上下文规则

- 所有模块使用同一`CompanyIdentity`：石家庄以岭药业股份有限公司（`002603.SZ`）。
- 企业基本面以FY2025年报披露为主。
- 股权结构保留实际披露基准日，首测为2025-12-31口径。
- 财务比较保持FY2024/FY2025、人民币、原始披露单位及合并口径；缺失数据不解释为零。
- 不把不同期间或不同主体数据直接混合；跨期信息必须明确标注。
- 给模型的上下文只包含紧凑基本面、股权关系/控制结论、财务快照、必要检索证据和资料缺口，不重复嵌入完整PDF、网页全文或大体量历史JSON。
- 股权图只以报告产物状态参与模型上下文；本地绝对文件路径不发送给模型。顶层来源文件仍保存S01/S10的本地下载与解析路径，并以`runtime_status=parsed`表明确定性数据准备状态，供后台审计而非正文展示。

## 5. 完整报告契约

正式`credit_research_report.md`应包含：

1. 第一章：企业基本情况；
2. 第二章：经营情况分析；
3. 第三章：行业环境与政策分析；
4. 第四章：财务分析；
5. 第五章：综合授信风险分析；
6. 第六章：待补充资料及尽调问题。

第五章必须关联经营、股权/治理、行业政策和财务中的至少两个维度。正文来自DeepSeek，Python仅允许插入：

- 首页“AI生成研究初稿，需人工复核”声明；
- 股权结构SVG/PNG；
- Python确定性财务数据表；
- 运行状态、未完成章节及研究限制；
- 后台研究依据说明。

正式Markdown不显示`[Sxx]`、Fact ID、Metric ID、Evidence ID或`source_handle`。URL、原文定位、结构化数据及技术标识保存在后台产物中。最小解析器遇到单个格式错误章节时只跳过该章并记录缺失，不丢弃已经解析成功的其他模型章节，也不使用Python模板补写。

## 6. 产物契约

| 产物 | 作用 | 当前状态 |
| --- | --- | --- |
| `credit_research_report.md` | 六章完整企业授信研究Markdown | 离线Mock及一次真实Live均已生成；Live六章齐全，状态为`PARTIAL_SUCCESS` |
| `credit_research_context.json` | 主体、基本面、Skill计划、期间及紧凑模块上下文 | 离线Mock及真实Live均已生成并核查 |
| `credit_research_sources.json` | 官方年报、网页URL、获取/失败状态及内部证据 | 真实Live记录2个外部候选、1次HTTP 412及无有效外部Fact |
| `credit_research_execution_trace.json` | Skill计划/加载/Tool、检索、预算、Token、模块状态及产物 | 真实Live记录6次模型请求、10次Tool调用及83,728 Tokens |
| `credit_research_llm_draft.json` | 原始模型完整报告草稿及诊断信息 | 真实Live保存完整DeepSeek原稿；六章与最终报告逐段一致 |
| `credit_research_delivery.json` | 作者、解析、交付状态及缺章信息 | `live_llm`，六个主题全部交付，15个尽调问题，无Python模板回退 |
| `ownership_structure.svg/png` | 复用V1.2比例校验后的股权图 | 真实Live已生成并嵌入报告；两个展开分支均通过100%校验 |
| 财务结构化JSON及表格 | 复用V2.1—V2.3数据和确定性计算 | 真实Live生成46条事实、30个指标和12个比率，校验通过 |

## 7. 状态与容错

- `SUCCESS`：两个Skill完成、六章均交付、本次为已授权的DeepSeek+百度双Live，而且执行轨迹至少存在一次证据提取事件并取得事实或候选信息。只有搜索/抓取动作、没有可用证据时不能标记成功。
- `PARTIAL_SUCCESS`：至少有可交付的模型正文或确定性成果，但某个Skill、来源、检索步骤或章节未完成；保留全部成功模块并列明限制。离线Mock即使结构完整，也不冒充真实Live成功。
- `FAILED`：没有可交付的模型正文，或发生不可恢复的配置/结构错误；仍尽量保留已经生成的数据、图表、来源、草稿和轨迹。

单个网页403/404、超时、鉴权/参数错误或正文不足不得终止两个Skill成果；这些错误以结构化Tool失败保留在轨迹中。总Tool或搜索预算耗尽后停止新增调用，使用已有材料完成部分报告，不自动扩大预算或重复付费任务。

## 8. 离线测试结果

本轮已验证：

1. 完整/综合任务按白名单顺序规划两个Skill，单模块和旧`select()`语义不受影响；
2. 同一根Agent轨迹记录两个Skill的模型选择、Registry确认、Loader加载及实际Python Tool；
3. 所有模块主体和财务期间一致；模型不能覆盖来源或路径；
4. 行业检索仍调用V1.5三个白名单Tools，Mock不产生真实网络请求；
5. 六章模型正文原样进入Markdown，Python不模板重写或机械拼接子报告；
6. 股权图和财务数据表正确插入；正式报告不显示技术ID；
7. 第五章存在模型生成的跨模块综合分析；
8. 单个Skill、网页和章节失败时保留其他成果并返回`PARTIAL_SUCCESS`；
9. 模型输出不可解析时只生成未完成说明，不生成Python专业分析；
10. 上下文不包含完整PDF/网页，重复资料受到长度和统一预算约束；
11. DeepSeek与百度Live缺少任一确认时，在外部调用前停止；
12. V0—V2.4全部旧测试继续通过。
13. 两个Skill Tool返回来自实际`SKILL.md`的紧凑`skill_method`，而不是仅记录Loader事件；
14. 搜索超时、鉴权和参数错误结构化降级，不阻断已有成果；
15. Live只有发生证据提取且得到事实或候选时才可返回`SUCCESS`；
16. 模型上下文不包含股权图本地绝对路径，顶层来源保存S01/S10解析路径和`runtime_status=parsed`；
17. 单个格式错误章节被跳过，其他有效模型章节继续交付；
18. CLI帮助明确`--confirm-live`授权的一次任务可能包含多次模型请求。

实际测试命令与结果：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_skills.py tests/test_skill_runtime.py tests/test_agent_runtime.py tests/test_integrated_agent_runtime.py tests/test_cli_v25.py -q --basetemp=.pytest_tmp_v25_targeted
# 69 passed in 5.62s

.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp_v25_full
# 263 passed in 15.08s
```

此外，`.venv\Scripts\python.exe -m compileall -q src`与`git diff --check`均通过；后者仅提示工作区既有的CRLF行尾转换信息，没有空白错误。

离线Mock产物位于`examples/v2_5_complete_mock/CN-A-SZSE-002603/`。本次执行结果为：

- 状态为`PARTIAL_SUCCESS`：这是Mock刻意采用的状态，避免把编排验证冒充真实DeepSeek+百度Live验收；
- 同一根轨迹规划并执行`ownership-structure`和`financial-analysis`两个Skill；共记录2次Skill调用、9次Tool调用；
- Mock模型请求10次，Mock Token合计1920；检索调用1次、真实外部搜索请求0次，另有3次抓取和3次证据提取；
- 六章模型正文全部交付，股权SVG/PNG和确定性财务表已嵌入Markdown；
- 正式报告未出现Fact、Metric、Source或Evidence等技术ID；
- 上述结果只证明离线编排、数据复用、容错和文件交付，不证明真实模型研究质量。

## 9. Live测试结果与边界

用户已明确授权并于2026-10-23执行一次完整DeepSeek+百度双Live，未自动重试：

- DeepSeek实际请求6次，Input 73,745、Output 9,983、Total 83,728 Tokens；5次以`tool_calls`结束，最后一次以`stop`结束；
- Function Tool共10次；`ownership-structure`与`financial-analysis`均由模型自主调用并由运行时真实加载；
- 百度实际请求6次，得到2个候选URL；唯一原文读取尝试返回HTTP 412，第7次搜索意图被预算守卫阻止；
- 未成功获取外部原文，未形成有效`extract_evidence`结果；因此行业政策外部证据链未完成；
- DeepSeek交付六章正文和15个尽调问题，Python没有生成回退分析或改写模型章节；
- 最终状态为`PARTIAL_SUCCESS`。详细命令、调用轨迹、费用估算、验收矩阵和人工质量核查见`V2_5_LIVE_TEST_STATUS.md`。

后续任何真实运行仍必须同时满足：

- `.env`存在有效`DEEPSEEK_API_KEY`；
- `.env`存在有效`BAIDU_SEARCH_API_KEY`；
- `--llm-mode live --confirm-live`；其中`--confirm-live`确认的是一次受控任务，该任务可能因Agent Loop包含多次模型请求；
- `--search-mode live --confirm-search-live`；
- 明确的模型轮次、总Tool、搜索、抓取、提取、Output Token、超时和重试限制；
- 运行前人工核对脱敏配置、主体、来源、期间和输出目录。

本文件不授权新的真实API调用。本次授权已经消费完毕；失败后不得自动放宽限制或重复执行付费任务。

## 10. 当前已知限制

- 离线正文来自`ScriptedModel`，只能证明编排和交付链路；本次另有一份真实DeepSeek完整正文，但仍存在若干需人工修正的过度推断，不能直接作为正式授信审批材料。
- 按项目已经确认的简化原则，本阶段不恢复逐句Fact ID、百分比白名单或复杂自然语言审查器。Python校验结构化财务数值、股权比例和来源链，但不会对模型每一句推断作自动语义证明。因此即使未来技术状态为`SUCCESS`，报告仍是需人工复核的研究初稿；首次Live必须人工抽查关键金额、比例、控制关系、具体经营事件和因果措辞。
- `skill_method`按上下文预算进行投影和长度限制；完整`SKILL.md`仍保存在本地并由Loader校验，但不会整份重复发送给模型。
- 离线行业资料为Mock夹具，不能作为以岭药业真实行业或政策结论；本次Live虽完成真实搜索，但政府网页读取失败，仍需后续受控任务取得并核验真实原文。
- 当前只输出Markdown；Streamlit界面和Word导出属于V3，不在V2.5范围内。

## 11. 下一步

1. 保留本次原始Live报告、模型草稿、来源和执行轨迹，不覆盖可复现证据；
2. 人工修订“流动资产主要由货币资金贡献”“不存在国资背景”“长期高于100%”等证据不足或定量不成立的表述；
3. 小范围改进搜索命中、候选选择及政府网站HTTP 412后的合规降级策略，不重构两个Skill；
4. 只有用户再次单独授权时，才执行新的DeepSeek+百度Live；不得自动重试；
5. 只有外部原文证据链和报告人工复核均通过后，才把V2.5标记为最终验收完成并进入V3。

## 12. 首次Live后的离线稳定性修复

- 已修正搜索子预算语义：搜索额度耗尽只禁止新增`search_web`，已有候选仍可继续`fetch_source → extract_evidence`；总Tool预算耗尽才停止全部Tool。
- 候选结果现在按一手来源优先并返回`recommended_fetch_url`；未尝试候选存在时阻止新的付费搜索。单页403/404/412或解析失败会返回`remaining_candidates`，不再丢弃其他来源。
- HTTP 412按访问受限处理且不绕过；长期政策允许无时效过滤，空结果逐项放宽条件，栏目页继续查找详情页。
- 基本面、股权和财务快照新增`reporting_constraints`，覆盖未知股东残差、国资结论、单品集中度、两期趋势、流动资产增量、营运资金和行业毛利基准等边界。当前货币资金对流动资产增量贡献率由Python确定性计算为28.92%。
- `ownership-structure` Skill正文协议已与V2.5统一：内部保留证据ID，模型直接撰写正文且无需显示ID，Python不以固定模板替写。
- 正式Markdown机械调整标题层级并把后台错误码转换为中文限制；精确错误码继续保留在执行轨迹。
- 本轮未调用真实API。专项回归`105 passed in 6.79s`，全量回归`275 passed in 15.78s`，`compileall`和`git diff --check`通过。详见`V2_5_LIVE_TEST_STATUS.md`第9节。
