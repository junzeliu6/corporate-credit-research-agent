# 企业授信研究Agent

企业授信研究Agent（Corporate Credit Research Agent）是一个面向银行对公场景的学习型项目：从上市公司公开披露出发，逐步构建可追溯的企业研究辅助 Agent。

当前代码版本是 **V2.3：DeepSeek 财务分析正文架构调整**；需求基线为 **v5**。V2.1使用以岭药业官方2025/2024年报建立FY2023—FY2025合并口径财务事实，V2.2由Python计算同比、比率和重大变动；V2.3受控Live已取得DeepSeek生成的完整9章财务分析和9项尽调问题，当前按简化后的正式报告规则离线恢复该正文：DeepSeek负责分析和写作，Python负责数据、计算、内部溯源、最小结构解析和Markdown组装，不再以技术ID或复杂自然语言校验作为交付门禁。V2.4暂不启动。本项目不是授信审批系统，不给出授信额度、贷款定价或具有审批效力的结论。

## V0.1—V0.4 已实现

- 使用 `src` 布局建立最小 Python 包，配置 Pydantic 与 pytest。
- 将`以岭药业`、`石家庄以岭药业股份有限公司`、`002603`和`002603.SZ`解析为同一个上市主体。
- 默认研究口径明确为上市公司合并报表口径（`consolidated`）。
- 用 JSON 登记企业与来源，便于后续扩展且可离线复现。
- 登记以岭药业 2025 年年度报告、巨潮资讯、深交所和公司官网。
- 用 `registered` 状态区分静态来源种子与实际下载结果。
- 对登记的官方年度报告执行 HTTPS/官方域名白名单校验、超时、重试和重定向复核。
- 流式下载并限制文件大小；验证 `%PDF-` 文件头，避免把访问限制页面当成年报。
- 生成 SHA-256 和独立元数据清单，记录 URL、标题、披露日期、获取时间、HTTP 状态和内容类型。
- 缓存命中前重新计算哈希；缓存损坏时自动重新下载。
- 下载失败时记录结构化错误，不把失败伪装成成功。
- 解析前复核下载清单中的 SHA-256，拒绝损坏或被替换的 PDF。
- 使用 pdfplumber 逐页提取文本，保留 1-based PDF 物理页码及逐页状态。
- 分别输出完整 `pages.jsonl`、解析质量摘要和轻量章节定位样例。
- 单页失败不丢弃整份文档；整体失败保留结构化错误记录。
- 对第 6、106 页进行视觉抽查，确认提取页码与原始页面一致。
- 建立 `CompanyIdentity`、`FactRecord`、`EvidenceRecord`、`SourceRecord` 和 `EvidenceBundle` 的 Pydantic 模型。
- 用 `Fxxx / Exxx / Sxx` 分别表示事实、证据片段和来源，验证所有交叉引用不存在悬空编号。
- 为 `pages.jsonl` 增加 SHA-256，事实构建前同时校验原 PDF 和逐页文本产物。
- 对 S01 第 6 页进行精确文本匹配，生成 4 条主体身份事实和 3 个原文证据片段。
- 自动渲染 `[S01]` Markdown 来源引用，包含标题、发布者、披露日期、访问日期、URL 和页码。
- 提供 CLI 和 41 项离线单元测试。

## V1.1 已实现

- 新增 `ResearchModule`，将披露事实分为公司身份、发展历史、主营业务、经营模式和主要产品。
- 新增 `FundamentalResearchBundle`、`ModelInference` 与 `MissingInformation`，从数据结构上隔离“年报披露事实 / 模型推断 / 缺失信息”。
- 新增人工审阅的 `fundamentals_seeds.json`，通过精确原文锚点提取基本面事实，不允许证据缺失时继续生成。
- 复用 V0 的公司识别、S01 来源、PDF缓存、逐页文本、双哈希校验、`FactRecord`、`EvidenceRecord` 和 `[S01]` 引用。
- 新增 `FundamentalsBuilder`、JSON/引用产物以及 `--build-fundamentals` CLI 入口。
- 对真实 S01 年报生成 21 条披露事实和 19 条证据，其中包含4条身份事实、3条发展历史、2条主营业务、8条经营模式和4条主要产品事实。
- `inferences` 当前为空，因为 V1.1 没有调用 LLM；另记录3条当前证据无法确认的信息。
- 增加离线单元测试、CLI测试和本机真实年报集成测试；全量回归49项通过。

## V1.2 已实现

- 新增 `OwnershipDataset`，分别建模股权节点、持股/表决权边、控制关系、国资控制判断、基准日、核验状态及来源证据。
- 原始股权事实与展示图分离：聚合和最多5层裁剪不会改写原始披露数据。
- 实现 `validate_ownership_facts`、`validate_ownership_percentages`、`aggregate_ownership_holders`、`build_ownership_graph` 和 `render_ownership_graph`。
- 展示规则覆盖自然人严格大于25%单列、机构严格小于5%可聚合；控股股东、实际控制人、最终控制人和一致行动人无条件单列。
- 对每个被展开主体按同一时点、同一股本口径执行100%完整性校验，默认容差±0.01个百分点；超过100%报错，不足100%且无证据声明时禁止正式出图。
- 仅在同口径公开证据支持时生成“其他未单列股东”差额计算节点；该节点只进入展示层，原始股东事实不被改写。
- 校验来源/证据悬空、重复节点/关系、日期冲突、冲突比例、环路和第6层截断。
- 国资参股与国资控制分开记录；程序不会由持股比例或国资平台股东自动推断实际控制人。
- 对真实 S01 年报保留5个原始节点、4条持股边、1条表决权边、4条控制关系、16条证据和2条缺失信息；正式展示图另含1个45.32%的差额计算节点。
- 图中节点只展示名称和名称下方的比例；股权箭头从下方股东指向上方被持股企业。控制关系、来源和核验状态只保留在图外 JSON/说明中。
- 输出 `ownership.json`、展示图 JSON、DOT、高清 SVG/PNG、比例校验 JSON、文字说明、来源和待核查清单；本机无 Graphviz 时明确使用分支感知的内部树状 SVG/PNG 降级引擎。
- V1.2 未调用 LLM，不产生模型 API 费用。

