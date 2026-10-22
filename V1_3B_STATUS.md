# V1.3B 开发状态 — DeepSeek LLM Agent Runtime

> 状态日期：2026-10-22
>
> 结论：**ownership场景的Fact-Grounded与Runtime来源绑定离线修复完成，134项离线回归通过。最新DeepSeek Live已验证模型只提交ticker、Python绑定S01、Function Calling、Skill加载、比例校验和10个产物；最终A002分析Claim提到控股股东但未绑定CR001，被严格事实校验拒绝，因此完整验收仍未通过。**
>
> 当前已保存 Commit：`339b40e feat: add deepseek llm agent runtime`；本次离线收尾改动尚未提交。

## 1. 本阶段目标与边界

V1.3B 在不重做 V1.2/V1.3A 的前提下，为项目增加第一个模型驱动的单 Agent Runtime：由 LLM 理解股权研究任务、调用受限 Function Tool、在应用运行时真实选择并加载 `ownership-structure` Skill、复用确定性股权分析与绘图，再基于结构化事实和来源生成说明。

本阶段没有开发 `financial-analysis`、V2财务分析、V1.4完整报告、Streamlit、多Agent或MCP。2026-10-22 的每次 Live 均由用户单独授权；最新一次用于验证Runtime来源绑定修复，没有自动重试整项任务。

## 2. 已完成并验证

### 2.1 DeepSeek Provider配置

- 文件：`src/credit_research_agent/llm_config.py`
- 默认Provider：`deepseek`
- Base URL：`https://api.deepseek.com`
- 默认模型：`deepseek-flash`
- 密钥：只从`DEEPSEEK_API_KEY`或项目本地`.env`读取。
- `.env.example`只含占位符；`.gitignore`已忽略`.env`和`.env.*`，并保留`.env.example`。
- 默认模式为`mock`，故意不读取密钥，也不创建网络客户端。
- Live模型通过`AsyncOpenAI`和`OpenAIChatCompletionsModel`兼容层构造；审计子类只截获`finish_reason`元数据，不记录原始请求或密钥。底层客户端重试关闭，由Agents SDK执行一次有限重试。
- 模型参数包括`parallel_tool_calls=False`、最大输出Token、30秒默认超时、usage收集及`extra_body={"thinking":{"type":"disabled"}}`。
- 密钥使用`SecretStr`并从序列化排除；日志和轨迹只记录`api_key_configured`布尔值。

### 2.2 主Agent与Agent Loop

- 文件：`src/credit_research_agent/agent_runtime.py`
- 使用OpenAI Agents SDK的`Agent`、`Runner`、`RunHooks`和`function_tool`。
- Agent Instructions要求股权任务必须调用Tool，企业实际比例只能引用Tool事实，禁止把5%/25%绘图阈值当作企业事实；控制结论必须有结构化事实和来源。模型可生成分析，但每条风险判断必须绑定可交付Fact ID，尽调问题必须绑定Missing ID。
- 第二轮模型不得直接生成研究文字，只能返回`{"fact_ids":[...]}`；Python拒绝未知或遗漏ID，再渲染股东、比例、控制关系、来源和资料限制。
- 默认最大模型轮次4、Tool调用1次、单次输出800 Token；均有Pydantic范围约束。
- 模型请求异常、Skill Runtime异常、无效Tool参数、最大轮次及最大Tool次数均生成机器可读错误码并写入失败轨迹。
- SDK包装的Tool异常会沿异常链恢复本项目原始错误码。
- OpenAI SDK tracing关闭，避免向其他服务发送追踪；本地`AgentExecutionTrace`不保存密钥、完整PDF或完整模型上下文，新增脱敏候选响应及SHA-256、Tool参数、返回/选择Fact ID、可获得的`finish_reason`、拒绝句和校验规则编号。

### 2.3 Skill与Function Tool调用关系

```text
用户任务
  → SDK模型决定调用 run_ownership_research
  → Python从SourceCatalog绑定source_id并校验主体/下载产物
  → Tool仅校验模型提交的ticker及调用次数
  → SkillRuntime
     → SkillRegistry确认ownership-structure白名单
     → SkillSelector按任务确认技能
     → SkillLoader只读加载并验SKILL.md哈希
     → build_ownership_artifacts
     → 比例校验、Graphviz/降级绘图、来源与待核查产物
  → Tool将既有事实/证据/股权结果适配为统一可引用事实目录
  → SDK第二轮模型选择Fact/Missing ID并提交绑定依据的分析Claim
  → Python校验ID、状态、主体、期间、来源和Claim类型并渲染说明
  → Python再次校验[Sxx]、比例语义和控制结论
```

