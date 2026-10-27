# V3.3 开发状态

更新时间：2026-10-26  
当前结论：**V3.3A、V3.3B 已完成离线实现与验证；V3.3C—V3.3E 尚未启动。**

## 1. 当前完成范围

V3.3A建立通用A股数据入口：

1. 根据证券代码从官方证券主数据确认上市主体。
2. 从官方披露渠道发现年度报告全文。
3. 为每个企业建立独立的来源目录、下载缓存和PDF解析目录。
4. 复用V0下载及逐页解析能力，并从PDF首页区域二次核对主体。
5. 提供不调用LLM、百度搜索或业务Skill的独立CLI。

V3.3B在该入口上增加通用合并财务报表提取：自动定位三张主表、标准化授信重点科目、保留来源和页码，并把数据接入既有财务计算及`financial-analysis`上下文。新企业财务生产入口不再读取以岭药业`financial_seeds.json`，但旧Seeds仍保留为回归fixture。

当前仍没有自动提取通用基本面和股权，也没有让Streamlit对新企业运行完整报告。这些工作分别属于V3.3C和V3.3D。

## 2. 既有代码审计结论

原系统只支持以岭药业并非单纯的网页限制，而是以下后端依赖共同造成：

- `CompanyResolver`只读取包含以岭药业的`companies.json`。
- 静态`SourceCatalog`及CLI/UI默认使用以岭药业的`S01/S10`。
- `fundamentals_seeds.json`、`ownership_seeds.json`、`financial_seeds.json`均与以岭药业主体和页码证据绑定。
- Agent Runtime会再次核对来源主体，因此不能只在界面层放开代码输入。

本轮只解除前两项中的“主体识别和年报准备”阻塞。旧Seeds保留为回归fixture，未被删除、覆盖或套用到新公司。

## 3. 已实现模块

### 3.1 动态证券主数据

新增`src/credit_research_agent/security_master.py`：

- `OfficialAshareSecurityMasterProvider`：通过巨潮统一代码索引确认代码、证券类型和`orgId`；深市公司全称由巨潮公司列表复核，沪市公司全称由上交所股票列表复核。
- `SecurityMasterRecord`：保存代码、ticker、统一`entity_id`、全称、简称、交易所、板块、上市状态、官方外部ID、来源和获取时间。
- `SecurityMasterCache`：按单条记录的`retrieved_at`判断新鲜度，支持`fresh / refreshed / stale`；刷新其他公司不会使旧记录被误认为最新。
- `DynamicCompanyResolver`：支持六位代码和`.SH/.SZ`后缀；最终身份以官方记录为准，后缀、ticker、代码、`entity_id`冲突时拒绝。
- 旧以岭药业静态目录仅在官方主数据不可用且查询能精确命中时作为显式`static_fallback`；该状态不能用于自动发现新年报。

V3.3A首版正式支持沪深A股。北交所只保留代码路由，因公司全称适配尚未完成而明确拒绝，不通过前缀猜测主体。

### 3.2 官方年报发现与选择

新增`src/credit_research_agent/annual_reports.py`：

- `CninfoAnnualReportProvider`从巨潮官方公告查询响应读取公告身份和PDF路径，不自行拼接静态URL。
- 年报选择只接受年度报告全文及其明确修订版；排除摘要、英文版、更正公告、提示性公告、审计报告、问询函、回复、撤回件和非官方PDF。
- 同一年度明确修订版优先，其他版本保留机器可读排除原因。
- `AnnualReportDiscovery`为每个任务确定性分配`S01/S02...`并写入任务级`sources.json`。这些编号只在当前企业任务内唯一，不是跨任务全局ID。
- `CompanyOnboardingService`把产物写入`<onboarding-root>/<entity_id>/`，不同企业即使都使用`S01.pdf`也不会互相覆盖。
- 下载复用`OfficialPdfDownloader`；解析复用`extract_to_artifacts`。下载后检查PDF前20页是否同时出现公司名称、证券代码及报告年度，不匹配则拒绝。

### 3.3 来源目录与下载边界

- `SourceCatalog`新增显式`from_path()`、`merged()`和原子`write()`，支持任务级来源持久化，同时保持旧静态目录兼容。
- PDF下载白名单增加上交所和北交所官方根域；原有HTTPS、逐跳重定向、大小、PDF文件头、SHA-256、失败记录和缓存复核保持不变。
- `CompanyIdentity`新增可选交易所代码、上市板块、状态、上市日期、官方外部ID、身份来源和解析时间，不破坏旧JSON fixture。

### 3.4 独立CLI

新增：

```powershell
python -m credit_research_agent 600998.SH --resolve-company
python -m credit_research_agent 600998.SH --discover-annual-reports
python -m credit_research_agent 600998.SH --prepare-company --annual-report-count 2
```