## V1.3A 已实现

- 新增白名单 `SkillRegistry`，只登记项目内批准的 `ownership-structure` 与 `financial-analysis`，记录名称、描述、相对路径、版本、状态、Tool 名称和 SHA-256。
- 新增确定性 `SkillSelector`；根据任务触发词选择一个 Skill，未知任务和多 Skill 歧义任务均显式失败，不让模型绕过白名单。
- 新增只读 `SkillLoader`；加载前复核路径仍在 `.agents/skills/` 下，并验证注册时哈希，文件被替换时拒绝执行。
- 新增 `SkillRuntime` 和 `SkillExecutionTrace`，按“主体 → 来源 → 选择 → 加载 → Tool → 结构校验 → 输出”的顺序记录事件。
- 运行时实际调用 V1.2 唯一实现 `build_ownership_artifacts`，并检查10个声明制品确实落盘；没有复制下载、PDF解析、来源或绘图逻辑。
- CLI 新增 `--run-skill TASK --skill-source S01`；任务和官方来源必须显式给出，失败也会保存机器可读追踪。
- `financial-analysis` 在V2.2已升级为确定性运行时`ready`；选择后只读加载Skill并调用白名单`build_financial_artifacts`，不会让LLM徒手计算财务数字。
- V1.3A 没有调用 LLM API，因此无模型费用；其受控入口已在 V1.3B 直接封装为 SDK Function Tool。

## V1.3B 已实现（真实 API 部分验证）

- 新增独立 DeepSeek Provider 配置，固定默认 Base URL `https://api.deepseek.com`、模型 `deepseek-flash`、密钥变量 `DEEPSEEK_API_KEY`；`.env` 被 Git 忽略，`.env.example` 不含真实密钥。
- 通过 OpenAI Agents SDK 的 `Agent`、`Runner`、`AsyncOpenAI` 与 `OpenAIChatCompletionsModel` 建立单 Agent Loop；首版用 `extra_body={"thinking":{"type":"disabled"}}` 关闭思考模式。
- 默认 `mock` 使用 SDK `ScriptedModel`，不会创建网络客户端：第一轮模拟模型选择股权 Tool，Tool 返回后第二轮选择Tool提供的Fact ID、Missing ID，并生成逐条绑定支持ID的分析Claim；具体事实与来源由Python渲染。
- `live` 必须显式选择、检测到密钥，并再加 `--confirm-live`；发送前在 stderr 输出脱敏 Provider、模型、thinking、最大轮次、Tool 次数、Token 和重试限制，用户可不确认而取消。
- 唯一 Function Tool `run_ownership_research` 的LLM可填参数只有证券代码；`source_id`由Python Runtime从已验证上下文和`SourceCatalog`绑定，并复核主体及下载产物一致性。原始参数守卫拒绝模型提供`source_id`或其他覆盖字段；内部继续复用 V1.3A `SkillRuntime` 和 V1.2 `build_ownership_artifacts`。
- Tool 只向模型返回压缩后的已核实关系、控制关系、比例校验、来源、缺失信息与产物路径，并由Python构造`response_contract`；新增只读`FactGroundingCatalog`复用现有Fact/Evidence/Source与股权ID，企业披露事实、Python计算、未核实候选、资料缺口、完整性规则和5%/25%展示阈值分开标记，不重复发送完整 PDF 或股权大 JSON。
- 模型最终返回Fact ID、Missing ID和有依据的分析Claim；Python拒绝未知、遗漏、未核实或主体/期间/来源不匹配的引用，再确定性渲染名称、比例、日期、来源和控制结论。风险判断必须绑定事实，尽调问题必须绑定资料缺口，内部阈值不能冒充企业实际持股事实。
- `AgentExecutionTrace` 区分模型调用、模型选择 Tool、Registry/Loader/Skill Runtime、Python Tool、比例校验与产物；记录 Token、Tool参数、返回/选择Fact ID与Missing ID、分析Claim ID、确定性产物保留状态、脱敏候选响应、可获得的`finish_reason`、拒绝句及规则编号，费用金额以 DeepSeek 控制台账单为准。
- 领域数据模型暂通过 Pydantic 2 提供的 `pydantic.v1` 兼容命名空间保持 V0—V1.3A 语义；V1.3B 配置与 Tool 输入使用 Pydantic 2。
- 已完成离线 Mock、错误参数、Runtime来源绑定/覆盖拒绝、轮次/Tool 限制、密钥安全、无网络、Fact ID/证据关联、CLI 与真实 S01 缓存集成测试。最新Live已证明模型只提交`ticker`、Runtime绑定`S01`并完成完整Tool链路；最终A002分析Claim提到控股股东但未绑定控制关系Fact ID，被严格校验器拒绝，因此Live状态仍是“部分验证”。

以上Fact ID/Claim契约是V1.3B ownership场景的既有内部兼容实现，不再作为后续正式研究报告正文的统一输出规范。正式报告默认不展示技术ID，内部来源追溯继续保留。

## V1.4 已实现

- 新增 `reporting.py`，读取并重新校验 `FundamentalResearchBundle`、`OwnershipDataset`、`OwnershipGraph` 和逐分支比例校验结果，不复制事实抽取、股权分析或绘图逻辑。
- 历史MVP使用 Python 确定性生成企业基本信息、发展历史、主营业务与经营模式、主要产品、股权图、主要股东与控制关系、待核查事项、来源附录八个章节。
- 正文事实通过 Evidence 反查来源并生成 `[Sxx]`；附录合并来源元数据、PDF页码、章节定位和原文 URL。
- 将已通过校验的 SVG/PNG 复制到报告 `assets/`，使用相对路径嵌入 Markdown；非 `formal_ready` 图降级为明确说明，不伪装成正式图。
- 历史实现曾以Agent Runtime校验和`[Sxx]`作为正文门禁；该门禁不再用于新的正式报告。后续正式报告由DeepSeek撰写，Python只组装数据、图表和模型原文，模型失败时明确标记未完成。
- CLI 新增 `--build-report S01`；以岭药业真实缓存端到端生成21条基本面事实、5项缺失信息和S01来源附录，全量离线回归为145项。

