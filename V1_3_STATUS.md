# 企业授信研究Agent — V1.3 开发状态恢复报告

> 核查日期：2026-10-22
> 核查范围：项目文档、Git 工作区、V1.2 股权内核、V1.3 Skill 运行时、依赖与配置、离线测试及现有运行产物
> 执行边界：本轮未修改业务代码，未删除或覆盖现有文件，未执行 Git 提交/推送，未联网，未调用任何 LLM API。

## 结论摘要

当前代码没有发现因意外关机造成的语法损坏、JSON/SVG 截断或测试回归。V1.2 股权结构优化仍然完整，持股比例校验与真实 Graphviz `dot` 绘图路径均通过测试。

V1.3 需要按两种口径分别判断：

| 判断口径 | 状态 | 结论 |
| --- | --- | --- |
| 当前 `ROADMAP.md` 定义的“白名单 Skill 注册/确定性选择/只读加载/确定性 Tool Calling/执行追踪” | **已完成并验证** | 代码、Mock 测试、真实 S01 集成测试和执行轨迹均存在且通过。 |
| 本次恢复任务定义的“正式接入 LLM、主 Agent、Agent Loop、模型配置、Agents SDK function tools、LLM 生成有来源分析” | **部分完成** | 可复用的确定性 Skill/Tool 边界已完成；LLM 与 Agents SDK 层尚未实现。 |

因此，不能把当前项目描述为“已经正式接入 LLM 的 V1.3”。更准确的说法是：**V1.3 的确定性运行时底座已完成，LLM 主 Agent 层尚未开发。**

## 1. 原定开发目标与文档口径

### 当前仓库路线

`ROADMAP.md` 将 V1.3 定义为：

1. 白名单 `SkillRegistry`；
2. 确定性 `SkillSelector`；
3. 只读、验哈希的 `SkillLoader`；
4. 调用 V1.2 确定性 Tool；
5. 保存 `SkillExecutionTrace`；
6. 不以 LLM 或付费 API 作为本阶段验收前提。

按这一口径，V1.3 已完成，路线中的下一项是 V1.4 Markdown 报告初稿。

### 本次恢复目标

本次任务进一步要求 V1.3 包含：OpenAI Agents SDK、模型与密钥配置、真实主 Agent、Agent Loop、运行轮次/工具次数约束、SDK Tool Calling、Mock/真实模型隔离，以及由 LLM 生成有来源的分析文字。

按这一口径，V1.3 尚未完成。两种口径需要在继续开发前统一，否则会出现 `ROADMAP.md` 已进入 V1.4、实际却仍在补 V1.3 LLM 层的版本歧义。

### 文档一致性

- `ROADMAP.md`、`README.md` 和 `SKILLS_SPEC.md`：将确定性运行时口径的 V1.3 标记为已完成。
- `PROJECT_BRIEF.md` 页首：仍写着两个 Skill 的 Agent 运行时选择/加载尚未实现，与现有代码和其他文档不一致，属于过期状态说明。
- `.agents/skills/ownership-structure/SKILL.md`：明确说明 `SKILL.md` 不会自动变成 Agents SDK 技能，宿主应用必须显式完成发现、加载和工具注册；现有确定性实现遵守了这一要求。

## 2. 当前项目结构与已确认模块

```text
src/credit_research_agent/
├─ company.py / sources.py              # V0 主体与来源
├─ downloader.py / pdf_parser.py        # V0 下载、缓存、PDF逐页解析
├─ models.py / evidence.py               # V0/V1.1 事实、证据和缺失信息
├─ fundamentals.py                      # V1.1 企业基本面构建
├─ ownership.py                         # V1.2 股权模型、校验、聚合、绘图和产物
├─ skills.py                            # V1.3 白名单、选择、只读加载
├─ skill_runtime.py                     # V1.3 确定性调度、Tool调用和执行追踪
└─ cli.py                               # 统一 CLI 入口
```

现有模块继续复用了 V0 的主体、来源、下载、PDF 和证据链，没有发现第二套重复数据管线。

## 3. V1.2 完整性核查

### 已完成并验证

- `validate_ownership_percentages()`：以单个被持股主体为校验单位，支持默认 `±0.01` 个百分点容差、不足 100%、超过 100%、证据支持的计算差额以及正式出图门禁。
- `aggregate_ownership_holders()`：图层聚合不删除底层原始股东事实。
- `build_ownership_graph()`：保持原始事实与展示图分离，并执行层级、聚合和正式出图校验。
- `ownership_graph_to_dot()`、`render_ownership_graph()`：支持 Graphviz 优先及内部树状降级渲染，箭头方向、节点标签和正式图限制仍在。
- `build_ownership_artifacts()`：继续生成完整 JSON、DOT、SVG/PNG、说明、来源、比例校验和待核查清单。
- 真实以岭药业校验结果仍为：目标公司分支已披露 54.68%，在同一时点/同一口径声明支持下生成 45.32% 的“其他未单列股东”计算节点；以岭医药科技有限公司分支为 100.00%。两个展开节点均通过完整性校验。