Function Tool向LLM只暴露`ticker`；`source_id`、路径、下载结果和解析产物由应用上下文提供。Runtime对SourceCatalog和上下文执行一致性校验，原始JSON守卫拒绝模型提交`source_id`或其他覆盖字段。模型没有Python、Shell、任意路径读写或未登记Skill权限。`financial-analysis`没有注册为Agent Tool。

### 2.4 Mock与Live模式

- Mock使用Agents SDK `ScriptedModel`，第一轮产生Function Call，第二轮接收Tool结果并返回Fact ID、Missing ID和有依据的分析Claim；具体事实、数值、期间和来源始终由Python确定性生成。
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
- `pyproject.toml`：版本1.3.5，增加OpenAI Agents SDK、python-dotenv、Pydantic 2及Brotli兼容约束。
- `src/credit_research_agent/grounding.py`：统一可引用事实模型，以及对现有EvidenceBundle和ownership结果的只读适配器。
- 原有领域模型模块仅把Pydantic导入切换为`pydantic.v1`兼容命名空间，保留V0—V1.3A的验证和JSON语义，没有重写业务逻辑。
- `README.md`、`ROADMAP.md`、`PROJECT_BRIEF.md`、`SKILLS_SPEC.md`：同步阶段状态、运行方法与边界。

## 4. 测试与实际结果

### 4.1 全量回归

```powershell
.venv\Scripts\python.exe -m pytest -q
```

原离线实现结果：`100 passed in 5.16s`。增加Brotli传输预检、失败用量审计及5000输出Token边界测试后为`104 passed in 9.49s`；第一版事实白名单为`109 passed in 9.52s`；Fact ID修复后为`116 passed in 23.02s`；Fact-Grounded增量实现后为`127 passed in 12.96s`。本次Runtime来源绑定修复后的全量复测：`134 passed in 10.42s`。

覆盖内容包括：

- 既有83项V0、V1.1、V1.2与V1.3A回归。
- DeepSeek默认配置、`.env`/环境变量、缺失密钥及密钥脱敏。
- `thinking=disabled`、输出Token、超时和有限重试配置。
- Function Tool注册、输入校验、Skill加载、Python Tool调用和执行轨迹。
- Mock Agent两轮Loop、无网络/无密钥、错误实体、重复Tool、最大轮次。
- Tool确定性构造`response_contract`，把企业披露事实、Python计算值、完整性规则与Skill阈值分开，并为可交付事实提供唯一Fact ID。
- 覆盖5%作为业务规则、5%作为未经证实持股、真实/虚构Fact ID、编造比例、控制结论缺来源、无关百分比，以及确定性最终文本的通过/拒绝场景。
- 新增统一事实目录测试：验证既有`FactRecord`/`EvidenceRecord`/`SourceRecord`可无损适配，股权披露事实、Python差额计算、计算依赖和资料缺口保持分型。
- 新增分析Claim测试：覆盖正确引用披露比例和Python计算、事实支持的风险判断、资料不足的尽调问题、错误年度/主体/来源、未核实网页候选和把5%阈值混入企业事实等场景。
- 测试命令：`.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-full-grounding-tmp`。Windows受限沙箱会阻止`asyncio`创建本地`socketpair`，因此经授权在沙箱外执行；测试本身仍为离线Mock，未调用DeepSeek或互联网。
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
- `Brotli 1.2.0`

现有`.venv`继承了Anaconda系统site-packages，因此`pip check`仍报告若干与本项目无关的Conda/Jupyter旧包冲突；项目声明依赖可导入且全部134项测试通过。全新环境应使用README所示不继承系统包的普通`python -m venv .venv`。

中文路径下editable安装会令某些Python环境按GBK读取UTF-8 `.pth`失败；本机已移除该可重建`.pth`并改用普通wheel安装验证CLI。源码和Git数据未受影响。

## 6. Live验证结果与限制

