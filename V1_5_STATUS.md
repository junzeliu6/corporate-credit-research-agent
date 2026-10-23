# V1.5 开发状态：百度基础信息检索

> 检查日期：2026-10-23
> 结论：**V1.5最终稳定性修复已通过离线验收。保存的Live正文S05/S06分别形成5条带来源的`single_source`事实，S07验证码页被排除为`unverified`候选；预算耗尽、模型文字拒绝和资料不足均可由Python保留已有成果并生成Markdown报告。两次历史Live仍只算部分验证，修复后的完整Live交付尚待用户确认。**

## 1. 范围与状态

V1.5 在不重构 V0—V1.4 的前提下，增加了三段式检索链路：

`LLM选择检索 → search_web返回候选来源 → fetch_source获取HTML/PDF原文 → extract_evidence登记证据 → LLM选择Fact ID → Python校验与渲染`

当前状态：

- **已完成并验证**：百度 Provider 请求/响应映射、缓存、分项与总Tool预算、HTML/PDF获取、URL安全及验证码检测、同义词段落召回、来源登记、证据分型、三个Function Tool、确定性Markdown兜底、Mock Agent Loop、CLI和脱敏执行轨迹。
- **Live部分验证**：真实百度鉴权和`references`响应、DeepSeek自主选择检索词/网页、官网及第三方网页正文获取、运行时来源登记和证据提取调用均已发生；事实产出、可信度分层选择和最终分析尚未完成端到端验收。
- **部分完成**：V1.5每次运行会生成独立`web_research_report.md`，外部事实也可选汇入V1.4报告并按可信度分层；自动把每次联网结果合并进完整V1.4八章节报告CLI仍待后续接线。
- **未启动**：财务分析Skill、复杂行业政策交叉验证、浏览器自动化。

## 2. 新增与修改的核心文件

- `src/credit_research_agent/search.py`：`SearchProvider`契约、`BaiduSearchProvider`、统一搜索结果、缓存、调用预算、错误映射与费用估算。
- `src/credit_research_agent/web_sources.py`：公网URL/SSRF防护、逐跳重定向复核、限时/限容量获取、HTML段落、PDF页码复用、运行时`Sxx`登记和网页证据分型。
- `src/credit_research_agent/retrieval_runtime.py`：Agents SDK 三个白名单Tool、Mock/Live隔离、两类Live确认、Fact ID选择、分析约束与`RetrievalExecutionTrace`。
- `src/credit_research_agent/data/search_mock_fixture.json`：明确标记的离线测试搜索结果与原文，不代表以岭药业真实披露。
- `src/credit_research_agent/cli.py`：`--run-web-research`、搜索模式/确认开关、搜索/抓取/补充轮次预算与轨迹输出。
- `src/credit_research_agent/models.py`、`grounding.py`：增加网页来源类型与`verified / single_source / unverified / unsupported`状态映射；单一来源使用`external_fact`来源类别，不伪装成`verified_external_fact`。
- `.env.example`、`.gitignore`、`pyproject.toml`、`README.md`、`ROADMAP.md`、`PROJECT_BRIEF.md`、`PRD_PUBLIC_RESEARCH.md`、`SKILLS_SPEC.md`和`ownership-structure/SKILL.md`：配置、安全边界、版本、路线与Skill复用规则。
- `tests/test_search.py`、`test_web_sources.py`、`test_retrieval_runtime.py`、`test_cli_v15.py`：V1.5 的离线单元、安全、预算与端到端验收。

V1.4 仍处于本地未提交变更中；本轮没有删除、重写或回滚其产物。

## 3. 百度 API 配置与官方文档核对

配置项：

```dotenv
SEARCH_PROVIDER=baidu
BAIDU_SEARCH_API_KEY=your_baidu_search_api_key_here
BAIDU_SEARCH_BASE_URL=https://qianfan.baidubce.com/v2/ai_search/web_search
BAIDU_SEARCH_AUTH_HEADER=Authorization
```

