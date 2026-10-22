# 企业授信研究Agent — 公共信息研究 Agent 开发路线（v5：双 Skill 集成）

> V0.1—V0.4、V1.1—V1.2 已由本地代码、真实年报产物和测试核验完成；`ownership-structure` 的确定性 Tools 已实现，Skill 运行时尚未开发。下一阶段为 V1.3；首个标的`以岭药业 / 002603.SZ`，默认不上传资料。

## [x] V0：上市公司数据入口与证据基础
- [x] **V0.1** 建立项目骨架、最少依赖、README、实体识别/映射（`以岭药业`、`002603`、`002603.SZ`→`石家庄以岭药业股份有限公司 / 002603.SZ`）及官方来源种子目录；编写离线测试。首个种子为2025年年报（`https://static.cninfo.com.cn/finalpage/2026-04-28/1225205683.PDF`）；同时登记深交所、巨潮资讯和公司官网。V0.1不要求真实下载，也不需要立即写通用搜索引擎。
- [x] V0.2 实现可控的网络抓取/下载、错误处理、文件缓存及来源元数据管理（URL/标题/日期/获取时间）。
- [x] V0.3 对至少一份实际年报实现文本提取及可定位页码，输出结构化小样；无法提取时记录失败并人工核查。
- [x] V0.4 Pydantic 事实/证据/公司主体数据模型，输出证据 JSON 和编号引用列表。

**V0验收**：CLI输入`以岭药业`或`002603`可确认A股上市主体、找到并处理真实可访问官方披露，生成来源清单/最少若干可核查事实；无来源时不填；pytest可离线执行。

## [x] v5 文档整合与 V0 现状核查（不实现 Skill）

- [x] 完整阅读并合并 v5 `AGENTS.md`、`PROJECT_BRIEF.md`、`PRD_PUBLIC_RESEARCH.md`、`SKILLS_SPEC.md`、`ROADMAP.md`。
- [x] 将 `ownership-structure` 与 `financial-analysis` 的完整 `SKILL.md` 纳入 `.agents/skills/`；文件当前仅是业务执行规范，不等于运行时功能完成。
- [x] 只读核对 V0 代码、Git 状态、真实缓存/示例和公共接口，形成 `V0_AUDIT.md`。
- [x] 重跑现有离线测试，确认 V0 回归通过；未修改 `src/` 与 `tests/`。

## [ ] V1：企业研究初稿与单 Agent
- [x] **V1.1 企业基本面事实抽取**：复用 `CompanyResolver`、`SourceCatalog`、`PdfTextExtractor`、`EvidenceBuilder`，将发展历史、主营业务、经营模式和主要产品分模块抽取为带 `Fxxx/Exxx/Sxx` 引用的结构化结果；先不追求覆盖所有年份。
- [x] **V1.2 `ownership-structure` 确定性内核**：定义股权节点、边、控制关系、基准日、核验状态及展示配置，并实现 `validate_ownership_facts`、`validate_ownership_percentages`、`aggregate_ownership_holders`、`build_ownership_graph`、`render_ownership_graph`。逐个被展开主体校验同一时点/股本口径的100%完整性；只有全部通过的 `formal_ready` 图才输出SVG/PNG。公开资料检索/下载/PDF读取继续复用 V0，不复制第二套管线。
- [ ] **V1.3 Skill 运行时机制与 Agent Tool Calling**：实现白名单 `SkillRegistry`（`name`、`description`、路径、版本/哈希）、按任务选择的 `SkillSelector`、只读 `SkillLoader` 及 `SkillExecutionTrace`。调用顺序固定为“解析主体 → 获取/校验来源 → 选择并加载 Skill → 调用确定性 Tools → 校验结构化结果 → 生成引用/文字”；Codex 能发现文件不视为 Agents SDK 已接入。此阶段再引入所需的 OpenAI Agents SDK/LLM API，不提前构建多 Agent。
- [ ] **V1.4 报告初稿**：Markdown 渲染正文`[S01]`引用、来源附录、股权图和待核查信息；数据不足时允许只输出局部结果和明确限制。

**V1 验收测试**：