以上是V1.4历史MVP能力。自V2.3报告作者职责调整起，Python确定性框架继续负责内部来源记录、图表、数据表和文件组装，但不能再用模板分析替代DeepSeek正式正文；正式Markdown默认不展示技术ID，用户需要时可另行导出研究依据文件。模型失败时正式分析报告必须标记未完成。

## V1.5 已实现（最终稳定性修复已离线验证，Live待复测）

- 新增通用`SearchProvider`契约和百度官方Web Search Provider，把内部`query/max_results/domain_filter/recency`映射到百度官方请求字段，并将`references`转换为统一搜索结果。
- 新增`search_web`、`fetch_source`和`extract_evidence`三个 Agents SDK 白名单Tool。模型可决定搜索词和选择来源，不能填写`source_id`、执行Shell或读取任意路径。
- HTML保留可定位段落，PDF复用V0解析器并保留物理页码；下载缓存、超时、大小、重定向和内网/本机/云元数据地址由Python限制。
- 运行时从已搜索URL确定性生成`source_handle`和`Sxx`，复用`SourceRecord`、`EvidenceRecord`、`FactRecord`和既有来源目录。`verified / single_source / unverified / unsupported`等细粒度状态可继续用于内部诊断，正式报告只显示“已核验/未核验”。
- 官方可靠披露和Python确定性计算属于“已核验”；单一来源、搜索候选、冲突信息或分析假设属于“未核验”，应使用“可能/需进一步核实”等措辞；完全无来源内容不得作为企业事实输出。疑似Prompt Injection继续隔离。
- 单个网页403/404、超时或正文不足会记录失败来源和未核验Candidate，并允许Agent继续读取其他来源。存在失败来源、单一来源、候选或冲突时，运行结果标记为`partial_success`而不是整体失败。
- 中文段落召回使用研究主题及同义表达（如核心业务、产品群、营业收入、营销模式和学术推广）；验证码、登录墙、Access Denied与反爬占位页不会登记为有效正文。
- 单任务默认最多10轮检索、10次`search_web`、10次真实搜索API请求、5条/次、20次抓取、20次证据提取、60次总Function Tool调用和70个Agent轮次。所有限制由Python计数；第10轮允许、第11轮拒绝。预算耗尽后停止新增工具并保留已有Fact/Candidate、检索记录和可信度状态；这些确定性资料不是正式分析正文，正式文字仍需由DeepSeek生成或明确标记未完成。
- 完整网页保存在本地；Tool只返回受限摘要、相关段落和必要来源信息，重复查询与网页复用缓存。内部ID可用于运行时关联，但不要求DeepSeek在正式正文中返回或展示。
- 默认LLM和搜索都是Mock；两者Live必须分别选择、配置密钥和显式确认。日常测试不会发送真实百度或DeepSeek请求；已获用户逐次授权的Live验收另行记录。

## V2.1—V2.3 已实现

- 新增S10（2024年报）和S11（2023年报）官方来源登记；真实下载和逐页解析均已验证。当前事实层以S01/S10形成FY2023—FY2025序列，S11用于后续扩大独立复核。
- 新增`financial.py`和人工审阅的`financial_seeds.json`，从合并资产负债表、利润表和现金流量表按精确原文及物理页码形成61条单来源事实、46条归一可比事实。
- 跨报告重复值、主体、期间、币种、单位和合并口径不一致会明确失败；短期借款空白没有被解释为零，而是保留为待核查事项。
- Python统一计算30个逐年变动指标和12个基础比率；`|同比|>=20%`（含20%）触发重点分析，并单独处理零基期、负值、正负切换和跨报告冲突。
- `financial-analysis`已注册高层白名单Tool并通过真实Skill Runtime调用；Python输出财务事实、指标、原因证据、风险观察、来源、问题清单和校验结果，Tool阶段的分析/问题Markdown只是未完成占位。
- `financial_explanation_seeds.json`对S01第52、54、58页进行精确原文匹配；经营现金流变动形成直接披露原因，营业收入和净利润形成明确标注的支持性推断，其余重点科目保留待核实假设。
- `extract_change_explanations`和`assess_reasonableness`形成可追溯的分析上下文，并额外输出`financial_explanations.json`与`financial_reasonableness.json`；旧的Python模板分析只作为历史实现记录，不再构成正式报告正文。
- 主Agent可调用只接受`ticker`的`run_financial_research`；S01/S10、PDF路径和输出目录由Python Runtime绑定。DeepSeek获得重点财务数据、指标、必要附注、缺失信息和来源概况后，直接生成完整自然语言分析与尽调问题，不再要求返回Fact/Metric/Source ID。
- Python解析最小输出结构，把DeepSeek原始章节、尽调问题和确定性财务数据表组装进`financial_analysis.md`。它不使用百分比白名单、正则/关键词或逐句来源匹配拒绝正文，也不得用固定模板重写模型文字。Tool阶段继续生成数据产物和内部来源记录，`financial_llm_draft.json`保留原始模型输出供诊断。
- DeepSeek调用失败时保留事实、指标、图表、数据附录和来源，并明确正式分析尚未完成；Mock只验证流程，不是正式报告，也不等同于Live验收。
- 运行命令：`.venv\Scripts\python.exe -m credit_research_agent.cli 002603.SZ --build-financial S01 --financial-comparison-source S10 --financial-dir data/financial`。
- Mock Agent命令：`.venv\Scripts\python.exe -m credit_research_agent.cli 002603.SZ --run-agent "分析财务报表、现金流和重大同比变动" --agent-source S01 --financial-comparison-source S10 --llm-mode mock --financial-dir data/financial`。
- 示例产物：`examples/v2_financial/CN-A-SZSE-002603/`。最新受控Live已验证模型调用Tool并完整撰写9章正文和9项尽调问题；120秒超时和0次自动重试配置有效。本轮直接使用已保存草稿离线恢复最终报告，不重新调用DeepSeek。

