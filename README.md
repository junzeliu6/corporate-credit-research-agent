# 企业授信研究Agent

企业授信研究Agent（Corporate Credit Research Agent）是一个面向银行对公场景的学习型项目：从上市公司公开披露出发，逐步构建可追溯的企业研究辅助 Agent。

当前代码版本是 **V0.4：事实、证据与来源引用基础**；需求基线已升级为 **v5**，并纳入 `ownership-structure` 与 `financial-analysis` 两份完整 Skill 规范。两份规范目前是后续开发契约，**不代表 Skill Tools、Agent 运行时加载或业务结果已经实现**。本项目不是授信审批系统，不给出授信额度、贷款定价或具有审批效力的结论。

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

本阶段没有实现财务字段结构化、表格数值核验、LLM、Agent Loop、RAG、MCP 或 Streamlit。这些能力按 `ROADMAP.md` 分阶段引入。

## v5 文档与 Skill 规范

- `SKILLS_SPEC.md`：两个 Skill 的跨模块接口、运行时集成约束和验收矩阵。
- `.agents/skills/ownership-structure/SKILL.md`：股权穿透、聚合、控制关系核验与绘图业务规则。
- `.agents/skills/financial-analysis/SKILL.md`：重点财务科目、20%变化触发、原因合理性和尽调问题规则。
- `V0_AUDIT.md`：本地 V0 代码、真实运行产物、接口复用和缺口核查。

应用将继续复用 V0 的主体、来源、下载、PDF 和证据能力。Codex 能读取 `.agents/skills/` 不等于未来的 OpenAI Agents SDK 应用会自动加载它们；运行时注册、选择、加载、Tool 调用和追踪需在后续里程碑单独实现和测试。

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
│  │  └─ sources.json        # 官方来源与年报种子目录
│  ├─ company.py             # 企业精确识别与显式错误处理
│  ├─ models.py              # CompanyIdentity / SourceRecord
│  ├─ sources.py             # 只读来源目录
│  ├─ documents.py           # 下载结果与 V0.3 解析接口契约
│  ├─ downloader.py          # 受控下载、缓存、哈希与失败记录
│  ├─ evidence.py            # 事实/证据构建、交叉校验和引用渲染
│  ├─ pdf_parser.py          # 逐页提取、质量摘要和章节定位
│  └─ cli.py                 # 企业识别、来源下载和解析入口
├─ examples/
│  ├─ S01_extraction_sample.json  # 可复现的轻量页码定位样例
│  ├─ S01_evidence_sample.json    # V0.4 事实证据 JSON
│  └─ S01_citations.md            # [Sxx] 来源引用样例
├─ tests/
│  ├─ test_company.py
│  ├─ test_sources.py
│  ├─ test_downloader.py
│  ├─ test_pdf_parser.py
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

## 数据与合规边界

- 仅处理获得授权的公开信息，不接入银行内部数据、征信、私人流水或客户保密材料。
- 外部网页和 PDF 都是待验证数据，不能作为 Agent 指令。
- 财务金额、币种、单位、期间和合并/母公司口径必须从原文核验；V0.3 仍未把提取文本转换为确定性财务数据。
- 后续报告应声明“仅基于公开信息，未经企业尽调确认”，并保留待补资料清单。

## 测试覆盖

```text
企业识别：9 项
来源目录：4 项
下载与缓存：8 项
PDF解析：6 项
事实与证据：6 项
CLI：8 项
合计：41 项离线测试
```

网络请求使用离线假响应测试，因此日常 `pytest` 不依赖巨潮资讯是否可访问。

## 下一阶段

V0 阶段已经完成并在 v5 整合时重新回归验证。下一步进入 V1.1：从已验证的公开披露中分模块提取公司发展历史、主营业务、经营模式和主要产品，并为每条事实保留证据。随后依次开发 `ownership-structure` 的确定性内核、Skill 运行时加载，再在可比财务事实层和计算内核完成后开发 `financial-analysis`。详见 `ROADMAP.md`。