- 现有 41 项 V0 测试持续通过；V1.1 的每条事实均能回溯到来源和页码/章节。
- 股权离线虚构 fixture 覆盖 1—5 层、第 6 层截断，24.99/25/25.01% 自然人，4.99/5/5.01% 机构，逐分支100%、85%不完整、105%异常、小股东合并、重要自然人保留、国资参股与控制区分、环路/重复节点、日期冲突和来源缺失。
- 图中目标公司置顶；节点只显示名称和比例，箭头从股东指向被持股企业；SVG/PNG可渲染且无来源/控制说明文字或虚线。
- 以岭药业端到端案例只使用经核验披露，不将虚构 fixture 或未知比例写入真实图。
- 运行日志或集成测试证明 `ownership-structure` 被选择、加载并调用了实际 Tool；只存在 `SKILL.md` 不算验收通过。

## [ ] V2：财务与行业研究
- [ ] **V2.1 可比财务事实层**：真实发现并登记 2024/2023 年官方年报，不猜测 URL；从 2024、2025 合并财务报表及附注提取三表数据，必要时使用 2023 期初余额。每个数据点保留原始科目、期间、币种、单位、合并/母公司口径、`source_id` 和页码；表格结构化结果必须回看原文核验。
- [ ] **V2.2 财务确定性 Tools**：开发 `normalize_financial_items`、`validate_financial_facts`、`calculate_financial_metrics`、`rank_material_changes`。统一同比公式、精度和错误状态，禁止 LLM 徒手计算关键数值；Pandas 仅用于确定性数据处理。
- [ ] **V2.3 `financial-analysis` 专业流程**：主 Agent 加载 Skill 后，按“金额 → 变化额/比例 → 已披露原因或待核实假设 → 合理性验证 → 授信关注/尽调问题”输出；再逐步增加 `extract_change_explanations`、`assess_reasonableness`、`generate_financial_section`。事实、支持性推断、未核实假设严格分层。
- [ ] **V2.4 行业与政策研究**：定向检索政府/监管一手来源，保留发布日期、实施状态、适用范围和企业影响的不确定性。
- [ ] **V2.5 双 Skill 集成**：同一次研究任务按需加载两个 Skill，复用统一的主体、来源、证据和报告引用，生成股权图、财务分析、风险信号与确实缺失的尽调问题。

**V2 验收测试**：

- 至少两个完整年度的真实关键财务数据可追溯、可复算；真实资料测试与模拟边界测试分开记录。
- 1000→1200 恰好 +20% 必须触发，1000→1199 不因该阈值触发；另测 0→100、100→0、-100→-200、正负切换、小基数、重述/重分类、币种/单位/报表口径冲突。
- 必查货币资金、应收账款、预付款项、固定资产、短期借款、应付账款、主营业务收入、经营现金流，并覆盖存货；缺失字段转为明确的待核查项。
- 至少识别“应收增速高于收入”“利润与经营现金流背离”“短债上升但可用现金未知”等基础信号，但不自动推导违约结论。
- 第一版银行流水分析保持关闭；未公开的存款银行、贷款银行、交易对手和流水不得臆造。
- 集成日志证明两个 Skill 的选择、加载、Tool 调用、校验结果和失败降级；最终报告引用沿用 `[Sxx]`，不得破坏 V0 编号。

## [ ] V3：演示产品与工程质量
- [ ] V3.1 Streamlit名称输入、主体展示、执行状态、分模块结果、图表；首版无需上传控件。
- [ ] V3.2 Word报告生成、来源附录、图表嵌入、报告导出。
- [ ] V3.3 增加网络异常、PDF解析失败、来源冲突、币种混淆、年份口径错误等离线测试与人工核查样本。
- [ ] V3.4 GitHub文档、演示视频/截图、耗时和效果评估；只写真实测出的简历结果。

**V3验收**：全新环境可按README复现，输入以岭药业得到有来源的七模块初稿和需补充资料清单；对失败和资料不足处理明确。

## [ ] V4（未来扩展，不属于当前第一版）
- [ ] 用户上传自有且获得授权的财报/企业介绍/尽调资料，与公开资料交叉核验。
- [ ] 第二个测试对象考虑九州通（600998.SH），验证医药流通场景；之后再扩展其他上市公司及未来授权的非上市公司资料。按实际需要使用RAG、MCP或更复杂的架构。