当前仍未实现全部附注原因的自动覆盖、完整审计意见分析、V2.4多来源行业政策研究、V2.5同一任务双Skill编排、RAG、MCP或Streamlit。当前只收尾V2.3正文恢复与报告组装，不提前启动V2.4。

## v5 文档与 Skill 规范

- `SKILLS_SPEC.md`：两个 Skill 的跨模块接口、运行时集成约束和验收矩阵。
- `.agents/skills/ownership-structure/SKILL.md`：股权穿透、聚合、控制关系核验与绘图业务规则。
- `.agents/skills/financial-analysis/SKILL.md`：重点财务科目、20%变化触发、原因合理性和尽调问题规则。
- `V0_AUDIT.md`：本地 V0 代码、真实运行产物、接口复用和缺口核查。

V1.3 继续复用 V0/V1.1/V1.2 的主体、来源、下载、PDF、证据与股权 Tools，没有复制第二套数据管线。Codex 能读取 `.agents/skills/` 与项目运行时是否加载 Skill 是两件事；现在由 `SkillRegistry`、`SkillSelector`、`SkillLoader` 和 `SkillExecutionTrace` 显式证明运行时行为。

## 项目结构

```text
corporate-credit-research-agent/
├─ .agents/skills/
│  ├─ ownership-structure/SKILL.md
│  └─ financial-analysis/SKILL.md
├─ src/credit_research_agent/
│  ├─ data/
│  │  ├─ companies.json      # 受控企业名称/代码登记表
│  │  ├─ evidence_seeds.json # 经审阅的最小事实与原文锚点
│  │  ├─ fundamentals_seeds.json # V1.1 企业基本面原文锚点/缺失项
│  │  ├─ ownership_seeds.json # V1.2 真实股权节点/关系与原文锚点
│  │  └─ sources.json        # 官方来源与年报种子目录
│  ├─ company.py             # 企业精确识别与显式错误处理
│  ├─ models.py              # CompanyIdentity / SourceRecord
│  ├─ sources.py             # 只读来源目录
│  ├─ documents.py           # 下载结果与 V0.3 解析接口契约
│  ├─ downloader.py          # 受控下载、缓存、哈希与失败记录
│  ├─ evidence.py            # 事实/证据构建、交叉校验和引用渲染
│  ├─ fundamentals.py        # V1.1 基本面构建与产物接口
│  ├─ ownership.py           # V1.2 股权/比例校验、聚合、建图和SVG/PNG渲染
│  ├─ skills.py              # V1.3 Skill白名单、任务选择和只读加载
│  ├─ skill_runtime.py       # V1.3A确定性Tool Calling与执行追踪
│  ├─ llm_config.py          # V1.3B DeepSeek配置、密钥与SDK模型设置
│  ├─ agent_runtime.py       # V1.3B主Agent、Function Tool、限制和执行追踪
│  ├─ reporting.py           # V1.4历史Markdown框架；新正式报告只组装数据、图表与LLM正文
│  ├─ grounding.py           # 内部事实/来源适配；不作为正式报告正文ID门禁
│  ├─ search.py              # V1.5百度SearchProvider、缓存、预算与错误映射
│  ├─ web_sources.py         # V1.5安全HTML/PDF获取、运行时来源与证据登记
│  ├─ retrieval_runtime.py   # V1.5 Agent检索Tools、事实选择和执行轨迹
│  ├─ pdf_parser.py          # 逐页提取、质量摘要和章节定位
│  └─ cli.py                 # 企业识别、来源处理和Skill运行入口
├─ examples/
│  ├─ S01_extraction_sample.json  # 可复现的轻量页码定位样例
│  ├─ S01_evidence_sample.json    # V0.4 事实证据 JSON
│  ├─ S01_citations.md            # V0.4 [Sxx] 来源引用样例
│  ├─ S01_fundamentals_sample.json
│  ├─ S01_fundamentals_citations.md
│  ├─ ownership.json             # 未经聚合/裁剪的真实股权数据
│  ├─ ownership_graph.json       # 含展示聚合/计算节点的图数据
│  ├─ ownership_structure.dot
│  ├─ ownership_structure.svg
│  ├─ ownership_structure.png
│  ├─ ownership_validation.json
│  ├─ ownership_summary.md
│  ├─ ownership_sources.json
│  ├─ ownership_questions.md
│  ├─ ownership_citations.md
│  ├─ V1.3_skill_execution_trace.json # 真实S01 Skill执行追踪
│  ├─ v1_3_runtime/          # V1.3真实Tool调用的独立制品
│  ├─ v1_4_report/           # V1.4历史MVP Markdown样例（不代表V2.3正式LLM报告）
│  └─ v1_5_mock/             # V1.5离线检索轨迹与证据链样例
├─ tests/
│  ├─ integration/test_yiling_fundamentals.py
│  ├─ integration/test_yiling_ownership.py
│  ├─ integration/test_yiling_skill_runtime.py
│  ├─ integration/test_yiling_agent_runtime.py
│  ├─ integration/test_yiling_report.py
│  ├─ test_company.py
│  ├─ test_sources.py
│  ├─ test_downloader.py
│  ├─ test_pdf_parser.py
│  ├─ test_fundamentals.py
│  ├─ test_ownership.py
│  ├─ test_skills.py
│  ├─ test_skill_runtime.py
│  ├─ test_llm_config.py
│  ├─ test_agent_runtime.py
│  ├─ test_reporting.py
│  ├─ test_search.py
│  ├─ test_web_sources.py
│  ├─ test_retrieval_runtime.py
│  ├─ test_cli_v15.py
│  └─ test_cli.py
├─ .env.example
├─ pyproject.toml
├─ SKILLS_SPEC.md
├─ V0_AUDIT.md
├─ V1_4_STATUS.md
├─ V1_5_STATUS.md
└─ ROADMAP.md
```