### Graphviz 状态

- 已安装：`dot - graphviz version 16.1.0 (20260904.0139)`。
- 将 Graphviz `bin` 显式加入测试进程 `PATH` 后，V1.2 测试为 `22 passed`，证明真实 `dot` 渲染路径可用。
- 现有 `examples/V1.3_skill_execution_trace.json` 记录的是 `internal-tree-fallback`，这是此前受限子进程的实际运行结果；它不代表当前 `dot` 不可用，也没有被伪装成 Graphviz 输出。

结论：V1.2 状态为 **已完成并验证**，无需重做。

## 4. V1.3 功能逐项核查

### 4.1 LLM API 接入

| 检查项 | 状态 | 证据与说明 |
| --- | --- | --- |
| OpenAI Agents SDK 安装与项目依赖 | **尚未实现** | `pyproject.toml` 只有 Pydantic、pdfplumber、Pillow、requests；虚拟环境中 `openai-agents` 与 `openai` 均未安装。 |
| 模型配置模块 | **尚未实现** | 未发现模型名称、客户端、超时、重试、温度或模型选择配置。 |
| 从环境变量读取 API Key | **尚未实现** | 代码未引用 `OPENAI_API_KEY`；本次核查进程也未检测到该变量。未读取或输出任何密钥值。 |
| 基础 LLM 调用 | **尚未实现** | 未发现 `Agent`、`Runner.run`、Responses API 或其他模型调用。 |
| Mock 与真实 API 隔离 | **尚未实现** | 已有的是 Python Tool 的 fake callable 测试，不是模型 Mock/真实 API 双模式。 |

判定基准参考 OpenAI 官方 Agents SDK 快速入门：Python 应用需安装 `openai-agents`、通过环境变量提供密钥、创建 `Agent` 并由 `Runner` 执行；函数工具需要显式注册。当前仓库未具备这些要素。

### 4.2 主 Agent

| 检查项 | 状态 | 证据与说明 |
| --- | --- | --- |
| 主 Agent 对象 | **尚未实现** | `SkillRuntime` 是确定性 Python 编排器，不是 LLM Agent。 |
| Agent Instructions | **部分完成** | `ownership-structure/SKILL.md` 已提供领域规则并能被运行时加载，但没有主 Agent 的系统 instructions 或上下文组装逻辑。 |
| Agent Loop | **尚未实现** | 没有模型推理—工具调用—结果回传—继续推理的循环。 |
| 运行轮次/工具次数限制 | **尚未实现** | 确定性流程只执行一次白名单 Tool；没有面向 Agent Loop 的 max turns/tool-call budget。 |
| 异常处理与运行日志 | **部分完成** | 确定性运行时具有类型化异常、失败轨迹和成功轨迹；LLM/SDK 级异常、超时、重试与 tracing 尚不存在。 |

### 4.3 Skill 运行时集成

| 检查项 | 状态 | 证据与说明 |
| --- | --- | --- |
| `SkillRegistry` | **已完成并验证** | 仅登记项目批准的两个 Skill，并保存路径、版本、状态、Tool 名称和 SHA-256。 |
| `SkillSelector` | **已完成并验证** | 基于触发词做确定性单 Skill 路由；未知任务和多 Skill 歧义显式失败。它不是 LLM 语义路由。 |
| `SkillLoader` | **已完成并验证** | 只读加载、限制在 `.agents/skills/`、限制文本长度、加载前二次验哈希。 |
| 运行时读取 `ownership-structure/SKILL.md` | **已完成并验证** | 当前文件 SHA-256 与真实 S01 执行轨迹中的哈希一致。 |
| 证明 Skill 真正被使用 | **已完成并验证** | 轨迹包含 `skill_selected → skill_loaded → tool_called`；集成测试执行真实加载和实际 Tool，而不是仅由 Codex 开发会话阅读文件。 |

### 4.4 Python Tool Calling

