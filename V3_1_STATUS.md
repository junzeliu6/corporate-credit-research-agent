# V3.1 Streamlit 完整交互界面开发状态

> 日期：2026-10-26
> 范围：V3.1A 报告展示、V3.1B 连接既有 Agent Runtime、V3.1C 持久化任务状态与用户体验。
> 外部调用：本轮开发与离线测试未授权、未执行新的 DeepSeek 或百度付费请求。

## 1. 目标与架构边界

V3.1把现有V2.5研究能力包装为本地Web应用，用户可输入上市公司名称/代码、修改研究任务、选择完整或单项研究，并在明确确认后提交Live任务。Streamlit只负责输入、确认、状态和产物展示；权威研究入口仍是`credit_research_agent.cli`及其调用的既有Agent Runtime、两个Skill和检索Tools。

调用链如下：

```text
Streamlit 表单
  → ui_service 校验主体、模式、确认与输出目录
  → 独立 ui_worker 后台任务
  → python -m credit_research_agent.cli（shell=False）
  → 既有 Agent Runtime / SkillRuntime / Python Tools
  → data/streamlit_runs/<task_id>/ 独立产物
  → Streamlit 读取任务状态、ExecutionTrace、Markdown、图表和财务数据
```

没有在Streamlit内复制Agent Loop、Skill选择、PDF解析、股权绘图、财务计算或报告生成逻辑。DeepSeek仍是正式分析正文作者；Python界面层不生成模板分析。

## 2. V3.1A：历史报告展示

状态：**已实现并完成界面冒烟检查**。

- `ui_artifacts.py`只扫描项目`data`和跟踪的V2.5 Mock样例根，不接受用户输入的任意本地路径。
- 优先加载`credit_research_report_recovered.md`；恢复版保留原始`failed`状态，显示为“部分完成／已离线恢复，需复核”。
- Markdown报告可按二级标题选择全文或单章阅读，并可下载原始Markdown。
- Markdown中的本地股权图链接不交给浏览器猜测；界面优先用PNG、缺失时用SVG，由`st.image`单独展示。
- 财务表直接读取现有`financial_metrics.json`的科目变动与Python已计算比率，界面不重新计算数值。
- 已在本机临时启动Streamlit，健康检查返回HTTP 200；浏览器实际显示以岭药业恢复版六章报告、股权图和两张财务数据表。验证后已停止临时服务。

## 3. V3.1B：连接既有 Agent Runtime

状态：**已实现并接入Streamlit页面；不以历史报告展示冒充Agent连接**。

- 支持`complete`、`ownership`、`financial`、`industry`和`fundamentals`五种受控模式。
- `CompanyResolver`先验证企业；当前企业登记表仍只支持以岭药业及其名称/代码别名。
- `ui_service.py`构造固定白名单argv，以`sys.executable -m credit_research_agent.cli`和`shell=False`调用现有CLI。
- 完整研究继续走`--run-complete-research`；股权/财务走现有`--run-agent`；行业走`--run-industry-research`；基本面走`--build-fundamentals`。
- Live完整/行业任务需要DeepSeek和百度搜索两项显式确认；Live股权/财务需要DeepSeek确认。CLI和Runtime继续执行第二层门禁。
- 页面只返回`DEEPSEEK_API_KEY`、`BAIDU_SEARCH_API_KEY`是否配置的布尔值，不显示密钥。
- 每次任务固定写入`data/streamlit_runs/<task_id>/`，不允许表单注入额外CLI参数、来源覆盖或任意输出路径。
- 新增`build_mock_command`用于完全离线前后端链路验收；Mock命令不包含任何Live确认参数。

## 4. V3.1C：状态、刷新恢复与重复提交

状态：**已实现并接入Streamlit页面**。

- `submit_research_task`先保存`ui_task_request.json`与`ui_task_status.json`，再启动独立`credit_research_agent.ui_worker`；关闭或刷新浏览器不会让表单代码重新调用CLI。
- 如果后台Worker的`Popen`启动失败，提交服务会原子地把任务状态收敛为`failed`并记录脱敏错误；不会留下永久`queued`任务。
- Worker再次用相同输入重建白名单命令并逐项比较；任务文件被篡改、加入`--env-file`或路径越界时，在启动CLI前拒绝。
- Worker在请求解析、路径边界或命令一致性校验阶段失败时，也会回写`failed`状态，而不是只在进程日志中留下异常。
- 状态为`queued / running / succeeded / partial_success / failed`；记录任务ID、创建/开始/结束时间、PID、退出码和不含密钥的错误摘要。
- 单个模块失败但已有报告、图表或数据时可保留`partial_success`，不会删除完成的成果。
- `discover_ui_tasks`和`load_ui_task`从磁盘恢复任务，因此页面刷新后可以继续观察同一任务，而不是自动创建新任务。
- 页面读取任务时会收敛意外断电或Worker崩溃遗留的陈旧状态：`running`任务的PID已不存在，或`queued`持续超过60秒时，原子标记为`failed`并保留已经生成的报告、图表和数据。
- 幂等键用于拦截同一页面提交动作的重复点击；命中既有任务时返回原任务，不创建第二个Worker。
- 项目级原子Live任务锁跨浏览器标签页和会话生效；并发提交时只有一个会话能够取得锁并启动Live。Worker正常完成、启动/请求校验失败或陈旧任务自动收敛时都会释放锁。
- 陈旧任务判断在Windows使用`OpenProcess`与`GetExitCodeProcess`只读探测PID，只有POSIX平台才使用`os.kill(pid, 0)`；不会为判断存活状态而误终止Worker。
- 损坏或空的Live锁文件只有在确认不存在其他活动Live任务时才会自动清理，并以原子方式重新竞争锁；无法确认安全时保持拒绝新Live提交。
- 页面检测到尚未结束的后台任务时锁定新的提交；任务结束后需要用户主动点击“允许提交新的研究任务”，才会生成新的幂等键。
- 页面使用定时片段读取磁盘状态和脱敏进度；完成后自动切换到已有产物展示，单个模块失败但留有报告、图表或数据时显示`partial_success`。
- `ui_progress.jsonl`是现有ExecutionTrace的展示旁路：只保存阶段、模型轮次、Tool名、Token计数和交付状态等白名单字段。完整审计仍以既有ExecutionTrace为准。
- 完整研究及`ownership`、`financial`、`industry`三个单项Runtime均接入同一个脱敏`progress_sink`，页面可以显示实际模型/Tool事件，不用伪造固定进度。

