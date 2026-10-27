# 企业授信研究Agent — 公共信息研究 Agent 开发路线（v5：双 Skill 集成）

> V0.1—V3.2 的既有数据、Agent、双 Skill、检索、报告、Streamlit 与 Word 能力继续保留；V2.5 最新网页 Live 已在保守 JSON 解析修复后成功生成完整报告。2026-10-26 用户将原“V3.3项目整理”顺延至V3.4，并把V3.3调整为通用A股自动接入。V3.3A—D现已完成沪深A股动态主体识别、官方年报准备、通用基本面/股权/财务提取，以及完整Agent/Streamlit的离线接入；V3.3E已完成九州通与工业富联两次新企业网页Live。九州通已正常交付六章报告；工业富联已生成六章正文，但模型在首个完整JSON后追加占位伪JSON，原在线状态因此失败，现已用保守规则零网络恢复Markdown/Word并新增回归。工业富联部分股权与财务科目仍只达到部分成功；独立盲测、北交所及更广泛版式尚未验收，不能把当前结果表述为所有A股均已兼容。

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
- [x] **V1.3A 确定性 Skill Runtime**：实现白名单 `SkillRegistry`（`name`、`description`、路径、版本/哈希）、按任务选择的 `SkillSelector`、只读 `SkillLoader` 及 `SkillExecutionTrace`。调用顺序固定为“解析主体 → 获取/校验来源 → 选择并加载 Skill → 调用确定性 Tools → 校验结构化结果 → 生成引用/文字”；真实S01集成测试已证明 `ownership-structure` 被选择、加载并实际调用 `build_ownership_artifacts`。本阶段没有LLM判断、Agent Loop 或付费 API 调用。
- [ ] **V1.3B DeepSeek LLM Agent Runtime（既有能力保留）**：使用 OpenAI Agents SDK 的 `Agent`、`Runner` 和受限 Function Tool 接入 DeepSeek OpenAI 兼容接口。默认 Mock 模式且禁止联网；Live 模式必须显式选择、存在 `DEEPSEEK_API_KEY` 并由用户确认。第一阶段只向模型开放 `ownership-structure` 高层 Tool，运行时真实经过 Registry、Loader 与现有确定性内核，并记录模型选择、Skill加载、Tool调用、比例校验、产物和Token使用轨迹。`thinking`固定为disabled。LLM仅填写ticker，Python绑定并验证source_id。历史Fact-Grounded输出契约继续作为ownership内部兼容实现，但不再作为后续正式报告正文的统一规范。
- [x] **V1.4 报告初稿**：已完成 Markdown 框架、`[S01]`引用、来源附录、股权图和待核查信息的确定性组装。该阶段的“无LLM也生成基础正文”是历史 MVP 方案；自 V2.3 架构调整起，正式研究分析正文必须由 DeepSeek 撰写，Python 只保留数据/图表/附录和未完成标记，不再用模板冒充正式分析。
- [x] **V1.5 基础信息检索（既有能力保留）**：实现通用`SearchProvider`与百度官方Web Search Provider，注册`search_web`、`fetch_source`、`extract_evidence`三个白名单Tool。既有`verified / single_source / unverified / unsupported`可继续用于内部诊断，正式报告统一映射为“已核验/未核验”；单页读取失败、无效页面和预算耗尽均可降级，搜索摘要不升级为确定事实。默认Mock且搜索API请求数为0；Live需`BAIDU_SEARCH_API_KEY`、`--search-mode live`与`--confirm-search-live`。

**V1 验收测试**：

- 现有 41 项 V0 测试持续通过；V1.1 的每条事实均能回溯到来源和页码/章节。
- 股权离线虚构 fixture 覆盖 1—5 层、第 6 层截断，24.99/25/25.01% 自然人，4.99/5/5.01% 机构，逐分支100%、85%不完整、105%异常、小股东合并、重要自然人保留、国资参股与控制区分、环路/重复节点、日期冲突和来源缺失。
- 图中目标公司置顶；节点只显示名称和比例，箭头从股东指向被持股企业；SVG/PNG可渲染且无来源/控制说明文字或虚线。
- 以岭药业端到端案例只使用经核验披露，不将虚构 fixture 或未知比例写入真实图。
- 运行日志或集成测试证明 `ownership-structure` 被选择、加载并调用了实际 Tool；只存在 `SKILL.md` 不算验收通过。
- V1.3B Mock 测试不得产生任何外部 LLM 请求；Live 模式缺少密钥、缺少显式确认或超出轮次/Tool次数时必须安全终止。内部事实与来源仍需可追溯，但正式报告默认不显示技术编号。
- V1.5 Mock 测试必须证明 Agent 按顺序调用搜索、原文获取和证据提取，且不发送网络请求。搜索摘要不得直接成为事实；未核验第三方资料、冲突值、验证码页和疑似网页指令注入必须隔离。单任务默认最多15轮检索、15次搜索Tool/API请求、5条/次、20次抓取、20次提取、60次总Tool调用和70轮Agent，由Python分别计数并强制；第15轮允许、第16轮阻止。预算是上限而非目标，资料充分时提前结束。

## [x] V2：财务与行业研究
- [x] **V2.1 可比财务事实层**：真实发现并登记 2024/2023 年官方年报，不猜测 URL；从 2024、2025 合并财务报表及附注提取三表数据，必要时使用 2023 期初余额。每个数据点保留原始科目、期间、币种、单位、合并/母公司口径、`source_id` 和页码；表格结构化结果必须回看原文核验。
- [x] **V2.2 财务确定性 Tools**：开发 `normalize_financial_items`、`validate_financial_facts`、`calculate_financial_metrics`、`rank_material_changes`。统一同比公式、精度和错误状态，禁止 LLM 徒手计算关键数值；Pandas 仅用于确定性数据处理。
- [x] **V2.3 `financial-analysis` 专业流程**：保留确定性财务事实、指标、原因资料和必要内部来源作为分析上下文，不再由Python模板承担正式报告作者。主Agent通过只接受ticker的`run_financial_research`加载Skill和数据Tool，DeepSeek输出完整自然语言财务分析与尽调问题；Python仅绑定来源/路径、计算数值、解析最小输出结构、加入数据表并组装Markdown。模型无需返回Fact/Metric/Source ID，正文不因百分比白名单、正则或逐句来源匹配被拒绝。120秒超时、0次自动重试的受控Live完整返回9章正文和9项尽调问题；已使用保存草稿和SHA-256绑定的人工审阅展示副本离线恢复不含技术ID的正式Markdown，报告标题由Python绑定企业身份生成，全量回归`223 passed`，无模板回退。
- [x] **V2.4 行业与政策研究**：不新增第三个Skill，以主Agent独立的轻量Instructions指导DeepSeek根据主营业务自主制定检索方向，复用V1.5的`search_web`、`fetch_source`、`extract_evidence`及预算/缓存/安全机制，并读取V2.3结构化财务上下文。DeepSeek负责撰写行业趋势、竞争格局、企业市场地位、政策监管、企业影响、授信风险和尽调问题；Python只负责数据/工具、政策元数据与来源保存、最小结构解析和Markdown组装。政策研究优先政府/监管一手原文；百度搜索日期只作为后台来源元数据，未经原文定位不写入事实期间。一般政策及行业—企业—财务因果关系未经确认时必须使用不确定性措辞。
- [x] **V2.5 完整企业授信研究**：使用一个DeepSeek主Agent，在同一次任务中规划并实际调用`ownership-structure`与`financial-analysis`两个既有Skill；行业研究继续复用V1.5检索Tools和独立Instructions，不注册第三个Skill。统一绑定主体、官方来源、可比期间和输出目录；DeepSeek撰写企业基本情况、经营、行业政策、财务、综合授信风险和尽调问题六章正文，Python只负责数据、计算、可选股权图、财务表格、来源/轨迹及Markdown机械组装。第一章写到研究日，覆盖历史背景、重大事件/业务演变、企业定位、产业链位置和上下游角色；常规股权/控制/完整性/国资文字从正式报告移除，仅在有图片时插入中性图注。ownership仍后台执行并留痕，资料不完整或无图不阻断其他章节；只有有明确原文依据的重大治理风险事件可进入第五/六章。早期Live暴露的内部引号和“简短前言+唯一JSON代码块”解析问题已按保守规则修复；最新网页Live已成功生成完整报告，旧失败轨迹继续保留用于审计。

