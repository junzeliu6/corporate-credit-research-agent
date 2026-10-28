# 企业授信研究Agent架构说明

## 1. 设计原则

项目采用“Python 提供数据与工具、Skill 提供专业方法、DeepSeek 负责分析和写作”的职责分工。

- Python 不使用固定模板冒充专业研究正文。
- DeepSeek 不自行创造财务金额、股权比例、公司身份或来源。
- 两个 Skill 必须经 `SkillRegistry`、`SkillLoader` 在运行时真实加载。
- 外部网页和 PDF 被视为不可信输入，不能改变系统指令或触发任意代码执行。
- 单个模块失败时保留其他已完成成果，并根据完成范围返回成功、部分成功或失败。

## 2. 完整研究数据流

```text
Streamlit / CLI
  -> CompanyResolver / 适用范围检查
  -> AnnualReportDiscovery / PDF下载与解析
  -> 通用基本面、股权、财务提取
  -> SkillRegistry + SkillLoader
       -> ownership-structure -> Python核验与绘图
       -> financial-analysis  -> Python数据与指标上下文
  -> DeepSeek主Agent
       -> search_web
       -> fetch_source
       -> extract_evidence
       -> 六章专业正文
  -> Python最小结构解析
  -> 插入股权图与财务表格
  -> Markdown / DOCX
  -> Streamlit展示与历史任务恢复
```

## 3. 关键组件

### 企业与年报准备

- `security_master.py`：解析沪深 A 股代码，核验公司身份并建立缓存。
- `annual_reports.py`：发现年度报告全文，处理摘要、修订版、年度和主体匹配。
- `downloader.py` / `pdf_parser.py`：下载、哈希校验和逐页文本提取。

### 通用披露提取

- `fundamental_disclosures.py`：发展历史、主营业务、经营模式和主要产品。
- `ownership_disclosures.py`：前十大股东、精确持股数、报告期末总股本及可核验控制关系。
- `financial_statements.py`：三张合并财务报表、期间、币种、单位、负数、空值和重述口径。

### Skill 与主 Agent

- `.agents/skills/ownership-structure/SKILL.md`：股权核验和绘图方法。
- `.agents/skills/financial-analysis/SKILL.md`：银行授信视角财务分析方法。
- `skill_registry.py` / `skill_runtime.py`：白名单、路径和哈希校验、Skill 调用追踪。
- `integrated_agent_runtime.py`：一个 DeepSeek 主 Agent 调用两个 Skill 和通用检索 Tools，生成完整报告正文。

完整研究模式对股权数据采用最小披露：模型只接收绘图状态，常规股东名单、持股比例、控制关系、比例完整性和国资性质不进入用户可见正文。图片由 Python 在有效时插入，无图不阻断其他章节。

### 检索与证据

- `search.py`：百度搜索 Provider。
- `web_sources.py`：网页/PDF 原文获取、缓存和安全校验。
- `retrieval_runtime.py`：`search_web`、`fetch_source`、`extract_evidence` 白名单 Tool 与预算。
- `SourceRecord`、`FactRecord`、`EvidenceRecord`：内部来源和证据关联；正式报告默认不显示技术编号。

### 报告与产品界面

- `integrated_agent_runtime.py`：保留 DeepSeek 原始章节并机械组装 Markdown。
- `word_export.py`：将现有 Markdown、表格和图片转换为 Word，不重新调用模型。
- `ui_service.py` / `ui_worker.py`：后台任务、幂等提交和刷新恢复。
- `streamlit_app.py`：Live 确认、进度、报告、股权图、财务表和下载。

## 4. 数据隔离

每项任务按 `task_id` 和稳定 `entity_id` 保存产物。来源编号只在当前任务内绑定，不能把另一家公司同名 `S01` 的文件、财务数据或股东事实复用到当前企业。

生产运行数据写入根目录 `data/`，该目录中的 PDF、网页正文、缓存和任务日志不进入 Git。运行必需的静态配置和 Mock fixture 位于 `src/credit_research_agent/data/`，随包发布。

## 5. 运行边界

- 默认离线测试和 Mock 模式不调用外部 API。
- Live 模式必须检测密钥并取得 DeepSeek 与百度搜索两个独立确认。
- 工具次数、搜索次数、抓取次数、证据提取次数和 Agent 轮次由 Python 强制限制。
- 访问失败、预算耗尽或部分科目缺失时，系统保留已完成模块，不自动扩大预算或重复付费请求。
