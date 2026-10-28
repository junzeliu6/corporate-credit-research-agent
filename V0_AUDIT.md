# V0 开发情况核对（v5 文档整合前置审计）

> 核对日期：2026-10-21。本文只记录现有代码、运行产物和实测结果；本轮未修改 V0 源代码，也未将两份 Skill 误记为已实现。

## 1. 当前仓库

- 本地仓库：`<project-root>`
- Python 包：`corporate-credit-research-agent 0.4.0`；源码包 `credit_research_agent`
- Python：3.11.5
- Git：`main`，当前仓库尚无 commit；现有项目文件均显示为 untracked。本轮未执行 reset、clean、checkout、commit 或其他破坏性/改写历史操作。
- 风险提示：在继续功能开发前，建议由用户确认是否建立一个 V0.4 基线提交；在此之前不得用清理未跟踪文件的命令处理仓库。

## 2. 功能核对

| 功能 | 实际状态 | 现有文件或接口 | 本次验证 |
| --- | --- | --- | --- |
| 以岭药业名称/证券代码解析 | 已完成 | `CompanyResolver.resolve`、`companies.json` | `以岭药业`、法定名称、`002603`、`002603.SZ`映射测试通过；未知/空输入显式失败 |
| 官方来源登记 | 已完成 | `SourceCatalog`、`SourceRecord`、`sources.json` | S01—S04 可离线读取；S01 为 2025 年报官方种子，登记状态与下载状态分离 |
| 公开文档下载与缓存 | 已完成（登记来源范围内） | `OfficialPdfDownloader.download`、`DownloadResult` | 官方域名/HTTPS、重定向、文件头、大小、重试、哈希与失败记录测试通过 |
| PDF 文本提取 | 已完成逐页文本层；表格结构化未实现 | `PdfTextExtractor.parse`、`extract_to_artifacts`、`ParsedPage` | S01 为 229/229 页有文本，0 空页、0 失败页，234,388 个规范化字符 |
| 来源与证据模型 | 已完成身份事实的最小闭环 | `CompanyIdentity`、`SourceRecord`、`FactRecord`、`EvidenceRecord`、`EvidenceBundle` | F/E/S 交叉引用、双哈希与精确行匹配测试通过；当前仅覆盖 4 条身份事实 |
| CLI | 已完成 V0 流程 | `credit-research-agent` / `python -m credit_research_agent` | 识别、列来源、下载、解析、构建证据及失败路径均有离线测试 |
| pytest 离线测试 | 已完成 | `tests/` | `.venv\Scripts\python.exe -m pytest -q`：`41 passed in 0.77s` |

## 3. 真实产物核对

- S01 PDF 存在于忽略目录 `data/cache/`：1,561,343 字节，HTTP 200，`application/pdf`。
- PDF SHA-256：`7ae08fb745ddae05053f5f8a5c262e1df2712b4376bd01c69329c3b8eb557178`。
- `data/parsed/S01/pages.jsonl` 存在，SHA-256 由解析摘要记录为 `cf3f299f21f63bfb1758d96e8f7351c81eb41d01e0131f023626a3bd7b796e06`。
- 轻量可提交样例存在：`examples/S01_extraction_sample.json`、`examples/S01_evidence_sample.json`、`examples/S01_citations.md`。
- 以上只证明下载、文本层、定位和身份事实证据闭环有效；不证明财务表格、股权关系或授信结论已经提取。

## 4. 两个 Skill 的接口兼容性

| V0 能力 | `ownership-structure` 复用方式 | `financial-analysis` 复用方式 | 仍需增量开发 |
| --- | --- | --- | --- |
| `CompanyIdentity` / `CompanyResolver` | 统一目标公司和 `entity_id`，避免把股东/子公司当上市主体 | 统一上市公司合并口径 | 股权主体类型、财务数据期间模型 |
| `SourceCatalog` / `SourceRecord` | 登记年报、权益变动与控制权公告 | 登记 2023—2025 年报、更正公告和附注来源 | 来源发现、更多 `SourceType`、基准日/公告类型字段 |
| `OfficialPdfDownloader` | 下载已登记官方 PDF，复用白名单、缓存和哈希 | 同左 | 当前主要面向年度报告；需在不削弱安全校验的前提下支持更多官方披露类型 |
| `PdfTextExtractor` / `ParsedPage` | 查找股东、控制关系原文并保留物理页码 | 查找三表/附注原文并保留物理页码 | 财务表格行列恢复、坐标/表格抽取、人工复核夹具 |
| `FactRecord` / `EvidenceRecord` / `[Sxx]` | 所有节点、边和控制判断继续引用 E/S 编号 | 所有金额、原因与指标输入继续引用 E/S 编号 | 股权/财务领域模型、冲突与多来源关系、计算记录和推断分级 |
| `EvidenceBuilder` | 可借鉴其双哈希和精确匹配模式 | 可借鉴其确定性证据校验 | 当前由身份 seed 驱动，不能直接承担通用股权或财务抽取 |

兼容性结论：现有接口适合作为共享底座，但不是两个 Skill 的完整输入契约。后续应增量新增领域模型和 `credit_research_agent/tools/`，保留现有 `Sxx/Fxxx/Exxx` 语义及 V0 公共接口；不得复制第二套下载、PDF解析或来源编号系统。

## 5. v5 新增而当前未实现的能力

- `.agents/skills/` 已纳入两份业务规范，但尚无 `SkillRegistry`、选择器、加载器或执行追踪。
- 尚无股权节点/边/控制关系模型、阈值聚合、5 层截断和 Graphviz 渲染 Tool。
- 尚无 2024/2025 可比财务事实层、财务表格结构化、单位/口径校验、同比/比率计算和 20% 重点变化排序 Tool。
- 尚无 Agents SDK/LLM Agent Loop；把 Skill 文件放入仓库不等于应用运行时会调用它。
- 尚无七模块完整报告、行业政策模块、Streamlit 或 Word 导出。

## 6. 建议的下一阶段

下一最小里程碑为 **V1.1 企业基本面事实抽取**：沿用当前下载、页文本、证据和引用链，扩展发展历史、主营业务、经营模式和主要产品的结构化事实。验收重点是每条事实可回溯、缺失不补造，并保持全部 V0 测试通过。

完成 V1.1 后按以下顺序推进：

1. V1.2 `ownership-structure` 领域模型和确定性 Tools；
2. V1.3 `SkillRegistry`、技能选择/加载与调用追踪，再接入所需 Agent SDK；
3. V1.4 股权章节与 Markdown 报告；
4. V2.1 真实可比财务事实层；
5. V2.2 财务计算内核；
6. V2.3 `financial-analysis` Skill 流程；
7. V2.5 双 Skill 集成。

本顺序让股权 Skill 先复用现有文本证据能力，同时给财务 Skill 预留表格结构化和两期可比数据前置条件，避免先生成分析文字再补数据基础。