**V2 验收测试**：

- 至少两个完整年度的真实关键财务数据可追溯、可复算；真实资料测试与模拟边界测试分开记录。
- 1000→1200 恰好 +20% 必须触发，1000→1199 不因该阈值触发；另测 0→100、100→0、-100→-200、正负切换、小基数、重述/重分类、币种/单位/报表口径冲突。
- 必查货币资金、应收账款、预付款项、固定资产、短期借款、应付账款、主营业务收入、经营现金流，并覆盖存货；缺失字段转为明确的待核查项。
- 至少识别“应收增速高于收入”“利润与经营现金流背离”“短债上升但可用现金未知”等基础信号，但不自动推导违约结论。
- 第一版银行流水分析保持关闭；未公开的存款银行、贷款银行、交易对手和流水不得臆造。
- 集成日志证明两个 Skill 的选择、加载、Tool 调用、结构化数据结果和失败降级；V0来源编号继续在内部数据层保留，最终报告默认不显示技术ID。
- 财务Mock需证明模型自然语言正文未经Python改写直接进入Markdown，并包含必要数据表和尽调问题；报告只以“已核验/未核验”区分资料和假设。模型整体调用或最小结构解析失败时不得生成固定模板分析。
- 行业Mock需证明DeepSeek模型路径自主调用搜索、原文获取和证据提取Tools，能够利用紧凑业务/财务上下文输出自然语言行业正文；Python不得生成固定模板行业分析，Mock不得创建真实DeepSeek或百度请求。
- 行业报告至少覆盖行业趋势、竞争及市场地位、政策监管、对企业经营/财务的可能影响、授信风险和尽调问题。市场规模、份额、排名和具体政策影响缺少资料时可以不输出数字，不得编造。
- 政策来源应优先一手原文并保存发布日期、状态、适用对象/地区和原始URL；单个来源403/404/超时不终止任务，其他成功来源和失败原因均保留，允许`PARTIAL_SUCCESS`。
- V2.4正式Markdown正文来自DeepSeek且不显示技术ID；后台独立保存紧凑研究上下文、来源/原文定位和执行轨迹。模型整体失败或最小结构解析失败时只交付可用数据/来源与未完成说明，不得以Python模板替代。
- V2.5 Mock必须证明同一根Agent轨迹中两个Skill均被模型选择、Registry确认、Loader加载并调用实际Tool；旧单Skill`select()`语义和双Skill白名单保持兼容。
- V2.5完整Markdown必须包含六章、必要财务表和针对性尽调问题；股权图仅在实际图片存在时插入，缺图直接跳过且不丢弃其他研究成果。模型章节原文不得被Python模板替换，正文不展示技术ID；财务/检索/章节缺失仍按实际状态降级。
- V2.5模型上下文不得嵌入完整PDF/网页正文或重复大JSON；两个Skill与检索Tools受统一轮次和总Tool预算限制。Mock不联网，完整Live仍需DeepSeek与百度搜索分别显式确认。

## [ ] V3：产品化与通用A股接入
- [x] **V3.1A 历史报告展示**：Streamlit安全发现并加载既有Markdown报告，支持全文/章节导航、股权PNG/SVG和现有财务数据表展示；恢复版报告保留原始失败状态，不静默包装为成功。
- [x] **V3.1B 连接既有 Agent Runtime**：支持企业名称/代码、研究指令和完整/股权/财务/行业/基本面模式；通过受控Python服务以白名单参数、`shell=False`调用现有CLI/Runtime。Live按模式要求DeepSeek或DeepSeek+百度显式确认，CLI/Runtime保留第二层门禁。
- [x] **V3.1C 持久化任务与用户体验**：独立后台Worker保存任务请求、状态和脱敏进度；页面刷新或关闭不重复启动任务，幂等键、运行中任务锁及跨浏览器会话的项目级原子Live锁阻止重复提交；并发时只有一个会话能启动Live。支持从磁盘恢复队列/运行/完成/部分完成/失败状态并保留已有产物。Worker完成、启动/校验失败或陈旧任务收敛时释放Live锁；PID消失的`running`任务或超过60秒的`queued`任务会自动收敛为失败。Windows通过`OpenProcess/GetExitCodeProcess`只读探测PID，POSIX才使用`os.kill(pid, 0)`；损坏/空锁仅在确认无其他活动Live任务后清理并原子重试。完整及股权/财务/行业单项Runtime共用脱敏进度接口。网页LLM Live固定使用600秒单次请求超时与0次SDK自动重试，不在确认区额外展示预算；网页完整Live隐藏预算现为70轮、60次总Tool、搜索轮次/调用次数/真实请求数均为15、20次网页读取和20次证据提取，其他模式不变，资料充分时提前停止。
- [x] **V3.2 Word报告导出**：新增CLI与Streamlit共用的本地确定性导出模块，把既有Markdown转换为A4 Word标题、正文、列表、表格和图片；复用任务目录内既有股权PNG，不重跑Graphviz、不调用LLM/搜索、不改写DeepSeek正文。Streamlit按当前任务目录生成或缓存对应DOCX，历史报告同样可下载；路径越界、远程图片和必要图片缺失会显式失败。真实V2.5以岭药业恢复报告已生成16页A4 DOCX并完成逐页视觉核查；专项与完整回归结果见`V3_2_STATUS.md`。
- [ ] **V3.3 通用上市公司自动接入**
  - [x] **V3.3A 通用企业识别与官方年报发现**：新增官方证券主数据Provider、单记录新鲜度缓存和动态`CompanyResolver`，精确支持沪深A股代码并拒绝后缀冲突；新增巨潮官方公告年报发现、全文/摘要区分、修订版优先、任务级`S01/S02`登记、PDF首页主体复核及按`entity_id`隔离的下载/解析编排。独立CLI不会初始化LLM、百度或业务Skill。北交所保留路由但首版全称适配未完成；官方网页内部接口无稳定公开契约，失败时只允许显式缓存降级，不猜测URL。
  - [x] **V3.3B 通用财务报表提取**：在V3.3A逐页文本上确定性定位合并资产负债表、利润表和现金流量表，提取16项授信重点科目，统一人民币元、期间和合并口径，保留页码/原文/来源并复用既有指标、比率和风险信号计算；新增企业的数据入口不加载`financial_seeds.json`。非人民币、未绑定主体、金额列歧义和未解释的跨报告冲突会被拒绝或降级，明确“调整后”的较新比较值保留审计记录后优先。
  - [x] **V3.3C 通用基本面与股权提取（离线完成）**：从已完成主体/文件绑定的当前企业年报提取发展历史、主营业务、经营模式、主要产品、前十大股东及明确披露的控制关系；历史召回增量覆盖更名改制、已完成并购重组、业务转型、重大项目投产及里程碑，并排除计划/拟/预计等前瞻事项。复用既有事实/证据模型、股权校验和Graphviz，不读取公司专属Seeds。只披露前十大股东时保留部分结果和`formal_ready=false`，不伪造剩余股东或完整穿透。
  - [x] **V3.3D 现有Agent与Streamlit集成（离线完成）**：新增`--auto-prepare`，把动态任务来源、通用结构化数据和可比年报注入两个既有Skill及完整Agent；Streamlit完整研究使用证券代码启动同一路径，任务目录、进度、Markdown与Word继续复用V3.1/V3.2，不复制Agent Loop。新企业单项研究模式仍属于遗留静态入口。
  - [ ] **V3.3E 跨企业验收（部分完成）**：九州通和工业富联均已从证券代码完成网页Live动态准备、双Skill与外部检索；九州通正常交付六章报告。工业富联正确绑定601138.SH及FY2024/FY2025年报并生成六章、16项尽调问题，但模型在完整首对象后追加占位伪JSON，原在线交付被严格解析器拒绝；现已零网络恢复Markdown/Word，并以“六章完整首对象、无第二个完整对象”为门槛增加保守容错，继续拒绝截断和歧义输出。工业富联股权/财务仍为部分成功，至少一家独立盲测公司及北交所适配尚未完成；完成第一章历史/定位、ownership图形非阻断及15次搜索预算优化后，最新完整离线回归`457 passed in 87.12s`。