| 检查项 | 状态 | 证据与说明 |
| --- | --- | --- |
| 股权能力进入运行时 Tool 白名单 | **已完成并验证** | `ToolRegistration` 将 `ownership-structure` 映射到现有 `build_ownership_artifacts`。 |
| 复用数据获取、比例校验与绘图 | **已完成并验证** | CLI 先复用 V0 下载/PDF解析；Tool 内部复用 V1.2 构建、100%校验和绘图，没有复制实现。 |
| Tool 调用日志/执行轨迹 | **已完成并验证** | `SkillExecutionTrace` 记录主体、来源、Skill、哈希、Tool、结构校验、产物和错误。 |
| OpenAI Agents SDK function tool | **尚未实现** | 未发现 `@function_tool` 或等效 SDK 工具包装；当前是应用内部的确定性 Python 调用。 |
| 将获取、校验、绘图分别暴露给 LLM | **尚未实现** | 当前只暴露一个高层 `build_ownership_artifacts` 给确定性运行时；这是安全且可复用的边界，但还不是由 LLM 调度的工具集合。 |

### 4.5 端到端任务

任务：“请分析以岭药业（002603.SZ）的股权结构，并生成股权穿透图及简要分析说明。”

| 流程节点 | 状态 |
| --- | --- |
| 识别 `002603.SZ` 上市主体 | **已完成并验证** |
| 校验并复用官方来源 S01 | **已完成并验证** |
| 选择 `ownership-structure` | **已完成并验证**，但由关键词规则而非 LLM 完成 |
| 加载 `SKILL.md` 并验哈希 | **已完成并验证** |
| 调用现有股权 Tool | **已完成并验证** |
| 100% 完整性校验并生成图/JSON/说明 | **已完成并验证** |
| LLM 理解任务并决定调用工具 | **尚未实现** |
| LLM 根据结构化结果生成有来源的分析文字 | **尚未实现**；现有说明由确定性模板生成 |

现有真实轨迹状态为 `succeeded`，共 8 个事件；轨迹引用的 10 个产物当前全部存在。它证明了确定性端到端流程，不证明 LLM Agent 端到端流程。

## 5. 已完成、部分完成和尚未开发清单

### 已完成并验证

- V0 公司识别、来源、下载缓存、PDF 提取和事实/证据接口。
- V1.1 企业基本面结构化事实及真实 S01 集成。
- V1.2 股权事实模型、逐分支 100% 校验、聚合、Graphviz/降级绘图及真实 S01 产物。
- V1.3 确定性 `SkillRegistry`、`SkillSelector`、`SkillLoader`、Tool 白名单、执行轨迹和 CLI 入口。
- `financial-analysis` 只登记为 `spec_only`，无法误执行；本轮没有开发财务 Skill。

### 部分完成

- 主 Agent 的安全边界：确定性 Skill 与 Tool 层已经可直接被未来 Agent 包装，但尚无 LLM Agent。
- Instructions：专业 Skill 规则完整，缺少主 Agent instructions 与运行时上下文拼装。
- 日志与异常：确定性流程完整，缺少模型调用级 tracing、超时和错误处理。
- 端到端：数据和工具链完整，缺少“LLM 理解/调度/总结”三段。

### 已实现但未验证

- 本次没有发现关键功能处于“代码已实现但完全没有测试或运行证据”的状态。

### 尚未实现

- OpenAI Agents SDK 依赖与可选安装方式。
- 模型/API 配置和安全的环境变量读取。
- 主 Agent、Agent Instructions、Runner/Agent Loop。
- 最大运行轮次、最大工具调用次数和模型调用预算。
- Agents SDK function tool 包装。
- LLM Mock 测试、真实 API 显式开关和付费调用保护。
- LLM 基于 Tool 结果生成带来源的最终答复。

### 无法确认

- 无法从文件系统证明意外关机前是否存在尚未保存到磁盘的编辑器缓冲区；只能确认当前落盘代码内部一致、可导入且测试通过。
- 无法确认用户是否在当前进程之外配置过 API Key；只能确认项目没有读取逻辑、当前核查进程未检测到该变量。

## 6. 测试与文件完整性结果

### 实际执行命令与结果