## 首测数据与验收补充
- 第一轮**只验证** `以岭药业`/`002603`/`002603.SZ` 名称映射、2025年官方年报种子登记、来源元信息和离线pytest；不强求当轮抓取成功或实现Agent。
- V0.2再测试真实下载，V0.3再提取PDF文字与页码，V2再结构化与复核完整财务数据。
- 上市主体、单位、期间、合并口径出现歧义时必须停止生成确定性数值，保留待确认说明。

## 开发日志

### 2026-10-21 — V0.1 完成

- 实现文件：`pyproject.toml`、`.gitignore`、`README.md`、`src/credit_research_agent/`、`tests/`。
- 企业登记：以岭药业/法定名称/`002603`/`002603.SZ`精确映射，默认合并口径；未知主体显式报错。
- 来源登记：S01 2025 年报、巨潮资讯、深交所、公司官网；S01 保持 `registered`，未声称已下载或解析。
- 接口设计：定义 `DocumentDownloader` 与 `DocumentParser` 的输入输出契约，实际实现留至 V0.2/V0.3。
- 实际测试命令：`python -m pytest -q`；结果：`15 passed in 0.17s`。
- 遗留问题：尚未联网验证或下载 S01，未做 PDF 解析；按路线分别进入 V0.2、V0.3。

### 2026-10-21 — V0.2 完成

- 实现文件：`src/credit_research_agent/downloader.py`，并扩展 `documents.py`、`cli.py`、`pyproject.toml`、README 和测试。
- 受控下载：仅支持登记的年度报告，强制 HTTPS 和官方域名白名单；校验重定向、HTTP 状态、大小上限和 PDF 文件头。
- 缓存与审计：原子写入 PDF，计算 SHA-256；元数据清单保存 URL、标题、披露日期、UTC 获取时间、HTTP 状态、内容类型、文件大小与哈希；复用前重新验哈希。
- 错误处理：瞬时网络/服务端错误有限重试；失败保存机器可读错误码和消息；无效 PDF、超限文件及非官方重定向均拒绝。
- 离线测试：`python -m pytest -q`，最终回归结果 `25 passed in 0.89s`。
- 真实下载：S01 返回 HTTP 200、`application/pdf`，共 `1,561,343` 字节；SHA-256 为 `7ae08fb745ddae05053f5f8a5c262e1df2712b4376bd01c69329c3b8eb557178`。二次执行通过哈希校验命中缓存。
- 遗留问题：尚未解析 PDF 文本、页码或披露内容；这些工作属于 V0.3，当前不得据此声称年报业务事实已提取。

### 2026-10-21 — V0.3 完成

- 实现文件：`src/credit_research_agent/pdf_parser.py`，扩展 `documents.py`、`cli.py`、`pyproject.toml`，新增 `tests/test_pdf_parser.py` 和 `examples/S01_extraction_sample.json`。
- 解析前校验：重新计算源 PDF 的 SHA-256，并与 V0.2 下载清单比较；不一致时拒绝解析并记录 `source_hash_mismatch`。
- 逐页提取：使用 pdfplumber，保留从 1 开始的 PDF 物理页码、文本、字符数及 `extracted/empty/failed` 状态；单页异常不丢弃其他页面。
- 解析产物：完整全文写入 `pages.jsonl`，质量数据写入 `summary.json`，章节页码写入轻量 sample；整体失败写入 `failure.json`。
- 离线测试：`.venv\\Scripts\\python.exe -m pytest -q`，最终回归结果 `33 passed in 0.63s`。
- 真实解析：S01 共 229 页，229 页有文本，0 空页、0 失败页，共 234,388 个规范化字符；章节定位为“公司信息”第 6 页、“合并资产负债表”第 106 页、“合并利润表”第 111 页。
- 视觉复核：渲染并检查 PDF 第 6、106 页，确认章节标题、表格与页脚页码清晰，和文本定位一致。
- 遗留问题：当前表格仍是线性文本，尚未进行财务科目结构化、金额/单位/口径核验；下一阶段为 V0.4 事实与证据模型。

### 2026-10-21 — V0.4 完成