## 5. 主要文件

- `streamlit_app.py`：项目根启动入口。
- `src/credit_research_agent/streamlit_app.py`：页面表单、章节导航、报告/图表/财务表和任务状态组件。
- `src/credit_research_agent/ui_artifacts.py`：历史产物发现、安全读取和统一展示模型。
- `src/credit_research_agent/ui_service.py`：输入校验、Mock/Live白名单命令、任务提交与持久化状态读取。
- `src/credit_research_agent/ui_worker.py`：独立后台Worker；只监督现有CLI。
- `src/credit_research_agent/ui_progress.py`：脱敏JSONL进度写入。
- `src/credit_research_agent/cli.py`、`retrieval_runtime.py`、`integrated_agent_runtime.py`：只增加可选进度回调，不改变未传参数时的既有执行逻辑。
- `tests/test_ui_artifacts.py`、`test_ui_service.py`、`test_ui_tasks.py`、`test_ui_progress.py`、`test_streamlit_helpers.py`：V3.1离线测试。

## 6. 安装与运行

```powershell
.venv\Scripts\python.exe -m pip install -e ".[ui]"
.venv\Scripts\streamlit.exe run streamlit_app.py
```

页面默认只显示历史报告；不会因为打开或刷新页面而调用API。真实研究操作步骤：

1. 在“新建研究”输入公司和任务，选择研究模式。
2. 查看页面仅以“已配置/未配置”显示的API准备状态。
3. 勾选DeepSeek确认；完整/行业模式还需勾选百度搜索确认。
4. 点击一次“启动研究”。
5. 从持久化任务状态查看运行阶段；完成后在同页或历史报告中打开产物。

## 7. 测试记录

已确认的专项结果：

- `.venv\Scripts\python.exe -m pytest tests/test_ui_tasks.py tests/test_ui_progress.py tests/test_ui_service.py tests/test_streamlit_helpers.py tests/test_ui_artifacts.py --basetemp .pytest_tmp_v31_targeted_windows -q`：最终五组专项结果`43 passed in 4.83s`。
- 其中历史产物与服务两组测试此前单独执行结果为`20 passed`。
- 后台Mock端到端任务`v31_mock_e2e_20261026`：Worker退出码`0`，状态按Mock规则为`partial_success`，同一主Agent轨迹包含10次Mock模型调用并生成完整六章`credit_research_report.md`；真实DeepSeek/百度请求均为0。
- 刷新页面后可从`ui_task_request.json`和`ui_task_status.json`恢复上述任务，并读取既有报告，没有重新启动Worker或重复提交任务。
- `.venv\Scripts\python.exe -m pytest --basetemp .pytest_tmp_v31_full_windows -q`：全量离线回归`321 passed in 16.03s`。
- `compileall`：通过。
- Streamlit本机服务：HTTP健康检查`200 / ok`；浏览器人工冒烟确认历史报告、股权图和财务表可见。

上述专项覆盖历史产物路径边界、Live确认门禁、白名单argv、后台Worker命令重建、防篡改、启动/校验失败回写、跨平台只读PID探测、陈旧任务收敛、幂等提交、跨会话原子Live锁及损坏锁安全恢复、状态恢复、部分成功映射和完整/单项Runtime脱敏进度。全量回归保持V0—V2.5核心功能通过；离线Mock仍不能冒充真实付费Live验收。

## 8. 已知限制

- 当前受控公司登记表只包含以岭药业；界面能输入其他企业名称或代码，但无法识别时会明确拒绝，不能据此宣称已支持全部A股公司。
- 本轮没有从Streamlit发起真实付费Live，因此“界面→真实DeepSeek/百度”依赖已验证的CLI/Runtime门禁和既有V2.5 Live记录；下一次页面Live仍需用户主动确认。
- V3.2 Word导出与V3.3 GitHub项目整理尚未启动。
- Streamlit为本地单机MVP，没有用户账号、权限隔离、数据库或生产部署能力。
- 执行进度只写脱敏摘要；模型上下文、网页正文和密钥不会为方便展示而复制到进度文件。

## 9. 下一步

V3.1A、V3.1B和V3.1C已完成并通过专项测试、Mock后台端到端、刷新恢复和全量离线回归。下一阶段为V3.2：在不改写DeepSeek正文的前提下导出包含标题层级、财务表和股权图的Word报告；V3.3项目整理尚未启动。
