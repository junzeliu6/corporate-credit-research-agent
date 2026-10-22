# V1.3B 开发状态 — DeepSeek LLM Agent Runtime

> 状态日期：2026-10-22
>
> 结论：**离线开发与验收完成；真实 DeepSeek API 尚未调用、兼容性待用户创建密钥后受控验证。**
>
> 基线 Commit：`9815f7c feat: establish v1.3a deterministic skill runtime`

## 1. 本阶段目标与边界

V1.3B 在不重做 V1.2/V1.3A 的前提下，为项目增加第一个模型驱动的单 Agent Runtime：由 LLM 理解股权研究任务、调用受限 Function Tool、在应用运行时真实选择并加载 `ownership-structure` Skill、复用确定性股权分析与绘图，再基于结构化事实和来源生成说明。

本阶段没有开发 `financial-analysis`、V2财务分析、V1.4完整报告、Streamlit、多Agent或MCP。没有执行真实付费LLM请求。

## 2. 已完成并验证

### 2.1 DeepSeek Provider配置

- 文件：`src/credit_research_agent/llm_config.py`
- 默认Provider：`deepseek`
- Base URL：`https://api.deepseek.com`
- 默认模型：`deepseek-flash`
- 密钥：只从`DEEPSEEK_API_KEY`或项目本地`.env`读取。
- `.env.example`只含占位符；`.gitignore`已忽略`.env`和`.env.*`，并保留`.env.example`。
- 默认模式为`mock`，故意不读取密钥，也不创建网络客户端。
- Live模型通过`AsyncOpenAI`和`OpenAIChatCompletionsModel`构造；底层客户端重试关闭，由Agents SDK执行一次有限重试。
- 模型参数包括`parallel_tool_calls=False`、最大输出Token、30秒默认超时、usage收集及`extra_body={"thinking":{"type":"disabled"}}`。
- 密钥使用`SecretStr`并从序列化排除；日志和轨迹只记录`api_key_configured`布尔值。

### 2.2 主Agent与Agent Loop

- 文件：`src/credit_research_agent/agent_runtime.py`
- 使用OpenAI Agents SDK的`Agent`、`Runner`、`RunHooks`和`function_tool`。
- Agent Instructions要求股权任务必须调用Tool，禁止编造股东、比例、实际控制人及来源。
- 默认最大模型轮次4、Tool调用1次、单次输出800 Token；均有Pydantic范围约束。
- 模型请求异常、Skill Runtime异常、无效Tool参数、最大轮次及最大Tool次数均生成机器可读错误码并写入失败轨迹。
- SDK包装的Tool异常会沿异常链恢复本项目原始错误码。
- OpenAI SDK tracing关闭，避免向其他服务发送追踪；本地`AgentExecutionTrace`不保存密钥、完整PDF或完整模型上下文。

### 2.3 Skill与Function Tool调用关系

```text
用户任务
  → SDK模型决定调用 run_ownership_research
  → Tool校验ticker/source_id及调用次数
  → SkillRuntime
     → SkillRegistry确认ownership-structure白名单
     → SkillSelector按任务确认技能
     → SkillLoader只读加载并验SKILL.md哈希
     → build_ownership_artifacts
     → 比例校验、Graphviz/降级绘图、来源与待核查产物
  → Tool压缩为已核实关系/控制关系/校验/来源/缺失信息
  → SDK第二轮模型生成说明
  → Python再次校验回答中的[Sxx]与百分比
```

Function Tool只接收`ticker`和`source_id`；路径、下载结果和解析产物由应用上下文提供。模型没有Python、Shell、任意路径读写或未登记Skill权限。`financial-analysis`没有注册为Agent Tool。

### 2.4 Mock与Live模式

- Mock使用Agents SDK `ScriptedModel`，第一轮产生Function Call，第二轮接收Tool结果并形成确定性说明。
- Mock测试证明即使环境中存在`DEEPSEEK_API_KEY`也不会读取或记录，更不会构造Live客户端。
- Live必须同时满足：`--llm-mode live`、检测到密钥、`--confirm-live`。
- 未加确认时，CLI在任何资料下载或模型请求前显示脱敏配置并退出，允许用户取消。
- usage记录requests/input/output/total tokens；SDK未返回账单金额时`estimated_cost=null`，费用以DeepSeek控制台为准。

## 3. 新增与修改文件

主要新增：

- `.env.example`
- `src/credit_research_agent/llm_config.py`
- `src/credit_research_agent/agent_runtime.py`
- `tests/test_llm_config.py`
- `tests/test_agent_runtime.py`
- `tests/integration/test_yiling_agent_runtime.py`
- `examples/V1.3B_agent_execution_trace.json`
- `examples/V1.3B_skill_execution_trace.json`
- `examples/v1_3b_runtime/`

主要修改：