- 实现文件：扩展 `models.py`，新增 `evidence.py`、`data/evidence_seeds.json`、`tests/test_evidence.py`，并扩展 CLI、解析摘要与示例产物。
- 数据模型：新增 `FactRecord`、`EvidenceRecord`、`EvidenceBundle`；分别使用 `Fxxx`、`Exxx`、`Sxx` 编号，并验证主体、事实、证据、来源之间不存在悬空引用。
- 完整性：V0.3 摘要新增 `pages_sha256`；构建事实前同时校验原 PDF 哈希与逐页文本哈希。
- 确定性证据：对 S01 PDF 第 6 页进行精确行匹配，生成法定名称、证券简称、证券代码、交易所共 4 条身份事实及 3 个证据片段；未调用 LLM。
- 引用输出：生成 `[S01]` Markdown 来源条目，包含标题、发布者、披露日期、访问日期、URL 和 1-based PDF 页码。
- 失败处理：页码不存在、页面未提取、原文不匹配、哈希不一致、事实值与主体登记冲突、引用悬空时均拒绝生成已验证事实并记录错误码。
- 离线测试：`.venv\\Scripts\\python.exe -m pytest -q`，最终回归结果 `41 passed in 0.76s`。
- 真实产物：`examples/S01_evidence_sample.json`（schema 0.4.0，4 facts / 3 evidence / 1 source）及 `examples/S01_citations.md`；构建过程无失败记录。
- 遗留问题：证据范围目前仅覆盖主体身份字段；公司历史、主营业务、经营模式、产品、股权和财务事实尚未构建，下一阶段进入 V1.1。

### 2026-10-21 — v5 文档与双 Skill 规范整合完成

- 只读核对现有 V0.1—V0.4 代码、缓存/解析产物、示例和 Git 状态，新增 `V0_AUDIT.md`；确认没有重做或修改 V0 代码。
- 合并 v5 `AGENTS.md`、`PROJECT_BRIEF.md`、`PRD_PUBLIC_RESEARCH.md` 和本路线，新增 `SKILLS_SPEC.md`。
- 纳入 `.agents/skills/ownership-structure/SKILL.md` 与 `.agents/skills/financial-analysis/SKILL.md`；二者当前是业务规范，相关 Python Tools 和运行时 Skill 调用均保持未完成状态。
- 明确开发顺序：V1.1 基本面证据 → V1.2 股权确定性内核 → V1.3 SkillRegistry/Agent 调用 → V1.4 初版报告 → V2.1 可比财务事实 → V2.2 财务计算 → V2.3 财务 Skill → V2.5 双 Skill 集成。
- 实际测试命令：`.venv\Scripts\python.exe -m pytest -q`；整合前结果：`41 passed in 0.77s`，文档整合后最终回归：`41 passed in 0.85s`。
- 遗留问题：Git 仓库目前尚无 commit 且项目文件均未跟踪；是否建立 V0.4/v5 文档整合基线提交需用户确认，本轮未执行 commit。

### 2026-10-21 — V1.1 企业基本面事实抽取完成

- 数据模型：新增 `ResearchModule`、`StatementType`、`ModelInference`、`MissingInformation` 和 `FundamentalResearchBundle`；`FactRecord` 只允许 `disclosed_fact`，推断与缺失信息不能伪装成事实。
- 复用接口：继续使用 `CompanyResolver`、`SourceCatalog`、`OfficialPdfDownloader`、`PdfTextExtractor`、`EvidenceBuilder`、`SourceRecord`、`FactRecord` 和 `EvidenceRecord`，未复制下载/PDF/来源系统。
- 基本面构建：新增 `fundamentals.py` 与人工审阅的 `fundamentals_seeds.json`，支持跨 PDF 换行的事实值校验、双哈希、精确原文匹配、悬空引用校验和失败记录。
- CLI：新增 `--build-fundamentals`，输出结构化 JSON 与 `[S01]` 引用附录；包版本更新为 `1.1.0`。
- 真实年报结果：S01 共生成21条披露事实、19条证据、0条模型推断和3条缺失信息；模块分布为身份4、发展历史3、主营业务2、经营模式8、主要产品4；引用PDF第6、10、27、28、128页。
- LLM：本阶段未调用任何 LLM API，未产生模型费用。人工审阅种子只生成候选契约，最终事实仍必须通过哈希一致的原文精确匹配。
- 测试：先执行 V1.1 模块与真实年报测试，`16 passed in 0.70s`；最终执行 `.venv\Scripts\python.exe -m pytest -q`，结果 `49 passed in 0.90s`。
- 安装验证：虚拟环境中的旧版0.4.0包已用本地源码离线更新到1.1.0；实际 CLI 命中缓存完成229页解析和基本面产物构建。
- 遗留问题：前身企业2001年前完整沿革、统一盈利模式、各单品收入/占比尚未从当前证据链确认；V1.2才开始股权结构模型与确定性Tools。

