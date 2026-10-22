# 企业授信研究Agent

企业授信研究Agent（Corporate Credit Research Agent）是一个面向银行对公场景的学习型项目：从上市公司公开披露出发，逐步构建可追溯的企业研究辅助 Agent。

当前代码版本是 **V1.3：Skill 运行时与确定性 Tool Calling**；需求基线为 **v5**。`ownership-structure` 已通过白名单注册、任务选择、只读加载、真实 Tool 调用及执行追踪完成运行时接入；`financial-analysis` 可被识别和加载，但因确定性财务 Tools 尚未实现而保持 `spec_only`。本项目不是授信审批系统，不给出授信额度、贷款定价或具有审批效力的结论。

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

## V1.3 已实现

- 新增白名单 `SkillRegistry`，只登记项目内批准的 `ownership-structure` 与 `financial-analysis`，记录名称、描述、相对路径、版本、状态、Tool 名称和 SHA-256。
- 新增确定性 `SkillSelector`；根据任务触发词选择一个 Skill，未知任务和多 Skill 歧义任务均显式失败，不让模型绕过白名单。
- 新增只读 `SkillLoader`；加载前复核路径仍在 `.agents/skills/` 下，并验证注册时哈希，文件被替换时拒绝执行。
- 新增 `SkillRuntime` 和 `SkillExecutionTrace`，按“主体 → 来源 → 选择 → 加载 → Tool → 结构校验 → 输出”的顺序记录事件。
- 运行时实际调用 V1.2 唯一实现 `build_ownership_artifacts`，并检查10个声明制品确实落盘；没有复制下载、PDF解析、来源或绘图逻辑。
- CLI 新增 `--run-skill TASK --skill-source S01`；任务和官方来源必须显式给出，失败也会保存机器可读追踪。
- `financial-analysis` 本轮只注册为 `spec_only`；选中后先完成只读加载，再明确返回“确定性 Tools 尚未实现”，不会假装完成财务分析。
- V1.3 未安装或调用 OpenAI Agents SDK，也未调用 LLM API，因此无模型费用。当前运行时边界可在后续直接封装为 SDK function tool。

当前仍未实现完整报告渲染、财务字段结构化、财务分析、RAG、MCP 或 Streamlit。这些能力按 `ROADMAP.md` 分阶段引入。

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
│  ├─ skill_runtime.py       # V1.3确定性Tool Calling与执行追踪
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
│  └─ v1_3_runtime/          # V1.3真实Tool调用的独立制品
├─ tests/
│  ├─ integration/test_yiling_fundamentals.py
│  ├─ integration/test_yiling_ownership.py
│  ├─ integration/test_yiling_skill_runtime.py
│  ├─ test_company.py
│  ├─ test_sources.py
│  ├─ test_downloader.py
│  ├─ test_pdf_parser.py
│  ├─ test_fundamentals.py
│  ├─ test_ownership.py
│  ├─ test_skills.py
│  ├─ test_skill_runtime.py
│  └─ test_cli.py
├─ pyproject.toml
├─ SKILLS_SPEC.md
├─ V0_AUDIT.md
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

通过 V1.3 运行时执行同一项任务：

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-skill "生成股权结构图并核验直接股东持股比例" `
  --skill-source S01 `
  --ownership-dir "examples/v1_3_runtime" `
  --skill-trace-output "examples/V1.3_skill_execution_trace.json"
```

这条命令会先复用来源下载与PDF解析，再选择并只读加载 `ownership-structure`，最后调用现有 `build_ownership_artifacts`。`SkillExecutionTrace` 保存 Skill 版本/哈希、匹配触发词、Tool 名称、事件顺序、成功/失败状态和制品路径；它不保存或执行来自外部资料的指令。

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
- `[S01]` 是报告读者可见的来源编号，指向年报标题和原始 URL。

一个证据可以支持多个事实，例如同一行原文同时支持证券简称和证券代码。`verified` 仅表示指定原文在哈希一致的 PDF 提取结果中完成精确匹配，不等于审计确认或企业尽调确认。