相关选项：

- `--security-master-cache`
- `--onboarding-dir`
- `--annual-report-count 1-5`
- `--refresh-official-data`

三个动作在旧静态Agent流程前提前返回，执行轨迹明确记录`external_llm_requests=0`和`external_search_requests=0`。

### 3.5 通用合并财务报表提取

新增`src/credit_research_agent/financial_statements.py`：

- `FinancialStatementExtractor`只消费V3.3A已下载、逐页解析并绑定企业身份的官方年报，不自行联网或调用LLM。
- 严格定位合并资产负债表、合并利润表和合并现金流量表，排除摘要、母公司报表及附注中的同名科目。
- 首版映射16项现有财务Skill重点科目：货币资金、应收账款、预付款项、存货、固定资产、短期借款、应付账款、流动资产、总资产、流动负债、总负债、营业收入、营业成本、净利润、经营活动现金流及销售收现。
- 每行保留原始科目、PDF物理页码、行号、原文、报告期、原始单位和人民币元换算值；币种必须由报表表头或年报记账/编报货币政策核实并保存依据，不能仅凭“元”猜测；破折号和单边空值保持缺失，不能补零。
- 表头年度顺序可反向映射；三列金额、非人民币、期间无法唯一判断或来源未绑定主体时拒绝或降级，不静默猜测。
- 跨年报比较期数值冲突只有在较新年报明确标注“调整后/重述”时才优先较新值，并保留原始差异和机器可读审计记录；其他冲突仍由既有校验器拒绝。

新增`build_generic_financial_artifacts`输出：

- `financial_statement_extraction.json`
- `financial_facts.json`
- `financial_metrics.json`
- `financial_risk_flags.json`
- `financial_sources.json`
- `financial_validation.json`
- `financial_explanations.json`（V3.3B为空并明确附注原因尚未自动提取）
- `financial_reasonableness.json`（V3.3B为空）
- `financial_data_appendix.md`

这些是数据和计算产物，不是Python撰写的专业财务报告。`GenericFinancialArtifacts.skill_context_paths()`已验证能被现有`build_financial_snapshot_from_artifacts`读取；正式DeepSeek分析接入仍留到V3.3D。

CLI新增纯数据动作：

```powershell
python -m credit_research_agent 600998.SH `
  --extract-financial-statements --annual-report-count 1 `
  --security-master-cache data/cache/security_master.json `
  --onboarding-dir data/onboarding
```

该动作在旧Agent入口前返回，明确记录DeepSeek及百度请求均为0。

## 4. 官方只读核查

开发期间只对公开官方页面进行了低频、只读核查，没有调用DeepSeek或百度付费API：

- 巨潮统一代码索引确认002603、600998、601138、300750的代码、简称和`orgId`。
- 巨潮公司列表确认以岭药业和宁德时代的法定全称。
- 上交所股票列表确认九州通、工业富联的法定全称和上市信息。
- 巨潮公告查询确认四家公司存在年度报告全文；工业富联FY2023存在原版和修订版，选择规则会保留修订版。

核查摘要保存在`examples/v3_3a_official_metadata_samples.json`。该文件不是生产缓存，也不根据公告ID猜测PDF URL；九州通正式CLI发现结果的精简副本保存在`examples/v3_3a_jiuzhoutong_discovery.json`。

随后使用正式CLI完成九州通冒烟：

```powershell
.\.venv\Scripts\python.exe -m credit_research_agent 600998.SH `
  --discover-annual-reports --annual-report-count 2 `
  --security-master-cache data/cache/security_master.json `
  --onboarding-dir data/onboarding
```

结果为`discovered`：主体为`CN-A-SSE-600998`，FY2025公告`1225212712`登记为`S01`，FY2024公告`1223384161`登记为`S02`，对应摘要均被排除。首次运行暴露巨潮对上交所公告使用“证券简称+年度报告”的标题格式，已改为只剥离当前已核验主体的法定名称/简称，并增加带前缀及旧式“2020年度报告”测试；任意其他公司前缀仍被拒绝。

随后对同一正式入口执行单年报准备验证：

```powershell
.\.venv\Scripts\python.exe -m credit_research_agent 600998.SH `
  --prepare-company --annual-report-count 1 `
  --security-master-cache data/cache/security_master.json `
  --onboarding-dir data/onboarding