- 官方接口：<https://ai.baidu.com/ai-doc/AppBuilder/pmaxd1hvy>
- API Key 说明：<https://ai.baidu.com/ai-doc/AppBuilder/lm68r8e6i>
- 官方计费：<https://ai.baidu.com/ai-doc/AppBuilder/Imca18xc6>

核对结果：当前官方接口为`POST /v2/ai_search/web_search`，使用`messages`、`edition`、`search_source=baidu_search_v2`、`resource_type_filter`、`search_filter.match.site`及`search_recency_filter`；响应的`references`可包含`id/title/url/content/date/type/website`等字段。开发时计费文档列示标准版人民币`0.036元/次`及每日100次免费额度；这些值可变，实际以百度控制台和当时官方文档为准。

鉴权头存在官方页面内部展示不一致：接口定义与通用API Key说明使用`Authorization: Bearer ...`，而curl示例展示`X-Appbuilder-Authorization`。第一版默认`Authorization`，允许用环境变量显式改为后者；单次请求只发送一个鉴权头，不做自动双发或降级重试。首次Live必须根据当前应用控制台的在线调试结果确认头名。

## 4. Agent、Tool 与证据链

- DeepSeek 决定是否搜索、生成搜索词、选择候选URL和最终Fact ID。
- 百度 Provider 只返回候选结果；`snippet`一律标记`search_snippet_unverified`。原文读取失败时可登记为`unverified` Candidate，但不会直接创建Fact。
- `fetch_source`只能读取当次`search_web`返回的URL；Python复核原始及每次重定向的公网地址，限制端口、超时、字节数和转跳次数，不绕过登录、验证码或访问控制。
- `RuntimeSourceRegistry`以既有`SourceCatalog`为种子，去重URL并生成当前任务的`Sxx`。公司官网域名来自当前主体已登记的官网来源，不固定绑定在以岭药业代码里。
- `extract_evidence`只接受Python生成的`source_handle`；模型不能填写或覆盖`source_id`。原文段落/PDF页码进入`EvidenceRecord`：可信官方来源形成`verified` Fact，主体匹配的单一第三方原文形成带限制标识的`single_source` Fact；其他内容保留为候选或冲突。
- V1.5 证据包经现有`build_evidence_bundle_grounding_catalog`转换为`CitableFact`，包含公司/主体、指标、值、期间、来源类型、原文定位、URL、证据ID和`verified_external_fact`来源分类。
- 模型最终只选择已登记Fact/Candidate ID；分析Claim必须绑定Fact ID且不得自行填数字、比例、URL或`[Sxx]`。Python负责渲染事实值和来源。

## 5. 预算与安全保护

默认单任务上限：

- 真实搜索 API 请求：3次；
- 每次搜索结果：5条；
- HTML/PDF 获取：5个；
- 证据提取：5次；
- 首轮之后的补充检索：2轮。

限制由Python Runtime执行并写入轨迹。鉴权失败、限流和超时也在请求发出前消耗本地搜索预算，防止失败请求无限重复。本地计数和`0.036元/次`估算只是保护与诊断，不是百度账单的硬上限。

## 6. Mock 执行轨迹

样例：`examples/v1_5_mock/retrieval_execution_trace.json`

- 任务：搜索以岭药业官网等资料，补充主营业务、主要产品和经营模式。
- 模型：SDK `ScriptedModel`，4次Mock响应，没有DeepSeek网络请求。
- Tool顺序：`search_web → fetch_source → extract_evidence`。
- 搜索词：`以岭药业 主营业务 主要产品 经营模式 官网`。
- Runtime绑定：`DOC001 → S05`。
- 返回/选择：`F801`、`F802`。内容明确标记为离线Mock，不得进入真实报告。
- Mock Token：110 Input / 60 Output / 170 Total，仅是脚本模型测试计数。
- 真实搜索API请求：0；本地估算费用：0元。

## 7. 测试结果

定向V1.5测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests/test_search.py tests/test_web_sources.py `
  tests/test_retrieval_runtime.py tests/test_cli_v15.py `
  --basetemp .pytest-v15-trace
