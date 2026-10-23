# V2.3 财务报告生成机制精简状态

日期：2026-10-23

## 1. 安全检查点

代码清理前已创建本地 Git 检查点：

```text
b47e48f chore: checkpoint v2.3 live narrative baseline
```

本轮未执行 Git Push，也未调用 DeepSeek、百度或其他真实外部 API。

## 2. 本轮目标与结果

本轮将财务报告链路统一为：

```text
Python 获取并整理数据
→ Python 计算金额变动和财务比率
→ financial-analysis Skill 提供银行授信分析方法
→ DeepSeek 撰写完整分析正文和尽调问题
→ Python 解析最小 JSON 文档结构
→ Python 插入数据表并组装 Markdown
```

已取消以下 LLM 正文交付门禁：

- Fact / Metric / Source / Assessment ID 输出契约；
- 正文必须逐一包含声明 ID 的检查；
- `derived_from` 底层事实必须全部出现在正文的检查；
- 百分比白名单、科目关键词和因果关键词正则检查；
- 逐句来源匹配及单段失败导致整份报告拒绝。

Python 内部的 `FactRecord`、`EvidenceRecord`、`SourceRecord`、财务指标 ID 和来源关联仍然保留，用于数据计算、调试及必要时导出研究依据，不再显示在正式报告正文中。

## 3. 精简后的输出约定

DeepSeek 最终只需返回文档结构：

```json
{
  "report_title": "string",
  "sections": [
    {
      "topic": "operating_revenue",
      "heading": "营业收入分析",
      "analysis_markdown": "完整自然语言分析"
    }
  ],
  "due_diligence_questions": [
    {"question": "具体尽调问题"}
  ]
}
```

旧版草稿中的技术 ID 字段由兼容解析器忽略。旧正文中的 `[Fxxx]`、`[FMxxx]`、`[Sxx]` 等标记只在最终 Markdown 展示副本中机械移除，原始 `financial_llm_draft.json` 不变。

用户可见可信度只区分：

- 已核验：Python 数据表、确定性计算及公司明确披露内容；
- 未核验：原因判断、风险情景和专业推断，需进一步核实。

## 4. 已恢复的真实 DeepSeek 报告

在不重新调用 API 的情况下，已从既有草稿恢复：

- 9 个 DeepSeek 财务分析章节；
- 9 个 DeepSeek 尽调问题；
- Python 生成的核心财务数据表；
- Python 生成的主要财务比率表；
- 短期借款 FY2024 / FY2025 明确显示“未取得”，未解释为零；
- 正式报告内技术引用标记数量为 0；
- 未显示冗长来源编号附录；
- 首页保留“AI 生成研究初稿，需人工复核”及已核验/未核验说明。

产物：

- `examples/v2_3_financial_live_narrative_50000_timeout120/CN-A-SZSE-002603/financial_analysis.md`
- `examples/v2_3_financial_live_narrative_50000_timeout120/CN-A-SZSE-002603/financial_questions.md`
- `examples/v2_3_financial_live_narrative_50000_timeout120/CN-A-SZSE-002603/financial_llm_delivery.json`
- `examples/v2_3_financial_live_narrative_50000_timeout120/CN-A-SZSE-002603/financial_llm_reviewed.json`
- `examples/v2_3_financial_live_narrative_50000_timeout120/CN-A-SZSE-002603/financial_llm_reviewed.metadata.json`
- 原始草稿继续保留在同目录的 `financial_llm_draft.json`。

交付清单状态为 `complete`，仅表示必需章节及尽调问题结构完整，不代表逐句事实审计；清单同时记录 `section_count=9`、`question_count=9`、`llm_authored_report=true`、`python_rewrote_llm_text=false`。人工审阅展示副本通过 SHA-256 与原始草稿绑定，避免新草稿误配旧审阅稿；报告标题由 Python 已绑定的企业名称、证券代码和报告期生成。

## 5. 代码与文档变更

主要代码：