## 环境与安装

要求 Python 3.10 或更高版本。以下命令以 PowerShell 为例：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install ".[dev]"
```

项目位于包含中文字符的 Windows 路径时，建议使用上述普通安装。部分旧版 setuptools 的 editable `.pth` 文件可能按错误代码页读取中文路径。

运行全部离线测试：

```powershell
python -m pytest -q
```

## 使用示例

安装后可通过名称或代码识别企业：

```powershell
credit-research-agent "以岭药业"
credit-research-agent "002603.SZ" --sources
```

也可以使用模块方式：

```powershell
python -m credit_research_agent "002603" --sources
```

下载 S01 到默认的 `data/cache` 目录：

```powershell
python -m credit_research_agent "以岭药业" --download-source S01
```

强制忽略有效缓存并重新下载：

```powershell
python -m credit_research_agent "002603.SZ" --download-source S01 --force-download
```

下载或复用缓存，并提取逐页文本：

```powershell
python -m credit_research_agent "以岭药业" --parse-source S01 `
  --sample-output "examples/S01_extraction_sample.json"
```

解析产物：

- `data/parsed/S01/pages.jsonl`：每行一页，包含物理页码、文本、字符数和状态。
- `data/parsed/S01/summary.json`：页数、文本覆盖率、空页、失败页、解析器版本和源文件哈希。
- `examples/S01_extraction_sample.json`：小型、可提交 Git 的章节页码定位样例。

`data/parsed/` 是可重建的大体量派生数据，因此被 Git 忽略。

执行完整 V0 流程并生成事实证据样例：

```powershell
python -m credit_research_agent "002603.SZ" --build-evidence S01 `
  --evidence-output "examples/S01_evidence_sample.json" `
  --citations-output "examples/S01_citations.md"
```

`--build-evidence` 会按顺序执行或复用下载、逐页解析、双重哈希校验、精确原文匹配、Pydantic 模式校验和引用渲染。任何一步失败都会输出机器可读错误，不生成已验证事实。

构建 V1.1 企业基本面样例：

```powershell
python -m credit_research_agent "002603.SZ" --build-fundamentals S01 `
  --fundamentals-output "examples/S01_fundamentals_sample.json" `
  --citations-output "examples/S01_fundamentals_citations.md"
```

`facts` 只包含通过精确原文匹配的披露事实；`inferences` 专门存放未来模型推断；`missing_information` 记录当前证据无法确认的事项。三类信息不能互相替代。

构建 V1.2 股权结构样例：

```powershell
python -m credit_research_agent "002603.SZ" --build-ownership S01 `
  --ownership-dataset-output "examples/ownership.json" `
  --ownership-graph-output "examples/ownership_graph.json" `
  --ownership-svg-output "examples/ownership_structure.svg" `
  --ownership-summary-output "examples/ownership_summary.md" `
  --ownership-validation-output "examples/ownership_validation.json" `
  --ownership-sources-output "examples/ownership_sources.json" `
  --ownership-questions-output "examples/ownership_questions.md" `
  --citations-output "examples/ownership_citations.md"
```

`ownership.json` 保存未经聚合和裁剪的披露事实；`ownership_graph.json` 保存按展示规则生成的节点/边。SVG 同目录会生成同名 PNG 和 DOT。若系统安装了 Graphviz 且 `dot` 在 `PATH` 中，`render_engine` 为 `graphviz-dot`；否则输出 `internal-tree-fallback`，不会把降级渲染误报为 Graphviz。

生成 V1.4 历史MVP Markdown报告（默认不调用LLM，按当前政策不视为正式AI研究报告）：

```powershell
python -m credit_research_agent "002603.SZ" `
  --build-report S01 `
  --fundamentals-dir "examples/v1_4_report/research/fundamentals" `
  --ownership-dir "examples/v1_4_report/research/ownership" `
  --report-dir "examples/v1_4_report" `
  --report-output "examples/v1_4_report/CN-A-SZSE-002603/credit_research_report.md"
```

该入口复用下载缓存和逐页解析，重新执行V1.1/V1.2的确定性构建器并组装框架、图表和内部来源。它用于复现V1.4历史成果；V2.3以后如果没有成功生成且完成最小结构解析的DeepSeek正文，只能作为数据/图表/来源产物并标记分析未完成，不能把旧模板报告作为正式研究成果。

运行 V1.5 完全离线 Mock 检索（不需要任何 API Key，不联网）：

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-web-research "请搜索以岭药业的官方网站及其他可信公开资料，补充其主营业务、主要产品和经营模式信息，并提供来源。" `
  --llm-mode mock `
  --search-mode mock `
  --web-research-dir "data/web_research"
```

Mock fixture 仅验证`search_web → fetch_source → extract_evidence → 内部来源登记 → 分析输出`链路，所有内容都明确标注为测试数据，不是以岭药业的真实外部披露。既有Fact ID只用于内部兼容关联，不要求在正式报告正文中展示。

配置真实百度搜索时，在已被 Git 忽略的`.env`中填写：

```dotenv
SEARCH_PROVIDER=baidu
BAIDU_SEARCH_API_KEY=your_baidu_search_api_key_here
BAIDU_SEARCH_BASE_URL=https://qianfan.baidubce.com/v2/ai_search/web_search
BAIDU_SEARCH_AUTH_HEADER=Authorization
```

先省略`--confirm-search-live`检查脱敏配置并确认程序停止。首次受控 Live 验收同时使用 DeepSeek 和百度搜索，因此需两个独立确认参数：

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-web-research "请搜索以岭药业的官方网站及其他可信公开资料，补充其主营业务、主要产品和经营模式信息，并提供来源。" `
  --llm-mode live --confirm-live `
  --search-mode live --confirm-search-live `
  --max-agent-turns 70 --max-tool-calls 60 `
  --max-search-rounds 10 --max-search-calls 10 `
  --max-search-requests 10 --max-search-results 5 `
  --max-source-fetches 20 --max-extract-calls 20
```