```

首次离线实现结果：`40 passed in 4.15s`。可信度分层与部分成功增量为`43 passed in 4.29s`；最终稳定性修复后的V1.5专项结果为`50 passed`，连同报告模块的定向结果为`59 passed in 4.98s`。

全量离线回归：

```powershell
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pytest -q -x `
  --basetemp .pytest-v15-debug-full
```

首次离线实现结果：`185 passed in 11.29s`。可信度分层与部分成功增量为`189 passed in 11.00s`；最终稳定性修复后的最新结果为`196 passed in 13.21s`。最新回归没有失败项或真实外部请求。Windows默认系统Temp目录及过长临时路径在本机存在权限/路径异常，因此最终命令使用仓库内短路径`--basetemp=bt`；该环境问题不是代码断言失败。

覆盖范围包括：请求参数和单鉴权头、响应转换/去重/空结果、缺密钥、超时/鉴权/限流、失败请求预算、HTML/PDF原文与缓存、URL/重定向安全、页码、来源去重、Fact/Evidence关联、无原文、冲突、Prompt Injection、Tool参数/来源覆盖拒绝、轮次/搜索/抓取预算和CLI Mock端到端。

## 8. 已知限制

- 百度当前帐号的`Authorization`鉴权和`references`响应已经Live验证；Mock仍不能代替对事实产出和最终报告的Live验收。
- HTML解析为轻量静态正文提取，不执行JavaScript，不绕过验证码、登录、付费墙或反爬限制。
- URL在发起请求前会解析DNS并拒绝任何非公网地址，但`requests`的实际连接层未锁定预解析IP；对高对抗环境仍存在DNS rebinding/TOCTOU剩余风险。当前用途是受控公开资料研究，如部署到服务端应再加网络层egress防护。
- 首版“证据充分”主要依赖主体匹配、可靠来源层级、原文定位和问题关键词；它不替代人工尽调、多来源交叉验证或公告更正检查。
- 一个研究问题的候选段落暂共用同一`metric`前缀；财务数值及复杂口径仍由未来V2专用模型处理。
- V1.3B 的A002/`CR001`控制关系Claim绑定问题仍是技术债，V1.5没有宣称它已修复。

## 9. 下一步受控 Live 测试

先在`.env`中配置`BAIDU_SEARCH_API_KEY`；如同时测试DeepSeek自主选择，也需已配置`DEEPSEEK_API_KEY`。不要在命令行直接写密钥。

建议命令：

```powershell
.\.venv\Scripts\python.exe -m credit_research_agent "002603.SZ" `
  --run-web-research "请搜索以岭药业的官方网站及其他可信公开资料，补充其主营业务、主要产品和经营模式信息，并提供来源。" `
  --llm-mode live --confirm-live `
  --search-mode live --confirm-search-live `
  --max-agent-turns 70 --max-tool-calls 60 --max-output-tokens 2000 `
  --max-search-rounds 10 --max-search-calls 10 `
  --max-search-requests 10 --max-search-results 5 `
  --max-source-fetches 20 --max-extract-calls 20 `
  --web-research-dir "data/web_research"
```

执行前应先去掉两个`--confirm-*`查看脱敏配置并确认程序停止；再在用户明确授权下只执行一次。首次Live重点验证：鉴权头、`references`字段、搜索次数、官网原文获取、运行时`Sxx`、Fact/Evidence定位、候选/冲突分型、DeepSeek是否真正自主调用三个Tool，以及最终文字是否只使用通过校验的Fact ID。

## 事实可信度分层与容错增量（2026-10-23）

首次Live测试实际完成了DeepSeek自主调用`search_web`、百度返回真实结果、同花顺网页原文获取和S06来源登记；同时另一个候选页面返回403。旧实现因单个Tool异常终止整项任务，因此没有继续调用`extract_evidence`或生成最终分析。

本轮采用最小增量修复：