| 命令/检查 | 结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m compileall -q src tests` | `COMPILEALL_OK` |
| `.venv\Scripts\python.exe -m pytest -q` | `83 passed in 1.62s`，0 失败，0 跳过 |
| V0/V1.1 核心模块与真实基本面集成测试子集 | `40 passed in 0.84s` |
| `tests/test_ownership.py` + 真实股权集成测试（默认环境） | `22 passed in 0.70s` |
| 同一 V1.2 测试，显式启用 Graphviz `dot` 和独立临时目录 | `22 passed in 3.49s` |
| `tests/test_skills.py` + `tests/test_skill_runtime.py` + 真实 Skill 集成测试 | `9 passed in 0.64s` |
| `src/.../data` 与 `examples` JSON 解析 | 19 个 JSON，0 个失败 |
| `examples` SVG XML 解析 | 3 个 SVG，0 个失败 |
| `git diff --check` | 无空白错误；仅提示 Windows 下未来可能发生 LF→CRLF 转换 |

第一次在沙箱外启用 Graphviz 时，pytest 尝试访问系统临时目录 `<system-temp>/pytest-of-user`，出现 3 个 setup `PermissionError`（其余 19 项已通过）。将 `--basetemp` 指向已确认可写且原先不存在的独立目录后，22 项全部通过，因此该失败属于测试宿主临时目录权限，不是 V1.2 代码或 Graphviz 功能错误。

### 意外关机残留检查

- 未发现零字节业务文件。
- 未发现 `.tmp`、`.part`、编辑器交换文件或失败产物等可疑残留。
- 项目内只有正常的 `.pytest_cache` 测试缓存。
- 真实执行轨迹引用的 10 个产物全部存在；Skill 文件哈希与轨迹一致。
- 未发现导入错误、依赖导入失败或未闭合的 JSON/SVG。

## 7. Git 状态与代码丢失风险

- 当前分支：`main`
- 最新提交：`8d6782f`（2026-10-21 17:48:36 +08:00）`chore: establish v0.4 baseline with v5 skill specifications`
- 核查开始时：12 个已跟踪文件有修改，Git diff 为 990 行新增、83 行删除。
- 核查开始时：41 个未跟踪实际文件；`git status` 将目录折叠后显示 30 个未跟踪条目。
- 本报告 `V1_3_STATUS.md` 是本轮唯一新增的项目文件，也处于未跟踪状态。

未提交内容按功能包括：

- V1.1：`fundamentals.py`、基本面种子、测试和真实示例；
- V1.2：`ownership.py`、股权种子、测试、SVG/PNG/JSON/Markdown 示例；
- V1.3：`skills.py`、`skill_runtime.py`、单元/集成测试、执行轨迹和独立运行产物；
- 配套文档、CLI、Pydantic 模型和证据接口修改。

风险判断：**代码丢失风险较高，但不是因为当前文件损坏，而是因为从 V1.1 到当前确定性 V1.3 的绝大部分成果都只存在于工作区，最新 Git 提交仍停留在 V0.4 基线。** 已跟踪修改可以看到 diff；未跟踪文件尚不受 Git 历史保护。如果磁盘再次异常，这些文件无法依靠当前提交恢复。

本轮没有执行 `reset`、`clean`、checkout 覆盖、删除、commit 或 push。

## 8. 当前阻碍

1. **里程碑定义冲突**：仓库把“不需要 LLM 的确定性运行时”定义为 V1.3 完成，本次任务则把“正式 LLM Agent”也定义为 V1.3。继续开发前应先选定版本口径。
2. **Git 基线落后**：V1.1—V1.3 确定性实现未提交，继续修改会增大恢复与审阅难度。
3. **LLM 技术层为空**：SDK、配置、Agent、Runner、模型 Mock 和 function tool 都尚未建立，不能通过修改几行现有运行时就宣称完成。
4. **付费验证尚未授权**：离线 Mock 可以先完成；真实 API 冒烟测试必须由用户明确确认模型、预算和调用次数后再执行。

## 9. 建议的恢复顺序

### 优先级 P0：先保护现有成果

经用户确认后，先审阅当前 diff，并为已经通过 83 项测试的 V1.1—V1.3 确定性底座建立本地检查点提交。不要在未提交的大工作区上直接叠加 LLM 层。

### 优先级 P1：统一 V1.3 命名与验收标准

推荐将当前成果明确命名为“V1.3A：确定性 Skill Runtime”，将待开发部分命名为“V1.3B：LLM Agent Runtime”，或直接更新 V1.3 的验收清单并将其重新标记为进行中。完成这一决策前不应开始 V1.4。

### 优先级 P2：下一步最小可执行开发任务

在不产生付费调用的前提下，先完成一个小批次：

1. 增加可选的 Agents SDK 依赖，不影响默认离线安装；
2. 新建模型配置模块，只读取环境变量并验证缺失状态，不记录密钥；
3. 定义 `mock` / `live` 两种运行模式，默认必须是 `mock`；
4. 为配置缺失、禁止意外真实调用和模型选择编写单元测试；
5. 保持现有 `SkillRuntime`、`build_ownership_artifacts` 和全部 83 项测试不变。

完成该最小批次后，再把现有高层股权构建入口包装为 Agents SDK function tool，建立单 Agent instructions、轮次/工具限制和 Mock 端到端测试。最后才在用户单独确认费用后执行一次小规模真实 API 冒烟测试。

## 10. 恢复点建议

建议从“**保护当前已验证工作区 → 统一 V1.3 口径 → 离线建立 LLM 配置与 Mock 边界**”恢复，而不是重写 V1.2、直接进入 V1.4，或立即发起真实模型调用。