官方页面的接口定义与curl示例对鉴权头的展示不一致：程序默认按接口定义使用`Authorization: Bearer ...`；只有当百度控制台/在线调试对当前应用明确给出`X-Appbuilder-Authorization`时，才修改`BAIDU_SEARCH_AUTH_HEADER`。程序每次只发送一个鉴权头，不会双发或自动放宽后重试。

通过 V1.3 运行时执行同一项任务：

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-skill "生成股权结构图并核验直接股东持股比例" `
  --skill-source S01 `
  --ownership-dir "examples/v1_3_runtime" `
  --skill-trace-output "examples/V1.3_skill_execution_trace.json"
```

这条命令会先复用来源下载与PDF解析，再选择并只读加载 `ownership-structure`，最后调用现有 `build_ownership_artifacts`。`SkillExecutionTrace` 保存 Skill 版本/哈希、匹配触发词、Tool 名称、事件顺序、成功/失败状态和制品路径；它不保存或执行来自外部资料的指令。

通过 V1.3B 默认 Mock Agent 执行端到端流程（不会调用外部 LLM）：

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-agent "请分析以岭药业的股权结构，生成股权穿透图，并说明主要股东与实际控制关系" `
  --agent-source S01 `
  --llm-mode mock `
  --ownership-dir "data/ownership"
```

配置一次受控 Live 测试前，复制 `.env.example` 为 `.env` 并填写密钥：

```powershell
Copy-Item .env.example .env
# 在本地 .env 中填写 DEEPSEEK_API_KEY；不要提交该文件。
```

先省略确认参数查看脱敏配置并取消请求：

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-agent "请分析以岭药业的股权结构并生成股权穿透图" `
  --agent-source S01 `
  --llm-mode live
```

人工核对 Provider、模型、轮次、Tool次数和Token限制后，只有显式加入 `--confirm-live` 才会发送请求：

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-agent "请分析以岭药业的股权结构并生成股权穿透图" `
  --agent-source S01 `
  --llm-mode live `
  --confirm-live