- [ ] **V3.4 项目整理**：完善GitHub README、架构图、技术栈与开发过程、Agent/Skill/Tools关系、界面截图、示例报告、已知限制和后续规划；只写真实测出的简历结果。

**V3验收**：V3.1/V3.2继续保证以岭药业界面与Word可复现；V3.3A—D当前实现的生产范围是“通过证券代码接入、官方资料可获取且当前规则可解析的沪深A股非金融企业”。V3.3最终完成仍要求002603、600998、601138及至少一家独立创业板样本无需人工修改`companies.json`或新增公司专属业务Seeds即可进入同一研究流程，并至少完成一个新企业经授权Live；不同企业来源、缓存和事实不得串用。北交所及金融企业在单独适配前不属于已支持范围。

## [ ] V4（未来扩展，不属于当前第一版）
- [ ] 用户上传自有且获得授权的财报/企业介绍/尽调资料，与公开资料交叉核验。
- [ ] 后续扩展用户授权资料、银行/保险/证券等特殊报表行业、港股和美股；按实际需要使用RAG、MCP或更复杂架构。

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

### 2026-10-21 — V1.3 Skill 运行时与确定性 Tool Calling 完成

- 新增 `skills.py`：`SkillRegistry` 只登记两个批准的本地 Skill，保存名称、描述、相对路径、版本、运行状态、Tool 名称及SHA-256；`SkillSelector` 按任务触发词确定性选择，未知和歧义任务显式失败；`SkillLoader` 只读加载并复核目录与哈希。
- 新增 `skill_runtime.py`：按“主体确认→来源校验→Skill选择→Skill加载→Tool调用→结构化结果校验→输出生成”记录 `SkillExecutionTrace`；成功和失败均写入JSON。
- Tool权限：当前仅 `ownership-structure` 可调用白名单中的 `build_ownership_artifacts`；`financial-analysis` 可被选择和加载，但保持 `spec_only`，不会提前执行或伪造财务分析。
- CLI：新增 `--run-skill TASK`、`--skill-source SOURCE_ID` 与 `--skill-trace-output`，继续复用V0下载/PDF解析及V1.2股权构建接口。
- 真实S01验收：任务命中3个股权触发词，加载 `.agents/skills/ownership-structure/SKILL.md` 后实际调用股权Tool；10个制品通过存在性校验，结果为6个展示节点、5条持股边、2项缺失信息；追踪保存为 `examples/V1.3_skill_execution_trace.json`。
- 测试：V1.3注册/选择/加载/运行时/CLI及真实S01集成共新增11项；最终全量回归为 `83 passed`。
- LLM/费用：本阶段未安装或调用OpenAI Agents SDK，未调用LLM API，未产生模型费用。确定性运行时已形成可供后续SDK function tool复用的受控边界。
- 下一阶段：V1.4报告初稿；复用已核实基本面、股权、引用和待核查结果，不提前开发`financial-analysis` Tools。

### 2026-10-22 — V1.3B DeepSeek LLM Agent Runtime 启动

- 路线拆分：原 V1.3 正式标记为已完成的 V1.3A；V1.3B 负责真实 LLM Provider 配置、主 Agent、Agent Loop、Function Calling、Mock/Live 隔离及运行轨迹；V1.4 暂不启动。
- Provider 决策：首个真实模型供应商为 DeepSeek，默认模型 `deepseek-flash`、Base URL `https://api.deepseek.com`、密钥环境变量 `DEEPSEEK_API_KEY`；业务模块不得硬编码或输出密钥。
- 安全边界：默认 Mock；本阶段只建立 Live 能力，不自动发起付费请求。暂时只开放已实现的 `ownership-structure`，`financial-analysis` 保持 `spec_only`。
- 兼容策略：通过 OpenAI Agents SDK 的自定义 OpenAI Chat Completions 模型接入；初期固定关闭 DeepSeek thinking，待验证 `reasoning_content` 多轮传递后再评估推理模式。

### 2026-10-22 — V1.3B 离线实现与验收完成

- 新增 `llm_config.py`：DeepSeek Provider、环境变量/`.env`密钥读取、脱敏配置、关闭thinking、请求超时、有限重试、输出Token限制及 `AsyncOpenAI + OpenAIChatCompletionsModel` 构造。
- 新增 `agent_runtime.py`：主Agent Instructions、SDK Runner、唯一白名单 `run_ownership_research` Function Tool、最大轮次/Tool次数、错误处理、Token使用、Agent执行轨迹及来源/比例后置校验。
- Mock/Live隔离：默认SDK `ScriptedModel`完成两轮Agent Loop且不创建网络客户端；Live同时要求模式、密钥和`--confirm-live`，发送前展示脱敏配置。未执行任何真实DeepSeek请求。
- 真实S01 Mock链路：模型选择Tool后，Registry确认并由Loader只读加载`ownership-structure/SKILL.md`，复用`build_ownership_artifacts`完成比例校验与绘图，再输出带`[S01]`的说明；Agent和Skill轨迹分别落盘。
- 依赖：`openai-agents 0.21.1`、`openai 3.36.0`、`pydantic 2.14.1`、`python-dotenv 1.2.4`；旧领域模型通过`pydantic.v1`兼容命名空间保留既有语义。
- 实际测试：`.venv\Scripts\python.exe -m pytest -q`，结果`100 passed in 5.16s`；另以安装后的CLI完成真实S01缓存端到端Mock演示。下一步仅做一次用户确认的Live兼容性测试，不启动V1.4。

### 2026-10-22 — V1.3B 首轮Live修复与部分验证

- 首次授权调用已连接至响应读取阶段，但因继承自Anaconda的旧Brotli不支持`output_buffer_limit`而失败；没有发生Tool Calling。
- 项目新增`Brotli>=1.2,<2`显式依赖及无网络传输预检；不兼容环境会在创建Live客户端时以明确错误码停止，不再等到读取付费响应时失败。
- 修复后经用户再次授权执行Live：DeepSeek完成2次模型响应，LLM自主调用`run_ownership_research(ticker="002603.SZ", source_id="S01")`；Registry、Loader、`ownership-structure`、`build_ownership_artifacts`、100%比例校验和10个Graphviz/结构化产物均成功。
- 两次响应Usage合计为3,192 Input Tokens、782 Output Tokens、3,974 Total Tokens。运行时现逐次累计Usage，即使最终证据校验失败也保留审计值。
- 最终模型文字自行生成Tool未直接返回的派生比例`23.15%`，被`ungrounded_percentage`后置校验拒绝；Instructions已明确禁止LLM自行加总或推导新比例。
- 新增2项Brotli预检测试和2项5000输出Token边界测试，最终全量离线回归为`104 passed in 9.49s`。V1.3B标记为Live部分验证；未经再次授权不继续付费复测，也不启动V1.4。
- 按用户要求将单次模型响应的可配置输出上限提高到5000，默认仍保持800；该参数不冒充整个Agent Loop的总Token预算。

### 2026-10-22 — V1.3B 事实契约收尾

