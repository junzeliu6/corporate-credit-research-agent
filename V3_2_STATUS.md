# V3.2 Word 报告导出开发状态

## 1. 阶段目标

V3.2在不改变V2.5研究内容和V3.1任务运行机制的前提下，将已生成的Markdown企业授信研究报告转换为排版规范的Word文件，并同时向CLI与Streamlit提供同一导出能力。

本阶段的作者边界保持不变：DeepSeek负责报告分析正文，Python只负责读取已有Markdown、转换Word文档结构、插入既有图表和提供下载。导出过程不调用DeepSeek、百度搜索或Graphviz，也不会修改原Markdown。

## 2. 已完成能力

### 2.1 Word 导出内核

- 新增`src/credit_research_agent/word_export.py`。
- 支持UTF-8 Markdown标题、普通段落、粗体、斜体、行内代码、链接、无序/有序列表、引用块、GFM表格和本地图片。
- 生成A4页面，设置标准页边距、宋体正文、黑体标题、统一行距、页眉、页码和报告封面。
- 财务表格使用重复表头、边框、表头底色和数值对齐；股权图居中并按版心限制最大宽高，保持原始纵横比。
- Markdown引用SVG时优先复用同目录同名PNG；显式提供的股权PNG在原Markdown图位替换，不重复插图，也不重新运行Graphviz。
- 输出采用临时文件保存并重新打开校验，再原子替换目标DOCX；不会覆盖Markdown。
- 图片缺失、远程图片、Markdown/输出/图片越出允许目录、错误扩展名及无效文档都会返回带错误码的明确异常。

### 2.2 CLI

新增参数：

- `--export-word MARKDOWN_PATH`
- `--word-output DOCX_PATH`
- `--word-image PNG_PATH`

`--export-word`在企业身份解析后进入纯本地早返回分支，不创建下载器、Agent Runtime、DeepSeek或百度客户端。CLI结果包含转换后的路径、图片/表格/标题数量、警告码，以及`external_api_requests=0`。

示例：

```powershell
.venv\Scripts\python.exe -m credit_research_agent.cli "002603.SZ" `
  --export-word "data\complete_research_live_v25_retest_20261026\CN-A-SZSE-002603\credit_research_report_recovered.md" `
  --word-output "data\complete_research_live_v25_retest_20261026\CN-A-SZSE-002603\credit_research_report.docx" `
  --word-image "data\complete_research_live_v25_retest_20261026\CN-A-SZSE-002603\assets\ownership_structure.png"
```

### 2.3 Streamlit

- `ui_artifacts.py`只为任务管理已经确认的`ResearchRunArtifact`导出Word。
- Markdown、股权图和DOCX必须位于同一`run_dir`，防止任意路径读取或跨任务文件混淆。
- 输出文件与当前Markdown同名；恢复版和原版报告各自生成独立DOCX。
- 当DOCX不早于Markdown及其引用的全部本地图表时复用缓存；任一图表更新或缺失都会使缓存失效或明确报错。页面刷新和下载不会重新运行研究任务。
- `streamlit_app.py`在Markdown下载旁增加“下载 Word 报告”。导出失败显示清晰警告，但不影响继续查看和下载Markdown。
- 历史报告默认优先选择更新时间排序后的首个有效Live报告；没有Live报告时才回退最新Mock。网页Markdown与Word转换始终复用同一个`artifact.report_path`。
- 下载数据设置大小限制，尚无有效报告时不提供Word下载。

## 3. 文件变更

新增：

- `src/credit_research_agent/word_export.py`
- `tests/test_word_export.py`
- `tests/test_cli_v32.py`
- `tests/integration/test_word_export_existing_reports.py`
- `V3_2_STATUS.md`

修改：

- `.gitignore`
- `src/credit_research_agent/__init__.py`
- `src/credit_research_agent/cli.py`
- `src/credit_research_agent/ui_artifacts.py`
- `src/credit_research_agent/streamlit_app.py`
- `tests/test_ui_artifacts.py`
- `pyproject.toml`
- `README.md`
- `ROADMAP.md`

## 4. 示例产物

输入报告：

`data/complete_research_live_v25_retest_20261026/CN-A-SZSE-002603/credit_research_report_recovered.md`

生成报告：

`data/complete_research_live_v25_retest_20261026/CN-A-SZSE-002603/credit_research_report.docx`

转换结果：1张股权结构图、2个财务表、48个Markdown标题，无导出警告，外部API请求为0。

## 5. 测试与视觉核查

专项测试命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_word_export.py tests/integration/test_word_export_existing_reports.py tests/test_cli_v32.py tests/test_ui_artifacts.py tests/test_streamlit_helpers.py -q
```

最终专项结果：`31 passed in 5.83s`。

完整回归命令：

```powershell
.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_tmp_v32_full_final
```

完整回归结果：`344 passed in 19.13s`。受限沙箱会阻断Windows `asyncio`创建本机loopback socketpair，因此完整回归在获准的离线测试环境中运行；没有访问DeepSeek、百度或其他外网。

Streamlit补充检查：本机健康端点返回`200 ok`；在同样允许本机loopback的离线环境中执行`streamlit.testing.v1.AppTest`，页面异常数为`0`。裸模式输出的`missing ScriptRunContext`属于Streamlit测试警告，不是应用异常。

视觉核查：内置DOCX渲染脚本因当前环境没有LibreOffice而无法使用。随后使用本机Microsoft Word 2021把示例DOCX导出为16页A4 PDF，并用Poppler逐页渲染检查。封面、中文正文、标题层级、表格、股权图、分页和列表均未发现明显溢出、重叠、变形或乱码；该PDF和页面图仅作为本地QA产物，不进入正式交付。

## 6. 安全与外部调用

- 本阶段没有调用DeepSeek、百度搜索或其他付费API。
- Word下载不会触发Agent Runtime，也不会启动新的研究任务。
- UI只允许导出任务目录内、由现有任务管理发现的报告和图片。
- 密钥、完整网页正文和模型上下文不会写入Word。
- 本阶段未执行Git Push。

## 7. 已知限制

- 第一版覆盖项目当前实际使用的Markdown子集，不是完整CommonMark/GFM浏览器渲染器；复杂嵌套列表、HTML块、脚注、数学公式和跨行/合并单元格尚未实现。
- 外部HTTP图片不会下载；图片必须已经存在于当前研究任务目录。
- Word字体依赖打开文档的电脑安装宋体和黑体；缺失时由Word进行字体替代。
- 导出器不会判断报告研究结论质量，只保证结构转换和路径安全。报告仍是AI研究初稿，需人工复核。
- 当前企业目录仍只正式登记以岭药业；这属于Agent数据入口的通用化范围，不是Word导出限制。

## 8. 下一步

按路线图进入V3.3项目整理：完善GitHub README、架构图、界面截图、示例报告和项目经验说明。根据用户当前安排，完成V3后再单独推进通用上市公司Agent，不在V3.2扩展公司目录或研究逻辑。