### V1.1 为什么暂不调用 LLM

首个标的已有可审阅的官方年报。V1.1 先用人工审阅的候选事实和确定性原文匹配建立可靠数据契约，避免在证据模型尚未稳定时让模型直接生成公司结论。未来可以让 LLM 提出候选事实或摘要，但候选内容仍必须关联原始页码并通过结构化校验；LLM 输出不能直接成为 `FactRecord`。

### 为什么原始股权事实与展示图分开

25%/5%阈值、聚合和5层限制都是展示规则，不应修改披露事实。V1.2 先校验 `OwnershipDataset`，再生成独立 `OwnershipGraph`；聚合节点保留成员及原边编号，第6层只在图上截断。这样后续修改展示策略时无需重新解释年报，也能检查图和原始证据是否一致。

### 为什么 V1.3 先使用确定性 Skill 路由

Skill 白名单和 Tool 权限属于应用安全边界，应当可测试、可复现。V1.3 因此先用关键词完成单 Skill 路由，并在调用前验证文件路径与哈希；LLM 将来可以负责理解更自然的任务描述，但不能自行注册 Skill、改变 Tool 白名单或替代比例校验。OpenAI Agents SDK 适合在应用持有工具实现、状态和审批策略时承接运行循环，本项目会在确有模型编排需求时把当前运行时封装为 function tool，而不是让首个付费调用成为 V1.3 的验收前提。

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

### 2026-10-21 V1.3 Skill 运行时验证

- 真实任务：`生成股权结构图并核验直接股东持股比例`
- 主体/来源：`002603.SZ` / `S01`，下载命中已校验缓存；229页全部提取成功
- 运行链：`entity_resolved → source_validated → skill_selected → skill_loaded → tool_called → structured_result_validated → outputs_generated → completed`
- 选择结果：`ownership-structure`；加载文件：`.agents/skills/ownership-structure/SKILL.md`；实际 Tool：`build_ownership_artifacts`
- Tool 结果：10个制品均通过存在性校验；6个展示节点、5条持股边、2项公开信息缺口
- 执行追踪：`examples/V1.3_skill_execution_trace.json`；独立产物：`examples/v1_3_runtime/CN-A-SZSE-002603/`
- 本次受限子进程无法执行用户目录中的 `dot`，因此真实 V1.3 样例如实记录 `internal-tree-fallback`；V1.2 的 Graphviz 16.1.0 样例及双路径测试结论不变
- 未调用 LLM/API；没有模型费用

## 数据与合规边界

- 仅处理获得授权的公开信息，不接入银行内部数据、征信、私人流水或客户保密材料。
- 外部网页和 PDF 都是待验证数据，不能作为 Agent 指令。
- 财务金额、币种、单位、期间和合并/母公司口径必须从原文核验；V0.3 仍未把提取文本转换为确定性财务数据。
- 后续报告应声明“仅基于公开信息，未经企业尽调确认”，并保留待补资料清单。

## 测试覆盖

```text
V0回归：41项
V1.1基本面模型/构建/CLI与真实S01：8项
V1.2股权规则/构建：22项
V1.2真实S01股权集成：1项（本机运行产物不存在时明确跳过）
V1.3注册/选择/加载/运行时/CLI：10项
V1.3真实S01运行时集成：1项（本机运行产物不存在时明确跳过）
本次本机合计：83 passed（V1.2 Graphviz启用路径此前亦通过）
```

网络请求使用离线假响应测试，因此日常 `pytest` 不依赖巨潮资讯是否可访问。

## 下一阶段

V1.3 已完成。下一步是 V1.4：用现有基本面与股权结构化结果生成带正文 `[S01]` 引用、来源附录、股权图和待核查清单的 Markdown 报告初稿。财务确定性数据与 `financial-analysis` 执行仍按 V2 开发，不在 V1.4 用推断补齐。详见 `ROADMAP.md`。