- Tool结果新增由Python确定性构造的`response_contract`：列明允许模型复述的来源编号、主体、持股比例、分支计算值和控制关系；披露值、计算合计、计算差额和100%校验目标分别标记来源类型。
- 主Agent Instructions明确禁止输出白名单之外的合计比例；后置校验改为直接读取事实契约，并增加明确控制结论的主体与同句来源检查。
- 新增5项边界测试，覆盖`23.15%`这类可算但未登记比例、允许的计算差额、错误控制主体及控制结论缺少匹配来源；全量离线回归为`109 passed in 9.52s`。
- 本次没有发送DeepSeek请求。下一步只能在用户再次明确授权后执行一次Live最终复测，并同时审计Token停止原因和回答完整性；通过后才将V1.3B标记完成并启动V1.4。

### 2026-10-22 — V1.3B 事实契约Live复测

- 经用户明确授权执行一次Live任务；配置为`deepseek-flash`、thinking disabled、最多4轮、1次Tool调用、每次响应最多5000 Token，没有自动重复整项测试。
- DeepSeek完成2次模型响应并自主调用`run_ownership_research(ticker="002603.SZ", source_id="S01")`；Registry、Loader、`ownership-structure`、`build_ownership_artifacts`及10个产物均成功，两个展开分支继续通过100%校验。
- Usage为4,086 Input Tokens、851 Output Tokens、4,937 Total Tokens；第二轮仅输出772 Token，远低于5000上限，本次失败没有Token不足迹象。
- 最终自由文本包含`response_contract`未登记的`5%`，被`ungrounded_percentage`拒绝。由于失败结果未进入正式输出，V1.3B仍为Live部分验证，V1.4继续暂停。
- 下一步不再仅依赖Prompt约束自由文本；优先设计“模型选择事实ID/控制结论ID，Python确定性渲染比例和来源”的结构化最终输出，再经用户授权进行Live验收。

### 2026-10-22 — V1.3B Fact ID离线修复

- 根因审计确认：`5%`既是Skill的小机构展示阈值，也曾出现在Tool返回的缺失信息原因“持股5%以上股东或前10名股东”中；完整`SKILL.md`正文并未发送给模型，最可能的直接诱因是该缺失信息原因被模型复述。因当次被拒绝的完整回复没有保存，原始语境无法证实，此结论保持为高概率判断而非已证实事实。
- Tool压缩结果不再把含阈值的内部`reason`发送给模型；`response_contract`分别标记企业事实、Python计算、完整性校验和Skill规则，5%/25%不会被无条件加入企业持股白名单。
- Agent最终响应收紧为只含Fact ID的JSON；Python验证未知/遗漏ID后确定性渲染股东、比例、控制关系、来源及资料限制，并继续执行二次事实校验。
- 本地轨迹新增脱敏模型候选响应及SHA-256、Tool参数、返回/选择Fact ID、可获得的`finish_reason`、拒绝句和校验规则编号；真实调用仍需另行授权。
- 全量离线回归：`116 passed in 23.02s`，包括阈值原因不进入模型事实载荷及真实S01产物上的Mock Agent集成；本轮未调用DeepSeek。下一步仅在用户明确授权后执行一次受控Live复测。

### 2026-10-22 — V1.3B Fact-Grounded Generation增量实现

- 新增只读统一事实适配层，复用`FactRecord`、`EvidenceRecord`、`SourceRecord`、股权边/控制关系ID与比例校验结果；未创建第二套来源或PDF管线。
- `CitableFact`显式记录公司、主体、指标、值、单位、期间/基准日、来源、定位、计算依赖及核验状态；披露事实、Python计算、未核实候选和资料缺口分型处理。
- 模型响应契约扩展为Fact ID、Missing ID与逐条分析Claim。风险判断必须绑定可交付事实，尽调问题必须绑定资料缺口；模型分析正文不得自行填写比例、年份、来源编号、链接或新的主体事实，具体事实继续由Python渲染。
- 纵深校验新增主体、期间和来源同句匹配；`5%`作为业务阈值与未经核实企业持股分别拒绝，错误分析不会写入正式说明，已通过确定性校验的股权图和JSON仍保留。
- 完整离线回归：`127 passed in 12.96s`；未调用DeepSeek或互联网。新契约具备受控Live复测条件，但V1.3B仍保持“Live部分验证”，等待用户另行授权。

### 2026-10-22 — V1.3B Runtime来源绑定修复与Live复测

- 上一轮Live中模型把`source_id`填写为`official`，在Pydantic边界被拒绝。修复后LLM-visible Tool Schema只包含`ticker`；Python从SourceCatalog及运行上下文绑定来源，复核来源登记、主体、目录元数据和下载产物，并以原始JSON守卫拒绝模型来源覆盖字段。
- 轨迹schema升级至1.3.5，新增`bound_source_id`和`runtime_source_bound`事件；Tool轨迹分别记录`model_arguments`与`runtime_bound`，不把`S01`硬编码进Tool。
- 新增7项离线测试，覆盖不填写source_id、自动绑定S01、绑定其他合法来源、未登记、元数据不匹配、来源主体不匹配、下载来源不匹配以及模型覆盖拒绝；完整回归为`134 passed in 10.42s`。
- 经用户授权执行一次Live：2次模型响应、1次Tool调用；模型只提交`ticker="002603.SZ"`，Runtime绑定`S01`，Skill加载、10个产物和两个分支比例校验均成功。
- Live Usage为7,740 Input、971 Output、8,711 Total Tokens，远低于5000单次输出限制。最终A002使用“控股股东”表述但未绑定`CR001`控制关系Fact ID，被`ANALYSIS-CONTROL-001`拒绝；确定性产物全部保留。V1.3B继续保持Live部分验证，不启动V1.4。

### 2026-10-22 — V1.4 初版Markdown报告完成

- 新增`reporting.py`与`--build-report SOURCE_ID`：复用V1.1基本面构建器、V1.2股权构建器、V0来源/证据及PDF缓存，不复制抽取、来源或绘图管线。
- 报告由Python确定性生成8个章节：企业基本信息、发展历史、主营业务与经营模式、主要产品、股权结构图、主要股东及控制关系、待核查事项、来源及引用附录。
- 报告输入会复核主体、股权基准日、图与比例校验产物的一致性；正文事实经Evidence反查`[Sxx]`，来源附录汇总PDF页码、章节和原文URL。
- 正式SVG/PNG复制至报告`assets/`并使用相对路径嵌入；图未达到`formal_ready`时仍输出局部报告，但不嵌入正式股权图。
- LLM分析改为可选增强：只有成功通过Agent Runtime校验且带合法来源的正文可纳入；未运行或被拒绝时输出明确状态，基础报告继续交付，不关闭现有事实校验。
- 真实以岭药业缓存生成`examples/v1_4_report/CN-A-SZSE-002603/credit_research_report.md`，包含21条基本面事实、5项明确缺失、S01来源附录和股权图；本次未调用DeepSeek。
- V1.4专项报告/CLI/真实产物测试共11项；全量离线回归为`145 passed in 12.53s`。V1.3B的A002/`CR001`绑定问题保留为技术债，未标记为完整验收。

### 2026-10-23 — V1.5 百度基础信息检索离线开发

- 核对百度智能云官方接口：`POST https://qianfan.baidubce.com/v2/ai_search/web_search`，内部统一使用`query/max_results/domain_filter/recency`，映射为官方`messages`、`resource_type_filter`、`search_filter`和`search_recency_filter`。由于官方页面的接口定义与curl示例展示两种头名，程序默认`Authorization`，允许通过`BAIDU_SEARCH_AUTH_HEADER`显式选择，每次只发送一个鉴权头，不自动双发或降级重试。
- 新增`search.py`、`web_sources.py`和`retrieval_runtime.py`：提供搜索Provider/缓存/预算，HTML/PDF获取、SSRF防护、重定向逐跳复核、原文分段，运行时`Sxx`绑定，证据/候选/冲突分型，以及SDK Agent的`search_web → fetch_source → extract_evidence`调用。
- 网页事实继续使用`FactRecord`、`EvidenceRecord`和`SourceRecord`；统一引用目录将V1.5经核验网页事实标记为`verified_external_fact`。第三方页面、无主体匹配、无原文、冲突数值或疑似Prompt Injection不会进入可引用事实。
- CLI新增`--run-web-research`与搜索预算/确认参数。离线Mock端到端轨迹记录三个Tool、Runtime绑定来源、Fact ID、Token和本地搜索计数；Mock的`search_api_requests=0`且本地费用估算为0。
- 本阶段没有发送真实百度或DeepSeek请求。定向V1.5测试为`40 passed in 4.15s`；全量离线回归为`185 passed in 11.29s`。