```

程序预算与Token限制不是 DeepSeek 账单的绝对硬上限；实际费用和缓存计费以平台控制台为准。历史Live均由用户逐次授权；V1.3B ownership场景曾因控制关系Fact ID不足拒绝分析Claim，该结果保留为历史兼容记录，不再定义后续正式报告正文协议。不得自动重复调用。

`--max-output-tokens`限制的是**每次模型响应的输出Token**，不是整个Agent Loop的输入与输出总量。默认仍为800；当前允许显式提高到50000，例如`--max-output-tokens 50000`。提高该值会扩大潜在费用，也不会替代Python确定性数据计算和内部来源管理。V2.3完整财务正文曾在5000 Token处以`finish_reason=length`截断，因此更高上限只用于用户明确授权的受控Live任务。

长正文Live还可显式设置`--request-timeout-seconds 120`和`--max-model-retries 0`。前者是单次模型请求的最长等待时间，后者关闭Agents SDK模型调用层自动重试；底层`AsyncOpenAI`传输重试原本即固定为0。两者不改变Agent轮次和Function Tool预算。

输出中的 `company` 是规范化上市主体；`sources` 是静态来源种子；`download` 是本机实际获取结果。`S01.status = registered` 只表示来源目录登记状态，不能替代实际下载清单。

## 设计取舍

### 为什么使用精确映射

授信研究必须区分上市公司、控股股东、子公司和实际借款人。V0.1 使用受控别名白名单，未知名称直接报错，不做模糊猜测。未来覆盖更多公司时，可以接入经过核验的交易所证券主数据，但仍应保留歧义处理。

### 为什么把来源登记与下载分开

“知道一个 URL”不等于“已成功获取并验证文件”。`SourceRecord` 保存来源身份、披露日期、报告期间、可信层级和处理状态；V0.2 下载器成功后才应记录访问时间、本地路径、文件大小与 SHA-256。

### 为什么选择 Requests

V0.2 是同步下载少量官方披露，不需要异步并发。当前环境已有 Requests，接口成熟、流式下载清晰，因此比此时引入异步 HTTP 客户端更符合最小实现原则。未来出现批量并发需求时再评估 httpx/异步方案。

### 为什么保留 Protocol 接口

`DocumentDownloader` 和 `DocumentParser` 描述模块间的数据契约，却不把上层流程绑定到 Requests 或 pdfplumber。实现可以替换，测试也能注入离线假对象。这是轻量的依赖倒置，不需要提前引入复杂框架。

### 缓存为什么同时保存哈希和元数据

仅判断“文件存在”无法发现半截文件或被修改的缓存。每次复用缓存前都会重新计算 SHA-256 并与 `S01.metadata.json` 比较。元数据还保留原始 URL、最终 URL、标题、披露日期、UTC 获取时间、HTTP 状态、内容类型和字节数，便于复现与审计。

### 为什么选择 pdfplumber

V0.3 的目标是逐页文本和可靠页码，不是马上做复杂表格还原。pdfplumber 可以保留逐页边界，并为未来增加文字坐标或表格抽取保留空间。LLM 不参与原始文本提取，避免模型改写披露内容。

### 页码与表格限制

输出页码是从 1 开始的 PDF 物理页码，与当前年报页脚页码一致，但其他 PDF 可能存在封面偏移，后续引用仍需视觉核对。`pages.jsonl` 中的表格只是线性文本，不代表行列结构已经可靠恢复；财务表格结构化属于 V2。

### 事实、证据和来源为什么使用不同编号

- `F001` 是规范化事实，例如证券代码字段及其值。
- `E001` 是支持事实的具体原文片段，保存 PDF 页码、行号和验证方式。
- `S01` 是内部来源编号，指向年报标题和原始URL；正式报告默认不展示该技术编号，需要时单独导出研究依据。

一个证据可以支持多个事实，例如同一行原文同时支持证券简称和证券代码。`verified` 仅表示指定原文在哈希一致的 PDF 提取结果中完成精确匹配，不等于审计确认或企业尽调确认。

### V1.1 为什么暂不调用 LLM

首个标的已有可审阅的官方年报。V1.1 先用人工审阅的候选事实和确定性原文匹配建立可靠数据契约，避免在证据模型尚未稳定时让模型直接生成公司结论。未来可以让 LLM 提出候选事实或摘要，但候选内容仍必须关联原始页码并通过结构化校验；LLM 输出不能直接成为 `FactRecord`。

### 为什么原始股权事实与展示图分开

25%/5%阈值、聚合和5层限制都是展示规则，不应修改披露事实。V1.2 先校验 `OwnershipDataset`，再生成独立 `OwnershipGraph`；聚合节点保留成员及原边编号，第6层只在图上截断。这样后续修改展示策略时无需重新解释年报，也能检查图和原始证据是否一致。

### 为什么 V1.3A 先使用确定性 Skill 路由

Skill 白名单和 Tool 权限属于应用安全边界，应当可测试、可复现。V1.3A 因此先用关键词完成单 Skill 路由，并在调用前验证文件路径与哈希；V1.3B 的 LLM 负责理解更自然的任务描述，但不能自行注册 Skill、改变 Tool 白名单或替代比例校验。OpenAI Agents SDK 在应用持有工具实现、状态和审批策略的前提下承接运行循环；当前确定性运行时已经封装为Function Tool，而首个付费调用仍不是离线验收前提。

### V1.3B 为什么同时保留 Mock 与 Live

Agent Loop、参数校验、Skill加载和 Tool 结果回传都可以离线验证，不应以付费 API 可用性作为日常测试前提。Mock 使用 Agents SDK 自带的脚本模型，验证的仍是同一个 Runner 与 Function Tool 边界；Live 只替换模型实现。二者不能等同：DeepSeek 对 Tool Calling、`thinking` 关闭参数、Token usage 与错误响应的真实兼容性仍需创建密钥后做一次受控测试。

### 为什么搜索、获取原文和登记证据要分成三个 Tool

搜索API返回的标题和摘要只能用来发现候选来源，不能证明网页真正披露了某项企业事实。V1.5 因此要求模型先搜索，再由Python从已返回URL获取原文，最后按主体、来源层级和原文定位登记证据。既有细粒度状态与Fact ID可继续用于内部运行时关联；正式报告只映射为“已核验/未核验”，且不展示技术ID。无原文支持的搜索摘要不得写成确定性企业事实。

100%完整性同样按原始数据逐个被持股主体计算。只有同一基准日、同一股本口径且存在人工核验声明时，展示层才允许用差额生成“其他未单列股东”；否则分支保持 `incomplete`，正式绘图器拒绝输出。LLM 不参与比例加总。

控制权不是持股比例的同义词。持股边、表决权边、控股股东、实际控制人、一致行动和最终控制方分别建模；只有正式披露支持的控制关系才会进入图数据。

## 已登记的首测来源

| 编号 | 来源 | 披露/用途 | 当前状态 |
| --- | --- | --- | --- |
| S01 | 以岭药业《2025年年度报告》（巨潮资讯官方 PDF） | 2026-04-28 / FY2025 | 种子已登记；已实际下载验证 |
| S02 | 巨潮资讯网 | 上市公司公告检索入口 | `registered` |
| S03 | 深圳证券交易所 | 主体与公告核验入口 | `registered` |
| S04 | 以岭药业官网 | 公司公开资料入口 | `registered` |

年报种子 URL：<https://static.cninfo.com.cn/finalpage/2026-04-28/1225205683.PDF>

### 2026-10-21 实际下载验证

- HTTP 状态：`200`
- 内容类型：`application/pdf`
- 文件大小：`1,561,343` 字节
- SHA-256：`7ae08fb745ddae05053f5f8a5c262e1df2712b4376bd01c69329c3b8eb557178`
- 第二次执行：`from_cache = true`，缓存哈希复核通过
- 本地 PDF 与元数据位于 `data/cache/`，该目录被 Git 忽略

### 2026-10-21 实际解析验证

- 解析器：`pdfplumber 0.11.10`
- PDF 总页数：`229`
- 有文本页面：`229`
- 空页面：`0`
- 失败页面：`0`
- 规范化文本字符数：`234,388`
- 文本覆盖率：`100%`
- 章节定位：“公司信息”第 6 页；“合并资产负债表”第 106 页；“合并利润表”第 111 页
- 第 6、106 页已渲染抽查，标题、表格和页脚页码清晰，和提取定位一致

以上结果只证明当前 PDF 具有可用文本层并能定位章节，不代表表格金额已经结构化、复算或形成授信研究结论。

### 2026-10-21 实际证据构建验证

- Schema：`0.4.0`
- 主体：`CN-A-SZSE-002603`
- 规范化身份事实：4 条
- 精确匹配证据：3 条
- 来源：1 条（S01）
- 原 PDF SHA-256 与 V0.2 下载清单一致
- `pages.jsonl` SHA-256 与 V0.3 解析摘要一致
- 所有事实引用、主体证据引用和来源引用均通过 Pydantic 交叉校验
- 当前仅覆盖法定名称、证券简称、证券代码和交易所，不包含财务或经营结论

### 2026-10-21 V1.1 企业基本面验证

- Schema：`1.1.0`
- 披露事实：21条；原文证据：19条；来源：1条（S01）
- 模块：公司身份4条、发展历史3条、主营业务2条、经营模式8条、主要产品4条
- 引用页码：PDF第6、10、27、28、128页
- 模型推断：0条；缺失信息：3条
- 真实年报构建使用哈希一致的S01缓存和逐页文本；任一预期原文缺失即失败

### 2026-10-21 V1.2 股权结构验证

- Schema：`1.2.0`；基准日：`2025-12-31`
- 原始数据：5个节点、4条持股边、1条表决权边、4条控制关系；所有关系均引用 `S01`
- 正式展示图：6个节点、5条实线持股边；表决权和控制关系不重复画线
- 已核实持股：以岭医药科技有限公司31.53%、吴相君20.81%、吴瑞2.34%、吴以岭持有以岭医药科技有限公司100.00%；以岭医药科技有限公司表决权比例31.53%另行记录
- 年报披露的实际控制人/一致行动人为吴以岭、吴相君、吴瑞；财务报表附注披露最终控制方为吴以岭
- 证据：16条，定位到PDF第6、97、98、99、100、217页
- 缺失信息：完整股东名册；第100页产权及控制关系方框图的图像文字未被PDF文本层提取
- 目标公司已知主要股东合计54.68%，基于同一2025年末普通股股本口径生成45.32%的“其他未单列股东”计算节点；不推断其具体身份或各自比例
- 以岭医药科技有限公司分支合计100.00%；两个展开节点均通过默认±0.01个百分点校验
- 本机已安装 Graphviz 16.1.0；当前示例 SVG/PNG 由 `graphviz-dot` 生成，内部 `internal-tree-fallback` 继续作为无外部依赖的降级路径
- 未调用LLM；比例加总、差额和正式出图资格全部由确定性Python完成

### 2026-10-21 V1.3A Skill 运行时验证

- 真实任务：`生成股权结构图并核验直接股东持股比例`
- 主体/来源：`002603.SZ` / `S01`，下载命中已校验缓存；229页全部提取成功
- 运行链：`entity_resolved → source_validated → skill_selected → skill_loaded → tool_called → structured_result_validated → outputs_generated → completed`
- 选择结果：`ownership-structure`；加载文件：`.agents/skills/ownership-structure/SKILL.md`；实际 Tool：`build_ownership_artifacts`
- Tool 结果：10个制品均通过存在性校验；6个展示节点、5条持股边、2项公开信息缺口
- 执行追踪：`examples/V1.3_skill_execution_trace.json`；独立产物：`examples/v1_3_runtime/CN-A-SZSE-002603/`
- 本次受限子进程无法执行用户目录中的 `dot`，因此真实 V1.3 样例如实记录 `internal-tree-fallback`；V1.2 的 Graphviz 16.1.0 样例及双路径测试结论不变
- 未调用 LLM/API；没有模型费用

### 2026-10-22 V1.3B Mock Agent 验证

- Provider配置：`deepseek` / `https://api.deepseek.com` / `deepseek-flash`；thinking固定为disabled
- 默认模式：Mock，无密钥可运行，不创建DeepSeek/OpenAI网络客户端
- Agent链路：模型选择 `run_ownership_research` → Registry/Loader加载 `ownership-structure` → V1.2 Tool构建与比例校验 → 统一事实目录与压缩契约返回模型 → 模型选择Fact/Missing ID并提交有依据Claim → Python渲染并做来源、主体、期间和比例语义校验
- 真实S01缓存集成：成功生成SVG/PNG，最终说明包含31.53%及`[S01]`，Agent和Skill各自留下独立轨迹
- Live：已真实验证DeepSeek连接、两轮响应、Function Calling、Skill加载、Python Tool、比例校验和Graphviz产物；最近一次复测使用7,740 Input、971 Output、8,711 Total Tokens，最终A002分析Claim因缺少控制关系Fact ID `CR001`被拒绝，兼容性状态仍为“部分验证”