- `cli.py`：增加`--run-agent`、Mock/Live、安全确认、限制参数及Agent轨迹入口；安装后从当前项目目录安全定位`.agents/skills`。
- `pyproject.toml`：版本1.3.1，增加OpenAI Agents SDK、python-dotenv和Pydantic 2约束。
- 原有领域模型模块仅把Pydantic导入切换为`pydantic.v1`兼容命名空间，保留V0—V1.3A的验证和JSON语义，没有重写业务逻辑。
- `README.md`、`ROADMAP.md`、`PROJECT_BRIEF.md`、`SKILLS_SPEC.md`：同步阶段状态、运行方法与边界。

## 4. 测试与实际结果

### 4.1 全量回归

```powershell
.venv\Scripts\python.exe -m pytest -q
```

实际结果：`100 passed in 5.16s`。

覆盖内容包括：

- 既有83项V0、V1.1、V1.2与V1.3A回归。
- DeepSeek默认配置、`.env`/环境变量、缺失密钥及密钥脱敏。
- `thinking=disabled`、输出Token、超时和有限重试配置。
- Function Tool注册、输入校验、Skill加载、Python Tool调用和执行轨迹。
- Mock Agent两轮Loop、无网络/无密钥、错误实体、重复Tool、最大轮次。
- 最终回答的来源编号及比例关联；未知`[Sxx]`或未核实比例会失败。
- Live未确认时在下载和付费请求前退出。
- 真实S01缓存上的Mock Agent集成。

### 4.2 真实S01 Mock命令行演示

输入任务：

> 请分析以岭药业（002603.SZ）的股权结构，生成股权穿透图，并简要说明其主要股东与实际控制关系。

实际结果：

- 下载命中已校验S01缓存；229页解析成功。
- Agent模型调用2次、Tool调用1次；Mock usage为合成测试值152 Token，不代表实际计费。
- 选择`run_ownership_research`和`ownership-structure`，加载`.agents/skills/ownership-structure/SKILL.md`。
- 实际调用`build_ownership_artifacts`；两个展开分支通过100%完整性校验，生成10个制品。
- 最终说明列示31.53%、20.81%、2.34%、45.32%计算差额及100%完整性，并引用`[S01]`。
- 输出完整股东名册与方框图图像文字两个待核查事项，没有补造信息。

本次命令行环境使用`internal-tree-fallback`渲染；V1.2已另行验证Graphviz路径。该差异不影响比例数据和Skill/Agent调用验收。

## 5. 依赖与环境说明

已安装并核验：

- `openai-agents 0.21.1`
- `openai 3.36.0`
- `pydantic 2.14.1`
- `python-dotenv 1.2.4`
- `Pillow 12.3.0`

现有`.venv`继承了Anaconda系统site-packages，因此`pip check`仍报告若干与本项目无关的Conda/Jupyter旧包冲突；项目声明依赖可导入且全部100项测试通过。全新环境应使用README所示不继承系统包的普通`python -m venv .venv`。

中文路径下editable安装会令某些Python环境按GBK读取UTF-8 `.pth`失败；本机已移除该可重建`.pth`并改用普通wheel安装验证CLI。源码和Git数据未受影响。

## 6. 尚未验证与限制

- 尚未创建或使用项目的DeepSeek API Key。
- 尚未验证DeepSeek真实Tool Calling响应格式、usage字段、错误码、重试和超时行为。
- 尚未验证`deepseek-flash`在当前账户/区域的实际可用性及计费。
- 未启用thinking模式；其`reasoning_content`多轮传递需单独兼容性设计。
- 来源/比例后置校验能拦截最关键的数字和引用错误，但不能替代人工审阅模型文字。
- Live Token限制是请求约束，不是账单绝对硬上限。

## 7. 配置密钥与受控Live测试

复制示例并在本地填写密钥：

```powershell
Copy-Item .env.example .env
# 编辑.env：DEEPSEEK_API_KEY=真实密钥
```

先查看配置并取消（不加确认参数不会请求模型）：

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-agent "请分析以岭药业的股权结构并生成股权穿透图" `
  --agent-source S01 `
  --llm-mode live
```

人工确认脱敏配置后，执行一次小规模Live测试：

```powershell
python -m credit_research_agent "002603.SZ" `
  --run-agent "请分析以岭药业的股权结构并生成股权穿透图" `
  --agent-source S01 `
  --llm-mode live `
  --max-agent-turns 4 `
  --max-tool-calls 1 `
  --max-output-tokens 800 `
  --confirm-live
```

不要把`.env`、密钥或终端中的密钥值提交到Git。完成后核对DeepSeek控制台实际用量与本地轨迹；只有真实调用通过后才能把“DeepSeek兼容性”标记为已验证。

## 8. 下一步建议

1. 用户创建DeepSeek API Key。
2. 先运行不带`--confirm-live`的预览命令，检查Provider、模型和限制。
3. 只运行一次带确认的小规模测试；检查Tool Calling、thinking关闭参数、最终来源、usage及平台账单。
4. 若兼容性通过，补充Live集成结果与脱敏轨迹，再考虑提交V1.3B。
5. V1.3B确认后再启动V1.4报告初稿；不提前开放`financial-analysis`。