### 2026-10-23 — V1.5 可信度分层与部分成功策略

- 首次Live完成2次DeepSeek响应和2次百度搜索，百度返回5个真实候选；同花顺页面成功获取109,364字节并解析35个段落，另一候选页面返回403，导致旧逻辑在`extract_evidence`前整体终止。
- `EvidenceStatus`和统一事实目录增量支持`single_source`与`unsupported`：可信官方原始披露保持`verified`；主体匹配的单一第三方原文可形成Fact，但报告必须标注“单一来源，待交叉核实”；未读取原文的搜索摘要只保存为`unverified` Candidate；无来源内容继续拒绝。
- `fetch_source`对403/404、超时及其他受控读取失败不再终止Agent，而是标记来源失败、保存候选及错误码并引导模型继续；存在失败来源、单一来源、候选或冲突时，运行状态为`partial_success`。
- V1.4报告器新增可选V1.5证据输入和确定性分层章节；冲突来源并列展示，不自动选择。示例见`examples/v1_5_partial_report/credibility_tier_report.md`。
- 可信度分层与部分成功改造阶段仅进行离线开发与Mock测试，没有调用DeepSeek或百度API；V1.5定向测试为`43 passed in 4.29s`，含V1.4报告集成的定向测试为`52 passed in 4.40s`，全量离线回归为`189 passed in 11.00s`。

### 2026-10-23 — V1.5 可信度分层后的受控Live复测

- 经用户明确授权只执行一次：DeepSeek 4次模型请求，18,475 Input、902 Output、19,377 Total Tokens；百度3次真实搜索，本地估算0.108元。
- 模型自主调用三个检索Tool；Python抓取并登记S04—S07共4份HTML（含以岭药业官网），并对S05、S06调用证据提取，但没有生成Fact。
- 模型随后尝试第4次搜索，超过`max_supplemental_rounds=2`边界；Python以`supplemental_search_limit_exceeded`停止，没有自动重试，也没有生成最终分析或报告。
- 复测暴露检索Runtime尚未执行通用`--max-tool-calls`总上限，以及验证码页可能被当作普通正文登记的问题。V1.5继续标记为Live部分验证，后续应先离线修复预算一致性、无效正文识别和证据提取召回，再由用户决定是否复测。

### 2026-10-23 — V1.5 最终稳定性离线修复

- 段落召回从少量精确词升级为确定性的研究主题同义词评分，覆盖“核心业务、业务布局、营业收入、产品群、药物、销售模式、营销模式、学术推广”等表达；保存的S05、S06正文离线重放后分别形成5条`single_source`事实。
- 新增验证码、登录限制、Access Denied、反爬URL及正文标记检测。保存的S07被识别为`access_restricted_content`，不登记为有效正文，只保留带原搜索来源的`unverified`候选。
- 搜索轮次、搜索Tool调用、真实API请求、抓取、证据提取、总Function Tool及Agent轮次分别计数；默认上限为10/10/10/20/20/60/70。显式`--max-tool-calls`现在是V1.5总Tool硬上限，旧`--max-supplemental-searches`保留兼容语义。
- Tool预算耗尽不再抛弃已有成果：Runtime停止新增调用，Python保留所有合法Fact/Candidate，拒绝未经校验的模型文字，并确定性输出`success / partial_success / failed`状态及`web_research_report.md`。没有事实和候选时仍输出“资料不足”报告框架。
- Tool载荷缩短为最多3个300字预览段落；证据提取只回传本次新增Fact/Candidate/Conflict，完整网页继续保存在本地并复用缓存。
- V1.5专项测试50项，全量离线回归`196 passed in 13.21s`；示例产物位于`examples/v1_5_stability_offline/`。本轮未调用DeepSeek或百度API，等待用户确认下一次Live测试。

### 2026-10-23 — V2.1/V2.2 可比财务事实与确定性 Tools 完成

- 通过巨潮资讯官方披露真实登记S10（2024年报）和S11（2023年报），并完成PDF下载、SHA-256校验及逐页解析；S10为233页、S11为224页，均无提取失败。V2.1首版事实使用S01/S10形成FY2023—FY2025序列，S11保留为下一轮扩大独立复核的官方来源。
- 新增`financial.py`和`financial_seeds.json`：继续使用`FactRecord`、`EvidenceRecord`、`SourceRecord`和物理页码精确文本锚点，共形成61条单来源财务事实，归一为46条同主体、同期间、同币种、同单位、合并口径事实；FY2024重复值在S01/S10之间自动交叉核对。
- 实现`normalize_financial_items`、`validate_financial_facts`、`calculate_financial_metrics`、`rank_material_changes`以及基础比率/风险信号。同比统一使用`(本期-上期)/abs(上期)*100%`，金额使用`Decimal`；覆盖恰好20%、零基期、负值、正负切换、跨报告冲突及单位/口径冲突。
- `financial-analysis`在确定性`SkillRuntime`中由`spec_only`升级为`ready`，白名单高层Tool为`build_financial_artifacts`；LLM主Agent仍未开放财务Function Tool，避免提前越过V2.3边界。
- 真实以岭药业产物包含46条归一事实、30个逐年变动指标、12个基础比率及7个FY2025量化关注信号；FY2024/FY2025短期借款主表空白没有被擅自解释为零，而是进入明确待核查项。
- 输出`financial_facts.json`、`financial_metrics.json`、`financial_analysis.md`、`financial_risk_flags.json`、`financial_sources.json`、`financial_questions.md`和额外校验文件；示例位于`examples/v2_financial/CN-A-SZSE-002603/`。
- 财务核心定向测试16项通过，新增财务CLI测试后完整离线回归`206 passed in 11.48s`。本阶段未调用DeepSeek或百度API，没有新增付费调用。

### 2026-10-23 — V2.3 第一版专业流程完成（历史实现，正文生成方式已由后续调整取代）

- 新增人工审阅的`financial_explanation_seeds.json`，复用既有Evidence管线精确匹配S01第52、54、58页；经营现金流原因保存为披露事实，营业收入和净利润原因只形成支持性推断。
- 实现`extract_change_explanations`、`assess_reasonableness`和`generate_financial_section`，对7个FY2025重大变动逐项输出金额、变化、已披露原因/待核实假设、合理性、授信关注和尽调问题。
- 原因严格分为`disclosed_reason / supported_inference / unverified_hypothesis`；合理性分为`supported / partially_supported / contradicted / insufficient_evidence`。无直接证据的经营成因不会写回`FactRecord`。
- 新增`financial_explanations.json`与`financial_reasonableness.json`，真实样例形成3条有证据解释、4条待核实假设、7项合理性评价；短期借款空白继续保留为缺失信息。
- 第一版新增只接受ticker的`run_financial_research` Function Tool与`FinancialResearchAgentRuntime`；Python绑定S01/S10、PDF产物和目录，模型只选择结构化ID。真实缓存Mock链路完成2次模型响应、1次Tool调用、Skill 2.3.0加载和9项产物验证。该ID-only输出方式随后被正式正文架构调整取代。
- 修复重大变动风险观察生成的缩进缺陷，确保每个FY2025重大变化均独立形成观察；另保留FY2024利润与现金流方向背离信号，共8条。
- 第一版离线实现当时的财务/Agent/CLI/Skill定向回归为`44 passed in 5.78s`，完整离线回归为`214 passed in 14.35s`；Skill结构校验通过。当时未调用真实DeepSeek或百度，后续受控Live结果及其验收缺口见下一节。