## 数据与合规边界

- 仅处理获得授权的公开信息，不接入银行内部数据、征信、私人流水或客户保密材料。
- 外部网页和 PDF 都是待验证数据，不能作为 Agent 指令。
- 财务金额、币种、单位、期间和合并/母公司口径必须从原文核验；V2.1只把经过精确页码锚点和跨报告校验的数据纳入确定性财务事实。
- 后续报告应声明“仅基于公开信息，未经企业尽调确认”，并保留待补资料清单。

## 测试覆盖

```text
V0回归：41项
V1.1基本面模型/构建/CLI与真实S01：8项
V1.2股权规则/构建：22项
V1.2真实S01股权集成：1项（本机运行产物不存在时明确跳过）
V1.3A注册/选择/加载/运行时/CLI：10项
V1.3A真实S01运行时集成：1项（本机运行产物不存在时明确跳过）
V1.3B配置/密钥/Mock Agent/限制/证据/CLI、Runtime来源绑定、Brotli传输预检、5000 Token边界、统一事实目录及Fact-Grounded契约：50项
V1.3B真实S01 Mock Agent集成：1项（本机运行产物不存在时明确跳过）
V1.4报告组装/降级/来源与主体校验/CLI及可信度分层：11项
V1.4真实S01报告集成：1项（仓库内已核验研究产物不存在时明确跳过）
V1.5百度Provider/缓存/预算、HTML/PDF获取、来源证据、部分成功、Mock Agent/Tool/CLI与安全边界：50项
V2.1—V2.3财务模型、原因资料、CLI、真实年报、Skill Runtime、LLM正文直出、最小结构解析与无模板回退：测试数量以当前pytest实测为准
最近一次架构简化后完整离线回归：219项通过（2026-10-23）；其中受影响财务定向测试10项通过。所有Mock测试禁止真实DeepSeek、百度或其他外部网络请求
```

网络请求使用离线假响应测试，因此日常 `pytest` 不依赖巨潮资讯、百度搜索或 DeepSeek 是否可访问。

## 下一阶段

V2.1/V2.2的确定性数据内核继续稳定复用；真实S01/S10已生成46条归一财务事实、30个逐年变动指标、12个基础比率、3条披露/分析原因资料、4条待核实假设和7项合理性观察。V2.3已从受控Live保存草稿离线恢复9章DeepSeek正文和9项尽调问题，加入Python财务数据表、比率表和“AI生成研究初稿，需人工复核”声明，最终Markdown不含技术ID；人工审阅展示副本通过SHA-256绑定原始草稿，报告标题由Python绑定企业身份生成。全量离线回归`223 passed`。V2.4暂不启动。