- 2026-10-22首次Live调用在HTTP响应解压阶段失败：继承自Anaconda的旧Brotli不支持`output_buffer_limit`。项目现已显式依赖`Brotli>=1.2,<2`，并在创建Live客户端前执行不联网的压缩/解压往返预检。
- 修复后第二次Live调用成功连接`deepseek-flash`，完成2次模型响应和1次`run_ownership_research` Function Calling；Registry选择`ownership-structure`，Loader读取对应`SKILL.md`，`build_ownership_artifacts`成功生成10个产物并通过比例校验。
- 两次响应合计Input Tokens 3,192、Output Tokens 782、Total Tokens 3,974。失败轨迹原先只在事件中保留Usage，现已改为逐次累计，后续失败也可直接审计总量。
- 首次最终模型文字包含Tool未直接返回的`23.15%`，很可能是对20.81%和2.34%的自行加总；后置校验以`ungrounded_percentage`拒绝结果。收尾修复随后由Python构造`response_contract`，将允许的比例、计算来源、来源编号和控制关系作为唯一白名单，Instructions和后置校验均以该契约为准。
- 事实契约修复后经用户授权复测：2次模型响应、1次Function Tool，Registry/Loader/Python Tool/10个产物及比例校验全部成功；Usage为4,086 Input、851 Output、4,937 Total Tokens。
- 复测最终文字又包含事实契约中不存在的`5%`，被`ungrounded_percentage`拒绝。第二轮输出772 Token，远低于5000上限，因此没有Token不足迹象；当前失败原因仍是自由文本事实约束，而非输出预算。
- 根因审计发现两个`5%`入口：Skill规定“小机构股东严格低于5%可作为展示聚合候选”，同时旧Tool压缩结果的缺失信息原因写有“年报披露持股5%以上股东或前10名股东”。运行时只加载并记录完整`SKILL.md`，没有把正文发送给LLM；旧Tool结果却会把后一段原因发送给模型，因此它是最可能的直接诱因。由于失败时没有保存模型完整回复，无法确认`5%`的原始句子和语境，不能把该推断写成已证实根因。
- 修复后Tool不再把含阈值的内部缺失原因发送给模型；5%/25%进入独立`percentage_rules`，不进入企业持股Fact ID。旧自由文本校验仍保留为纵深防御，不能被关闭或通过无条件白名单绕过。
- 采用增量Fact-Grounded方案，没有重构V0—V1.3A数据链：复用股权边ID、控制关系ID、`FactRecord`、`EvidenceRecord`、`SourceRecord`与执行轨迹；只新增只读适配层。模型选择ID并提交绑定依据的分析Claim，Python从原有结构化数据渲染事实与来源。
- Runtime来源绑定修复后，LLM-visible Tool Schema只含`ticker`；Python在模型调用前从SourceCatalog绑定并验证来源。轨迹以`model_arguments`和`runtime_bound`分开记录输入，模型提交`source_id`会触发`TOOL-INPUT-001`，不能覆盖上下文。
- 最新Live共2次模型响应和1次Tool调用，Usage为7,740 Input、971 Output、8,711 Total Tokens；finish reasons为`tool_calls, stop`。Runtime绑定S01，Skill与10个确定性产物成功，两个比例分支通过校验。
- 模型候选JSON完整返回11个Fact ID、2个Missing ID和7条分析Claim；A002提到“控股股东”，但支持列表只有持股边及比例校验事实，没有`CR001`，被`ANALYSIS-CONTROL-001`拒绝。该失败是证据绑定不足，不是Token不足，也不是source_id问题。
- 因最终文字未通过证据约束，V1.3B只能标记为Live部分验证，不能声称完整端到端成功。
- 未启用thinking模式；其`reasoning_content`多轮传递需单独兼容性设计。
- 来源/比例后置校验能拦截最关键的数字和引用错误，但不能替代人工审阅模型文字。
- Live Token限制是请求约束，不是账单绝对硬上限。
- `--max-output-tokens`是每次模型响应的输出上限，不是整个Agent Loop的总Token预算；默认800，用户可显式提高到5000。

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
  --max-output-tokens 5000 `
  --confirm-live
```

不要把`.env`、密钥或终端中的密钥值提交到Git。完成后核对DeepSeek控制台实际用量与本地轨迹；只有真实调用通过后才能把“DeepSeek兼容性”标记为已验证。

## 8. 下一步建议

1. 保留各轮Live轨迹，并在DeepSeek控制台核对实际扣费；最新轨迹为`examples/V1.3B_source_bound_live_agent_trace.json`。
2. 已完成Brotli版本约束、传输预检、失败Usage累计，以及Tool→LLM `response_contract`事实白名单和控制关系来源约束。
3. 已完成统一可引用事实接口、Fact/Missing ID选择、有证据分析Claim与Python确定性渲染；V0—V1.3A稳定接口和底层证据链继续复用。
4. 下一步先分析A002为何未绑定`CR001`，仅做Prompt/契约或校验边界的最小修复；下一次Live仍须再次取得用户授权，不得自动调用。
5. 最终输出通过事实契约校验后，才能把V1.3B标记为完整验收并启动V1.4；不提前开放`financial-analysis`。