### 2026-10-23 — V2.3 正式报告作者职责调整

- 保留上文已完成的财务事实、确定性计算、原因证据分层、合理性数据、Skill Runtime和Function Tool；不重做V2.1/V2.2。
- 受控DeepSeek Live曾验证2次模型响应和1次`run_financial_research`调用，但模型只返回结构化ID，最终正文仍由Python模板生成，因而不满足“AI研究Agent实际撰写报告”的新验收标准。该结果作为历史测试记录保留，不能视为V2.3最终通过。
- 正式财务报告改为单一路径：DeepSeek获得重点财务事实、指标、附注和来源后撰写完整自然语言段落；ID继续用于引用和事实约束，但不能替代分析文字。
- Python职责收敛为数据获取、计算、证据管理、关键数值/来源/不支持陈述的轻量逐段校验及Markdown机械组装。通过段落保持模型原文；不合格段落标记未完成，不使用固定模板替换。
- 模型调用失败时保留财务事实、指标、图表、数据附录与来源，并明确正式分析尚未完成；Mock只验证流程，不等同于正式报告。离线Mock和完整回归已完成（`227 passed in 12.11s`），并已提供受控Live测试命令，但本轮未执行付费调用。
- V2.4行业与政策研究暂不启动；V2.3需在DeepSeek Live正文通过事实约束后再完成最终验收。
- 后续两次受控正文Live分别暴露运行边界：5000 Output Tokens在第九章以`finish_reason=length`截断；将上限提高至50000并完成`227 passed`回归后，第二轮正文生成改为在默认30秒请求超时处失败。两次均成功完成Tool选择、Skill加载和确定性财务产物构建，但没有形成通过校验的正式正文；下一次动作应先由用户决定是否把单次请求超时提高至120秒并关闭底层自动重试。
- 第三次受控正文Live使用120秒单请求超时、0次SDK自动重试和50000 Output Token上限。两次模型请求均正常结束，第二次约33.19秒且`finish_reason=stop`；总用量为17,722 Input、6,063 Output、23,785 Total Tokens。模型生成完整9章分析和9个尽调问题，Function Tool、Skill加载、S01/S10绑定及确定性产物均成功；但9个章节因内联ID完整性、计算指标`derived_from`引用、多值事实解析及部分推断支持等校验项全部被拒绝。超时与截断问题已排除，当前阻碍转为“模型引用格式与校验契约不匹配，并夹杂少量需实质审查的分析陈述”；Live后完整离线回归为`227 passed in 20.41s`。V2.3继续保持未完成，禁止自动重试。

### 2026-10-23 — V2.3 正式报告事实管理简化（已完成）

- 用户确认统一后续正式报告职责：Python负责资料获取、结构化数据、确定性计算、内部来源保存、图表、最小输出结构解析和Markdown组装；DeepSeek负责专业分析、跨指标判断、风险识别、尽调问题和报告正文。
- 正式报告默认不展示`[Sxx]`、`Fxxx`、`FMxxx`等技术ID，也不要求模型返回这些ID。来源URL、年报文件、页码、原始数据和执行轨迹继续在内部独立保存，用户需要时再导出研究依据文件。
- 取消百分比白名单、正则/关键词推断审查、逐句来源匹配、`derived_from`完整引用和单段失败拒绝整份报告等自然语言交付门禁；完全无依据的具体金额、股权比例或经营事件仍不得编造成企业事实。
- 用户可见可信度只区分“已核验/未核验”：可靠披露和Python确定性计算为已核验；单一来源、尚未确认的外部信息和分析假设为未核验，并使用“可能”“需进一步核实”等措辞。既有细粒度状态可以继续用于内部诊断。
- 本轮优先离线读取第三次Live已保存的`financial_llm_draft.json`，恢复9章正文和9项尽调问题，加入Python财务数据表及“AI生成研究初稿，需人工复核”声明；不得重新调用DeepSeek，不得启动V2.4。
- 已完成离线恢复：最终`financial_analysis.md`包含9章DeepSeek正文、9项尽调问题、核心财务数据表和比率表，技术引用标记为0；原始draft及内部来源记录保持不变。
- 财务定向测试`14 passed in 4.53s`，全量离线回归`223 passed in 13.65s`；本轮未调用任何真实API。详细结果见`V2_3_SIMPLIFICATION_STATUS.md`。

### 2026-10-23 — V2.4 行业与政策研究离线实现与验证完成（Live待授权）

- 范围确认：只建设以LLM研究和写作为核心的行业研究运行时，不新增第三个Skill，不重构V1.5搜索API、V2.3财务分析或两个既有Skill。
- 复用边界：DeepSeek自主决定行业/竞争/政策检索方向并调用V1.5白名单Tools；Python复用搜索预算、URL安全、网页/PDF缓存、SourceCatalog及V2.3确定性财务上下文，负责最小结果解析和Markdown机械组装。
- 离线输出已生成`industry_analysis.md`、`industry_research_context.json`、`industry_sources.json`和`industry_execution_trace.json`；正文不展示技术ID，后台保留来源、原文定位、失败记录、预算和Tool/模型轨迹。
- Live边界：完全离线Mock与回归已经完成。真实DeepSeek和百度搜索各自需要密钥、Live模式及显式确认；在用户单独授权前不调用，不将Mock结果描述为真实行业研究验收。
- 新增`industry_agent_runtime.py`、行业Mock fixture、`--run-industry-research` CLI入口及行业运行时/CLI测试；离线Mock实际执行`search_web → fetch_source → extract_evidence`，复用9项V2.3重点财务指标并生成核心报告、上下文、来源和轨迹产物。
- 实际定向命令：`.venv\Scripts\python.exe -m pytest tests/test_industry_agent_runtime.py tests/test_cli_v24.py -q`，结果`14 passed in 4.73s`；全量命令：`.venv\Scripts\python.exe -m pytest -q`，结果`237 passed in 12.63s`。新增覆盖模型Tool视图隐藏技术ID、搜索发布日期不冒充政策事实期间、财务披露原因/支持性推断分离及预检失败产物。本轮未调用真实DeepSeek或百度API。
- 当前状态：离线实现与验证完成，真实DeepSeek+百度双Live仍待用户单独授权。Live验收通过前不勾选V2.4最终完成，也不启动V2.5；详见`V2_4_STATUS.md`。

### 2026-10-23 — V2.5 完整企业授信研究离线实现与回归完成（Live待授权）