- `verified`：可信原始披露或确定性计算结果，正常输出并引用来源。
- `single_source`：已读取、可定位且主体匹配，但只有一个第三方原文；允许进入报告，并标注“单一来源，待交叉核实”。
- `unverified`：仅有搜索摘要或原文读取失败；保存为Candidate，以“某来源称”方式单列，不能作为确定性事实。
- `unsupported`：无来源或模型自行创造的内容；继续拒绝，不进入报告。

`fetch_source`现在将受控的403/404、超时、正文不足等失败记录为来源失败和未核验Candidate，再引导模型尝试其他结果。只要仍有可交付事实或候选，运行可返回`partial_success`。冲突来源继续并列展示，疑似网页指令继续隔离，未关闭任何事实校验器。

V1.4报告器增加可选`web_evidence_path`输入和可信度分层章节；示例报告：`examples/v1_5_partial_report/credibility_tier_report.md`。改造阶段本身仅执行离线测试；随后根据用户明确授权执行了一次受控Live复测，结果见下节。

## 受控Live复测结果（2026-10-23）

- 按既定参数只执行一次，没有自动重试或放宽预算。DeepSeek完成4次模型请求，使用18,475 Input、902 Output、19,377 Total Tokens；SDK未返回可作为账单依据的金额。
- 百度完成3次真实搜索请求，返回包括公司官网在内的候选来源，本地按标准版估算搜索费用为0.108元，实际费用以百度控制台为准。
- 模型自主调用`search_web`、`fetch_source`和`extract_evidence`；Python成功登记S04—S07并抓取4份HTML，其中S04为以岭药业官网。S05、S06进入证据提取，但本次未形成Fact。
- 模型在已完成3次搜索后又发起补充搜索；Python按`max_supplemental_rounds=2`拒绝第4次搜索并以`SUPPLEMENTAL_SEARCH_LIMIT_EXCEEDED`停止。未生成最终模型候选文本、可信度分层输出或Markdown报告。
- 轨迹同时暴露两个后续问题：检索Runtime没有把通用`--max-tool-calls 3`作为所有检索Tool的总上限（本次已记录9次成功Tool调用）；百度安全验证页S07被静态解析器登记为正文，仍需增加验证码/内容不足识别。
- 完整脱敏轨迹保存在`data/web_research/v1_5_partial_success_live/retrieval_trace.json`。本次结论仍是Live部分验证，不把它描述为成功报告交付。

## 最终稳定性修复（2026-10-23，离线）

- 零Fact根因已由保存正文复现：旧召回只识别“主营业务/主要产品”等原词，未识别S05/S06中的“核心业务、营业收入、产品群、药物、营销模式”等表达。新召回器使用确定性研究主题同义词评分，不调用LLM决定证据。
- S05完整保存正文形成F801—F805共5条事实；S06形成F806—F810共5条事实。二者均为第三方单一原文，因此Evidence状态为`single_source`，报告明确标注“单一来源，待交叉核实”，没有提升为`verified`。
- S07的最终URL和页面标题/正文命中验证码与访问验证规则，标记为`access_restricted_content`，只保留CAND001及原搜索结果摘要，不作为有效研究正文或确定性事实。
- 默认预算调整为10轮检索、10次搜索Tool、10次真实搜索API、20次抓取、20次提取、60次总Tool、70轮Agent。轮次、Function调用及真实API请求独立计数；第10轮允许，第11轮拒绝。显式`--max-tool-calls`现在确实约束V1.5全部Function Tools。
- 预算耗尽作为结构化Tool结果返回，提示模型停止新增调用；无论模型是否正常收尾，Python都会保留已有Fact/Candidate、拒绝非法模型文字并生成`web_research_report.md`。有部分资料时为`partial_success`，完全无事实/候选时为`failed`并输出资料不足说明。
- Tool返回最多3个300字预览段落；`extract_evidence`只返回本次新增事实/候选/冲突，完整网页继续本地缓存，降低历史JSON与正文的Token负担。
- 实际离线示例：`examples/v1_5_stability_offline/web_research_report.md`及`web_evidence.json`。本轮没有发送DeepSeek或百度请求，也没有Git Push。
