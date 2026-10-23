# V1.4 开发状态 — 初版 Markdown 企业授信研究报告

> 状态日期：2026-10-23
>
> 结论：**V1.4 已完成并通过离线验收。** 系统能够复用 V1.1 基本面事实、V1.2 股权研究产物和 V0 来源证据，使用 Python 确定性生成八章节 Markdown 报告。以岭药业真实缓存案例已生成报告、SVG/PNG 股权图和来源附录；全量离线测试为 `145 passed`。本轮没有调用 DeepSeek，因此没有新增模型费用。
>
> 基线 Commit：`34df4ef feat: add fact-grounded ownership agent runtime`。V1.4 改动当前尚未提交，也未执行 Git Push。

## 1. 本阶段目标与边界

V1.4 的目标是建立一个不依赖 LLM 成功的初版报告交付链路：

```text
企业输入
  → 主体识别与官方来源校验
  → V1.1 企业基本面事实
  → V1.2 股权结构、比例校验和图形
  → Python 确定性报告组装
  → Markdown 正文、股权图、待核查事项和来源附录
```

报告包含以下八个章节：

1. 企业基本信息
2. 企业发展历史
3. 主营业务与经营模式
4. 主要产品
5. 股权结构图
6. 主要股东及实际控制关系
7. 待核查事项
8. 数据来源及引用附录

本阶段没有开发 V2 财务分析、行业政策研究、Streamlit、Word 导出或互联网检索系统，也没有重新实现 V1.1/V1.2 的提取和绘图逻辑。

## 2. 已完成并验证

### 2.1 确定性报告组装

- 新增 `src/credit_research_agent/reporting.py`。
- `MarkdownReportBuilder` 读取并校验既有 `FundamentalResearchBundle`、`OwnershipDataset`、`OwnershipGraph` 和股权比例校验结果。
- `build_report_artifacts` 复用现有 V1.1、V1.2 构建器；`assemble_report_artifacts` 支持从已有研究 JSON 直接生成报告。
- 组装前检查主体、基准日、来源和图中比例是否一致；股权图只有在 `formal_ready` 时才作为正式图嵌入。
- 正文事实通过 Evidence 反查来源，以 `[Sxx]` 标记；来源附录合并标题、发布者、披露日期、报告期、页码、章节定位和原文 URL。
- SVG/PNG 被复制到报告目录的 `assets/`，Markdown 使用相对路径引用，便于整体移动报告目录。
- 文件采用临时文件替换方式写入，避免异常中断留下半写入报告。

### 2.2 LLM 失败降级

- `ValidatedAgentAnalysis` 将可选分析分为 `not_run`、`validated`、`rejected` 三种状态。
- 只有已经通过现有 Agent Runtime 事实校验、且引用合法来源编号的分析正文才能进入正式报告。
- 分析未运行或被拒绝时，报告仍由已验证结构化事实完整生成，并显示相应状态；被拒绝的候选文字不会泄漏到报告。
- V1.3B 的遗留问题继续保留：最新 Live 测试中 A002 控制关系 Claim 未绑定 `CR001`，所以 V1.3B 仍为“Live 部分验证”，没有因 V1.4 完成而被错误标记为通过。

### 2.3 命令行入口

CLI 新增：

```powershell
.venv\Scripts\python.exe -m credit_research_agent 002603.SZ `
  --build-report S01 `
  --fundamentals-dir examples/v1_4_report/research/fundamentals `
  --ownership-dir examples/v1_4_report/research/ownership `
  --report-dir examples/v1_4_report `
  --report-output examples/v1_4_report/CN-A-SZSE-002603/credit_research_report.md
```

该入口继续执行既有下载缓存校验、PDF 解析、基本面提取和股权研究流程，然后生成 V1.4 报告。报告构建失败时返回独立非零退出码并保留明确错误信息。

## 3. 以岭药业实际产物

真实案例使用：

- 主体：石家庄以岭药业股份有限公司（`002603.SZ`）
- 来源：`S01`，2025 年年度报告
- 披露日期：2026-04-28
- PDF：229 页，229 页均成功提取文本
- 企业基本面事实：21 条
- 待核查/缺失信息：5 项
- 报告章节：8 个
- LLM 分析状态：`not_run`

主要产物：

- `examples/v1_4_report/CN-A-SZSE-002603/credit_research_report.md`
- `examples/v1_4_report/CN-A-SZSE-002603/assets/ownership_structure.svg`
- `examples/v1_4_report/CN-A-SZSE-002603/assets/ownership_structure.png`
- `examples/v1_4_report/research/fundamentals/CN-A-SZSE-002603/fundamentals.json`
- `examples/v1_4_report/research/ownership/CN-A-SZSE-002603/ownership.json`
- `examples/v1_4_report/research/ownership/CN-A-SZSE-002603/ownership_validation.json`

报告中的企业事实、持股比例、控制关系和来源均来自已有结构化结果；没有用 LLM 补写未披露信息。尚缺证据的内容进入“待核查事项”。

## 4. 新增和修改文件

主要新增：

- `src/credit_research_agent/reporting.py`
- `tests/test_reporting.py`
- `tests/integration/test_yiling_report.py`
- `examples/v1_4_report/`
- `V1_4_STATUS.md`

主要修改：

- `src/credit_research_agent/cli.py`：增加报告生成参数和执行分支。
- `src/credit_research_agent/__init__.py`：导出报告模块公共接口，版本更新为 1.4.0。
- `pyproject.toml`：项目版本更新为 1.4.0。
- `tests/test_cli.py`：增加报告成功和失败路径测试。
- `README.md`、`ROADMAP.md`、`PROJECT_BRIEF.md`：同步 V1.4 状态、运行方法和项目边界。

## 5. 测试结果

最终全量离线回归命令：

```powershell
.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-v14-final
```

实际结果：

```text
145 passed in 12.63s
```

V1.4 新增覆盖包括：

- 八章节、`[Sxx]` 引用和 Markdown 图片路径。
- 已验证 Agent 分析可纳入报告。
- 被拒绝分析不会阻断报告，也不会泄漏候选文本。
- 未知来源、主体不匹配和非正式股权图被拒绝或安全降级。
- CLI 复用既有研究管线并正确返回错误。
- 真实 S01 研究产物生成以岭药业 Markdown 报告。

测试均为离线执行，没有调用 DeepSeek 或互联网。

## 6. 已知限制

1. V1.3B 最终 LLM 分析的控制关系 Fact ID 绑定仍需后续修复；V1.4 当前通过确定性基础报告规避其单点故障。
2. 当前仅用 S01 2025 年年报完成首个真实案例，尚未形成跨年度或多来源冲突处理演示。
3. V2 尚未启动，因此报告不能据此评价偿债能力、现金流风险、财务趋势或行业政策影响。
4. 当前输出为基础 Markdown；尚未实现 Word 模板、分页、封面和银行正式报告版式。
5. 基本面抽取仍依赖人工审阅的精确锚点种子，尚未扩展为通用上市公司抽取器。

## 7. 下一步建议

V1.4 可以先提交为稳定基线。随后再由用户决定：

1. 先补齐 V1.3B 的控制关系 Fact ID Live 验收；或
2. 按 `ROADMAP.md` 进入 V2.1，先建立 2024/2023 官方年报来源与可比财务事实层。

无论选择哪条路线，都应继续复用 V0 的主体、来源和证据体系，以及 V1.4 的统一报告引用接口，不重建第二套数据管线。