```

结果为`success`：下载FY2025年报全文`3,368,506`字节，SHA-256为`99d384c831b0a794322729be4e29e4849fcade21bb87702685874900d250d613`；解析`346`页，其中`345`页有文本、`0`页失败；前`19`页完成公司名称、证券代码和报告期间三项核对，状态为`verified`。精简结果保存在`examples/v3_3a_jiuzhoutong_prepare_summary.json`。本次只读取官方公开资料，没有调用DeepSeek或百度付费API；下载PDF和逐页文本位于被Git忽略的`data/onboarding/`，不会提交进仓库。

## 5. 测试结果

V3.3A新增34项离线测试，覆盖：

- 002603、600998、601138、300750动态解析。
- 巨潮/深交所与巨潮/上交所Provider响应转换。
- 交易所后缀、ticker和`entity_id`冲突。
- 缓存fresh、stale、corrupt及单记录新鲜度。
- 年报摘要、修订版、更正公告、审计报告、主体不匹配、非PDF和非官方URL。
- 任务级来源编号及跨企业目录隔离。
- PDF公司名称、证券代码和年度复核。
- CLI早返回、参数传递、错误状态以及不初始化LLM/搜索。

实际命令与结果：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_security_master.py tests/test_annual_reports.py tests/test_cli_v33a.py `
  -q --basetemp=bt/v33d
# 34 passed in 4.25s

.\.venv\Scripts\python.exe -m pytest `
  tests/test_company.py tests/test_sources.py tests/test_downloader.py `
  tests/test_pdf_parser.py tests/test_cli.py tests/test_cli_v33a.py `
  tests/test_security_master.py tests/test_annual_reports.py `
  -q --basetemp=bt/v33c
# 80 passed in 4.38s

.\.venv\Scripts\python.exe -m pytest -q --basetemp=bt/v33e
# 387 passed in 18.32s
```

完整回归在受限沙箱内会卡在Agents SDK使用的Windows本机`asyncio` socketpair；获准在非socket受限的离线环境运行后完成。第一次使用过长的pytest临时目录还触发了Windows路径长度问题；改用短目录后387项全部通过。两者均为执行环境问题，不是V3.3A业务失败。

V3.3B新增/扩展测试覆盖：

- 合并三表定位及摘要、母公司报表、附注同名行排除。
- 期间、反向年度列、拆行科目、常见别名、元/万元换算、负数和括号负数。
- 空值不补零、三列金额歧义、非人民币拒绝、来源未绑定/错绑主体拒绝。
- 双年报同值合并、明确追溯调整值优先及未解释冲突继续拒绝。
- 新企业不加载默认Seeds、企业目录隔离、现有指标/比率和Skill上下文兼容。
- CLI参数互斥、早返回、状态码及不初始化LLM/搜索/Skill Runtime。
- 九州通和以岭药业本机真实年报缓存的跨版式集成。

最终完整离线回归：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=bt\v33b_full_final
# 420 passed in 19.13s
```

九州通FY2025真实结果为`success`：三表齐全，32条标准化期间事实、16项同比指标、8项比率；以岭药业不同版式能够提取三表，短期借款两期为空时返回`partial_success`且未补零。精简结果见`examples/v3_3b_jiuzhoutong_financial_summary.json`。所有测试及真实缓存验证均未调用DeepSeek或百度API。

## 6. 当前限制

1. 巨潮和交易所公开页面背后的查询接口没有稳定、版本化的开放API契约；当前实现使用Provider隔离、任务缓存和显式失败，不能保证网站字段永不变化。
2. 名称查询只能命中现有官方缓存或旧静态fixture；首次接入新企业建议输入证券代码。
3. 北交所正式主数据适配未完成。
4. V3.3B只提取授信分析所需的16项核心科目，不声称还原全部财务报表；财务附注中的变化原因、完整审计意见和复杂三列调整表仍需后续处理。
5. 通用基本面、前十大股东及控制关系尚未自动提取；现有完整Agent和Streamlit仍只对已经准备好专属结构化数据的以岭药业开放，放开前必须完成V3.3C/D。
6. 本轮使用本机缓存验证九州通和以岭药业真实年报，PDF及逐页文本保留在被Git忽略的本地目录、不会提交；没有执行新企业DeepSeek/百度Live研究。

## 7. 下一步

下一最小阶段是V3.3C：

1. 在V3.3A准备的`pages.jsonl`上自动定位企业基本情况、发展历史、主营业务、经营模式和主要产品。
2. 自动识别前十大股东、控股股东及实际控制关系披露，复用现有股权模型和Graphviz；只披露前十名时不伪造剩余股东。
3. 对资料不足的穿透关系输出部分结果和待核查事项，不绕过V1.2比例及证据约束。
4. 使用九州通、工业富联及创业板样本进行跨格式离线测试，并保持以岭药业回归。

在V3.3C/D完成前不应执行新企业完整Agent Live测试，也不应把V3.3A+B标记为“任意A股已能生成完整报告”。