- 用户在V2.4独立Live尚未执行的情况下调整执行顺序：先完成V2.5离线集成，再对完整主Agent链路进行一次受控DeepSeek+百度Live。上节“不启动V2.5”保留为当时决策记录，不代表V2.4已完成真实Live验收。
- V2.5保持一个主Agent和两个既有Skill：完整/综合授信任务规划`ownership-structure`与`financial-analysis`，行业研究继续复用V1.5检索Tools和轻量Instructions，不新增第三个Skill或多Agent架构。
- 主Agent目标流程为：统一主体与来源 → 读取企业基本面 → 模型调用两个Skill高层Tool → 模型按需搜索/读取/提取行业政策资料 → DeepSeek撰写六章正文与跨模块综合风险 → Python插入股权图和财务表并组装Markdown。
- 容错规则为：单个Skill、来源或章节失败时保留其余成果并标记`PARTIAL_SUCCESS`；模型整体失败时保留结构化数据、图表、来源和轨迹并明确报告未完成，不使用Python固定模板补写。
- 计划产物包括`credit_research_report.md`、`credit_research_context.json`、`credit_research_sources.json`、`credit_research_execution_trace.json`、模型原始草稿/交付状态及股权SVG/PNG。正式报告不显示技术ID，后台保留审计信息。
- 离线Mock已在同一根执行轨迹中规划并执行两个Skill，共记录2次Skill调用、9次Tool调用、10次Mock模型请求和1920个Mock Token；执行1次Mock搜索（真实外部请求0次）、3次抓取和3次提取，六章均交付，股权SVG/PNG及财务表已嵌入，报告不显示技术ID。
- 产物位于`examples/v2_5_complete_mock/CN-A-SZSE-002603/`。按“Mock不得冒充Live”规则，结果为`PARTIAL_SUCCESS`；这不表示模块失败，也不代表真实研究质量通过验收。
- 加固项包括：实际`SKILL.md`方法经紧凑`skill_method`进入模型上下文；检索超时/鉴权/参数错误结构化降级；Live成功要求存在证据提取事件及事实/候选；股权图绝对路径不发送模型；单章格式错误不丢弃有效章节；顶层来源记录S01/S10解析路径与`runtime_status=parsed`；CLI明确一次`--confirm-live`任务可能包含多次模型请求。
- 定向命令`.venv\Scripts\python.exe -m pytest tests/test_skills.py tests/test_skill_runtime.py tests/test_agent_runtime.py tests/test_integrated_agent_runtime.py tests/test_cli_v25.py -q --basetemp=.pytest_tmp_v25_targeted`结果为`69 passed in 5.62s`；全量命令`.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp_v25_full`结果为`263 passed in 15.08s`。`compileall`与`git diff --check`通过，后者只有CRLF提示。
- 当前仍保持未勾选：V2.5完整DeepSeek+百度双Live须经用户单独授权并通过后，才能标记最终完成；本节不代表已执行真实DeepSeek或百度调用。

### 2026-10-23 — V2.5 首次完整 DeepSeek + 百度 Live（PARTIAL_SUCCESS）

- 经用户明确授权，仅执行一次完整Live任务；配置为`deepseek-flash`、thinking disabled、最多30轮、40次Tool、单次输出16,000 Tokens、120秒超时、0次模型自动重试，未自动扩大预算或重复调用。
- DeepSeek完成6次真实模型请求，Input 73,745、Output 9,983、Total 83,728 Tokens；前5次`finish_reason=tool_calls`，最后一次`finish_reason=stop`，没有因Token上限截断。
- 模型自主调用`run_ownership_research`与`run_financial_research`；两个实际`SKILL.md`均由Runtime加载，分别执行`build_ownership_artifacts`和`build_financial_artifacts`。股权两分支100%校验、Graphviz SVG/PNG及财务结构化校验全部通过。
- 共执行10次Function Tool调用。百度完成6次真实搜索并得到2个候选URL；对NMPA页面的唯一`fetch_source`返回HTTP 412，第7次搜索意图被搜索预算阻止，未形成有效原文或外部Fact，也未完成有效`extract_evidence`。
- DeepSeek真实撰写六章正文和15个尽调问题；交付记录为`authorship=live_llm`、`python_generated_fallback_analysis=false`、`python_rewrote_llm_text=false`。完整Markdown、股权图、上下文、来源、草稿和根轨迹均已保存。
- 因外部政策原文证据链未完成，运行状态正确降级为`PARTIAL_SUCCESS`，V2.5继续保持未勾选。人工核查另发现流动资产增量归因、国资背景断言、“大单品/长期”等若干过度推断，原始Live报告不得直接作为正式授信审批材料。
- 详细结果、命令、调用量、费用估算和验收矩阵见`V2_5_LIVE_TEST_STATUS.md`；未经用户再次授权不得重跑付费Live。

### 2026-10-23 — V2.5 首次Live问题离线修复

- 根因修复：此前`search_web`子预算耗尽被错误表达为“停止全部Tool”。现在搜索预算耗尽只停止新增搜索，已有候选仍可继续`fetch_source`和`extract_evidence`；只有总Tool预算耗尽才停止全部工具。
- 候选策略：搜索命中后返回一手来源优先的`recommended_fetch_url`，未尝试候选存在时不再发送新的付费搜索；单页403/404/412或解析失败时返回`remaining_candidates`并继续，HTTP 412按访问受限处理且不绕过。
- 检索策略：基础法律和长期政策允许`recency=null`；空结果只逐项放宽时效、域名或关键词，栏目/列表页需继续查找详情原文。
- 写作边界：基本面、股权、财务Tool结果新增`reporting_constraints`，禁止从未知股东残差推断无国资、从产品名单推断大单品集中、把两期数据称为长期、用局部科目概括整体营运资金或无行业基准时称高毛利/领先。货币资金对流动资产增量的贡献率由Python确定性计算为28.92%。
- 协议与展示：`ownership-structure/SKILL.md`第3.6节已与现行LLM正文规则统一；正式Markdown机械规范标题层级，并把后台错误码转换为中文限制，精确诊断仍保留在轨迹。
- 本轮只做离线修复，未调用DeepSeek或百度。专项命令结果`105 passed in 6.79s`，全量回归`275 passed in 15.78s`，`compileall`和`git diff --check`通过。原始Live目录保持不变，修复效果需在用户再次授权的新Live目录中验证。

### 2026-10-26 — V3.1 Streamlit 完整交互界面

- V3.1A完成历史报告安全发现与展示：Markdown支持全文/章节导航，股权图独立渲染，财务表复用现有结构化指标；本机Streamlit健康检查返回`200 / ok`，并人工确认以岭药业恢复版报告、股权图和财务表可见。
- V3.1B完成页面到既有Agent Runtime的真实调用边界：公司、任务和研究模式被转换为固定白名单CLI参数，通过`sys.executable -m credit_research_agent.cli`、`shell=False`执行；没有在Streamlit中复制Agent Loop、Skill选择或报告写作逻辑。
- V3.1C完成独立后台Worker、磁盘状态、刷新恢复、幂等提交、运行中任务锁和跨浏览器会话的项目级原子Live锁；并发时只有一个会话可启动Live，Worker完成、启动/校验失败或陈旧任务收敛时释放锁。Windows使用`OpenProcess/GetExitCodeProcess`只读探测PID，POSIX才使用`os.kill(pid, 0)`，避免误终止Worker；损坏或空锁仅在确认无其他活动Live任务时清理并原子重试。`ui_progress.jsonl`只保存阶段、Tool、轮次和Token等脱敏字段，完整审计继续使用既有ExecutionTrace。断电/崩溃后PID不存在的`running`任务和超过60秒的`queued`任务自动失败收敛，既有产物不删除。
- 完整研究以及股权、财务、行业三个单项Runtime均接入相同脱敏`progress_sink`，页面展示实际模型和Tool事件，不依赖固定假进度。
- V3.1五组专项测试最终为`43 passed in 4.83s`（测试临时目录放在项目可写路径）；`compileall`通过。全量离线回归`.venv\Scripts\python.exe -m pytest --basetemp .pytest_tmp_v31_full_windows -q`结果为`321 passed in 16.03s`。
- 后台Mock端到端任务`v31_mock_e2e_20261026`退出码为`0`，状态按Mock规则为`partial_success`，完成10次Mock模型调用并生成完整六章报告；页面刷新后从磁盘恢复同一任务且未重启Worker。该结果没有任何真实DeepSeek或百度请求，不能冒充Live验收。
- 本阶段没有从Streamlit执行真实DeepSeek或百度调用。真实Live仍需页面确认和Runtime二次门禁；V3.2 Word导出与V3.3项目整理尚未启动。详细说明见`V3_1_STATUS.md`。

### 2026-10-26 — V3.2 Word 报告导出