### 2026-10-21 — V1.2 股权结构确定性内核初版完成

- 数据模型：新增股权节点、持股/表决权边、控制关系、国资控制判断、基准日、核验状态、展示配置、原始数据集与展示图模型；控制关系不由比例自动推断。
- 确定性 Tools：实现 `validate_ownership_facts`、`aggregate_ownership_holders`、`build_ownership_graph`、`render_ownership_graph`，原始事实与聚合/裁剪后的展示图分离。
- 规则边界：覆盖自然人24.99/25/25.01%、机构4.99/5/5.01%、重要控制主体例外、1—5层及第6层截断、无证据不补足100%、国资参股与控制区分。
- 异常校验：覆盖来源/证据缺失、重复节点/关系、日期与比例冲突、合计超过100%、环路和图/JSON一致性；任一已核实边缺少来源时拒绝建图。
- 真实S01结果：基准日2025-12-31，5个节点、4条持股边、1条表决权边、4条控制关系、16条证据和2条缺失信息；引用PDF第6、97、98、99、100、217页。
- 真实持股事实：以岭医药科技有限公司31.53%、吴相君20.81%、吴瑞2.34%、吴以岭持有以岭医药科技有限公司100.00%；实际控制人/一致行动和最终控制方依据年报分别建模。
- 产物：原始数据JSON、展示图JSON、DOT、SVG和`[S01]`引用；本机未安装Graphviz `dot`，示例明确记录为`internal-svg-fallback`，没有冒充Graphviz成功。
- LLM：本阶段未调用LLM API，未产生模型费用；也未实现SkillRegistry或Agent Tool Calling。
- 测试：股权模块/CLI/真实年报集成与全量回归均通过，最终 `.venv\Scripts\python.exe -m pytest -q` 为 `67 passed`。
- 遗留问题：完整股东名册无法由前十名股东表补足；第100页产权关系方框图的图像文字未被当前PDF文本层提取；下一阶段为V1.3 Skill运行时机制。

### 2026-10-21 — V1.2 股权图与比例完整性优化

- 新增 `validate_ownership_percentages`，以单个被持股主体为单位按同一基准日和股本口径加总；默认容差±0.01个百分点，85%保持不完整，105%报错，未通过时拒绝正式出图。
- 只有存在同口径、同一时点的人工核验声明时，才在展示层生成“其他未单列股东”确定性差额；原始股东节点、边和证据不变。
- Graphviz DOT及内部降级渲染统一为目标公司置顶、股东按分支向下、箭头由下方股东指向上方被持股企业；节点仅含名称与比例，控制关系/来源/状态移到图外。
- 产物扩展为 `ownership.json`、展示图JSON、DOT、`ownership_structure.svg/png`、`ownership_summary.md`、`ownership_validation.json`、来源及待核查清单。
- 真实S01展示图为6个节点、5条持股线；目标公司分支54.68%+计算差额45.32%=100%，以岭医药科技分支100%，均通过校验。计算差额不代表某一具体股东。
- 已安装并验证官方Graphviz 16.1.0；当前示例使用`graphviz-dot`生成SVG/PNG并完成目视检查，内部`internal-tree-fallback`继续保留。两种路径均无节点重叠或交叉线。
- 全量测试：降级路径为`72 passed in 1.93s`；Graphviz启用路径使用独立临时目录复核为`72 passed in 5.20s`。
- 仍未实现 SkillRegistry/Agent Tool Calling；下一阶段仍为V1.3，不提前开发financial-analysis Skill。