- `src/credit_research_agent/financial_agent_runtime.py`
  - 删除复杂自然语言校验器和 ID 交付门禁；
  - 模型上下文不再包含内部技术 ID；
  - 直接组装 DeepSeek 正文；
  - 嵌入 Python 财务数据表和比率表；
  - 新增 `restore_financial_report_from_saved_draft()` 离线恢复入口；
  - 以 `financial_llm_delivery.json` 记录调用与结构交付状态。
- `src/credit_research_agent/financial.py`
  - 更新 LLM 未完成占位说明，不再使用“通过正文校验”的旧措辞；
  - 保留全部确定性财务计算、来源和数据附录能力。
- `src/credit_research_agent/__init__.py`
  - 导出离线报告恢复入口。

规范文档已同步更新：

- `AGENTS.md`
- `PROJECT_BRIEF.md`
- `PRD_PUBLIC_RESEARCH.md`
- `SKILLS_SPEC.md`
- `ROADMAP.md`
- `README.md`
- `.agents/skills/financial-analysis/SKILL.md`

## 6. 离线测试结果

受影响范围测试：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_financial_agent_runtime.py tests/integration/test_yiling_financial_agent_runtime.py tests/integration/test_yiling_financial.py -q -p no:cacheprovider --basetemp .pytest_tmp_v23_20261023g
```

结果：`14 passed in 4.53s`。

全量回归测试：

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp .pytest_tmp_full_20261023c
```

结果：`223 passed in 13.65s`。

本轮测试覆盖：

- Tool Schema 只允许模型填写证券代码；
- Runtime 来源绑定不可由模型覆盖；
- 模型上下文不含 Fact / Metric / Source 等技术 ID；
- 新版最小输出结构解析；
- 带旧 ID 的历史草稿兼容解析；
- 模型原文哨兵未被 Python 模板替换；
- 正式报告不显示技术 ID 或来源编号附录；
- 已保存 Live 草稿的 9 章、9 问完整恢复；
- 人工审阅展示副本与原始草稿 SHA-256 绑定，错误绑定会被明确拒绝；
- 报告标题使用 Runtime 已绑定企业身份，模型返回错误主体不会进入最终标题；
- 正常的 `[FY2025]` 等方括号文本不会被误删；
- 缺少章节或尽调问题时仍交付已有正文，但状态为 `partial`；
- 表格金额和同比来自原有 Python 数据；
- 非法 JSON 时保留数据产物、不生成 Python 专业分析模板；
- V0–V2.3 其他核心功能回归通过。

## 7. 当前限制

- 本报告是 AI 生成研究初稿，不构成银行正式授信意见，仍需人工复核。
- 取消复杂自然语言门禁后，Python 不再尝试判断每个专业推断是否充分；这是本轮有意的职责边界调整。明显未证实的判断应由 DeepSeek使用“可能”“分析判断”“待核实”等措辞，并由人工最终复核。
- FY2024 / FY2025 短期借款主表可比数值仍未取得，有息债务期限结构无法确认。
- 当前正式报告使用人工审阅展示副本，已软化少数过强推断并移除“工具标记”“工具未提供”等技术化措辞；原始 DeepSeek 草稿保持不变，便于追溯。
- 原始草稿仍含历史技术 ID，这是内部诊断记录；最终 Markdown 已隐藏这些标记。
- Python 不再对自然语言正文进行逐句事实审计；交付清单明确记录 `automated_content_fact_audit=not_performed` 和 `human_review_required=true`。无依据金额及事件仍由 Prompt 约束和人工复核防范，这是精简架构的已知边界。
- V2.4 行业和政策分析尚未启动。

## 8. 下一步建议

V2.3 当前已经具备“DeepSeek 正文直出、Python 数据支撑、无模板替代”的完整离线交付链。下一步可在用户确认后进入 V2.4，并沿用同一简化原则：Python 提供检索、数据和来源基础设施，Skill 提供方法，DeepSeek 完成行业与政策分析正文。