- 新增`word_export.py`，把现有Markdown中的标题、段落、行内格式、列表、GFM表格及本地图片转换为真正的DOCX结构；A4、中文字体、页边距、行距、页眉页码、表格边框及图片版心宽度统一设置。
- 正文和章节顺序直接来自既有Markdown，不调用DeepSeek、不调用百度、不使用模板重写分析。SVG引用优先复用同名PNG，股权图复用既有Graphviz产物，不重新绘制。
- CLI新增`--export-word`、`--word-output`、`--word-image`纯本地分支；Streamlit在报告展示区新增Markdown/Word并列下载，并按任务目录缓存。Markdown或其中引用的任一本地图表变化时才重新导出，页面刷新或下载不会启动Agent。
- 历史报告默认优先展示最新有效Live任务，无Live时才回退Mock；网页正文与Word导出绑定同一个`report_path`，避免最新Mock遮蔽已完成的V2.5 Live报告。
- UI安全边界保持在任务管理确认的`run_dir`：Markdown、图片及输出DOCX均不得越界；远程图片、缺失图片或无效DOCX明确报错，Word失败不影响Markdown继续查看。
- 真实V2.5恢复报告导出包含1张股权图、2个财务表和48个标题。由于内置LibreOffice不可用，使用本机Microsoft Word 2021转为16页A4 PDF并逐页检查，未见明显溢出、图片变形或中文乱码。
- V3.2专项回归为`31 passed in 5.83s`；完整离线回归为`344 passed in 19.13s`。完整回归因受限沙箱阻断Windows `asyncio`本机loopback socketpair而在获准的离线测试环境运行，测试本身未访问DeepSeek、百度或其他外网。本阶段没有真实付费API请求，没有执行Git Push。

### 2026-10-26 — V3.3A 通用企业识别与官方年报发现

- 完成既有代码审计：确认静态`companies.json`、静态`SourceCatalog`、CLI/UI固定`S01/S10`及三组以岭药业Seeds分别属于V3.3A、V3.3B/C/D的改造边界；本阶段没有删除或迁移旧Seeds。
- 新增`security_master.py`：以巨潮统一代码索引结合深交所公司列表/上交所股票列表核实主体，缓存按单条记录的`retrieved_at`判断新鲜度；支持`fresh/refreshed/stale/static_fallback`并拒绝证券代码后缀、ticker、entity_id或交易所口径冲突。首版支持沪深A股，北交所明确返回未完成适配。
- 新增`annual_reports.py`：从巨潮官方公告查询结果识别年度报告全文，排除摘要、英文版、更正公告、审计报告和非PDF，明确修订版优先；运行时不拼接或猜测PDF URL。每个企业在`<onboarding-dir>/<entity_id>/`保存独立身份、发现缓存、`sources.json`、下载和逐页解析产物，并在PDF前20页复核公司名称、证券代码和报告年度。
- CLI新增纯数据入口`--resolve-company`、`--discover-annual-reports`、`--prepare-company`及缓存/年度数量/刷新参数；这些分支在静态Agent入口前返回，不初始化DeepSeek、百度搜索或Skill Runtime。完整Agent与Streamlit仍维持既有企业白名单，待V3.3B—D完成后再接入新公司。
- 官方只读核查确认002603、600998、601138、300750的证券主数据和年报公告身份；审计样例保存于`examples/v3_3a_official_metadata_samples.json`。因官方页面内部接口没有稳定公开API契约，系统保存来源、缓存和失败信息，遇到验证码/访问限制时不绕过，也不把缓存宣称为最新。
- 新增34项V3.3A离线测试，覆盖四家公司、沪深Provider响应、缓存新鲜/过期/损坏、后缀冲突、带主体前缀/旧年度标题、摘要/修订/主体/URL筛选、任务级来源编号、跨企业目录隔离、PDF主体复核及CLI早返回。定向相关回归`80 passed in 4.38s`；最终完整离线回归在获准的非socket受限测试环境以短临时路径运行，结果`387 passed in 18.32s`。
- 新CLI完成九州通官方只读冒烟：主体解析为`CN-A-SSE-600998`，FY2025/FY2024年报全文登记为任务内`S01/S02`，摘要和窗口外年度按原因排除。首次冒烟发现上交所公告标题包含证券简称前缀；筛选器现只剥离当前已核验主体的全称/简称，不接受任意前缀。随后通过同一正式CLI下载FY2025年报全文`3,368,506`字节并解析`346`页（`345`页有文本、`0`页失败），公司名称、证券代码和报告期校验均为`verified`。本地PDF/逐页文本位于Git忽略缓存；全程未调用DeepSeek、百度或其他付费API，也未执行Git Push。

### 2026-10-26 — V3.3B 通用合并财务报表提取

- 新增`financial_statements.py`，直接消费V3.3A已经下载、解析并绑定主体的官方年报；不联网、不调用LLM，也不读取新增企业的专属Seeds。首版严格定位三张合并主表，排除财务摘要、母公司报表和附注同名行。
- 结构化16项授信重点科目并保留原始科目、物理页码、行号、原文、报告期、原始单位和人民币元换算值；破折号及单边空值保持缺失，负数和括号负数确定性解析。非人民币、未绑定主体、三列金额歧义、期间列无法唯一判断及未解释的跨报告冲突不会被猜测为有效数值。
- 两份年报出现数值冲突时，只有较新年报明确标注比较列“调整后/重述”才采用该值，同时保留原始差异和`restated_comparative_value_selected`审计记录；其他冲突继续由既有财务校验器拒绝。
- 新增`--extract-financial-statements`纯数据CLI和`build_generic_financial_artifacts`接口，输出财务事实、证据、指标、比率、风险信号、校验及数据附录；产物可直接被现有`financial-analysis`上下文读取，但V3.3B不生成或伪装DeepSeek专业分析正文。
- 真实九州通FY2025年报提取结果为`success`：三表齐全，32条标准化期间事实、16项同比指标、8项比率，营业收入、净利润和经营现金流分别与主表原文一致。真实以岭药业不同版式也通过；因短期借款两期主表为空，正确返回`partial_success`且未补零。
- 新增和扩展测试覆盖三表定位、摘要/母表/附注排除、拆行、别名、元/万元、币种证据、负数、缺失、反向年度列、三列歧义、非人民币、来源主体隔离、追溯调整、动态未来期间、CLI早返回及既有Skill上下文兼容。最终完整离线回归为`420 passed in 19.13s`，真实DeepSeek和百度请求均为0；完整测试因Windows Agents SDK socketpair限制在获准的非socket受限离线环境运行。

### 2026-10-27 — V3.3C/D 通用披露提取与完整研究接入

- 新增`fundamental_disclosures.py`、`ownership_disclosures.py`和`company_scope.py`：从当前企业已绑定的官方年报提取基本面、前十大股东及明确控制关系，并以业务/报表证据保守排除当前不支持的金融企业；生产路径不读取公司专属Seeds。
- 前十大股东披露不足100%时只生成明确标记的部分图，保持`formal_ready=false`，不伪造差额股东；中文PDF换行空格会在不破坏英文股东名的前提下修复。任务来源、下载清单、解析摘要和逐页文本同时核对主体、URL、期间及SHA-256，阻止同名`S01`跨企业串用。
- 新增`generic_research.py`及CLI`--auto-prepare`，在不复制Agent Loop的情况下，把动态任务级来源和通用数据接入现有两个Skill、完整主Agent、Streamlit、Markdown及Word链路。完整研究默认任务已去除以岭药业和固定年度；非医药企业检索不再被注入医保、集采等医药专属词。
- 九州通FY2025真实缓存得到7条基本面事实、32条标准化财务事实、16项同比指标、8项比率和10名前十大股东；披露持股合计64.92%，控股股东/实际控制关系来自年报明确披露。离线Mock完整编排生成六章Markdown、财务表和部分股权图，未调用DeepSeek或百度。
- 定向V3.3/V3.5回归为`89 passed in 31.57s`；新增Function Calling畸形JSON恢复、有限重试及完整首对象尾随占位伪JSON的保守恢复测试后，项目完整离线回归在非socket受限测试环境为`449 passed in 75.75s`。受限Windows沙箱只用于编译和文件检查，不再承担Agents SDK相关pytest，避免把环境socket限制误判为代码卡死。
