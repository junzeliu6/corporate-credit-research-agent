# GitHub 首次发布检查

检查日期：2026-10-28。

## 安全检查结论

- 已检查当前工作区、暂存区和 `main` 的全部可达提交。
- 未发现历史中提交过真实 DeepSeek、百度、OpenAI、GitHub、AWS 或其他 API Key/Token。
- 本地 `.env` 含已配置密钥，但从未被 Git 跟踪；文件值未写入本报告。
- `.streamlit/secrets.toml` 未被跟踪，并已加入 `.gitignore`。
- 当前发布版本不包含 PDF、网页缓存、完整运行日志或任务级执行轨迹。
- 历史提交包含开发机绝对路径；本轮按约束未重写历史。因此首次发布应使用 Private 仓库，公开前再决定是否进行历史清理。

## 审计覆盖范围

- 18 个可达提交和其中的文本 Blob。
- 常见 API Key 环境变量和 Token 格式。
- `.env`、Streamlit secrets、Authorization 请求头、邮件地址和本地绝对路径。
- 当前跟踪文件、忽略目录中的本地日志以及拟发布演示材料。

未覆盖：

- 二进制图片的 OCR 隐私扫描。
- 供应商控制台侧的密钥使用记录和账单。
- GitHub 服务端规则、组织权限和分支保护，因为本轮未创建或连接远端仓库。

## 发布文件策略

保留源码、两个 Skill、测试、静态配置、Mock fixture、文档，以及一份经过人工检查的以岭药业 Live Markdown/股权图示例。

排除根目录 `data/` 中的 PDF、网页正文、搜索缓存、任务日志和中间产物。旧版批量 `examples/` 技术轨迹已从当前发布索引移除，但开发机本地文件没有删除。

测试仍会读取的少量 V1/V2 历史产物以“离线回归 Fixture”保留；它们不包含个人绝对路径、鉴权值或完整执行轨迹，也不作为推荐演示入口。

## 独立环境复现结果

验证对象是拟提交的 Git 暂存树快照，而不是原工作区副本：

- Python：3.11.5；全新 `.venv`。
- 安装：`python -m pip install -e ".[dev,ui]"` 成功。
- 未设置 `DEEPSEEK_API_KEY` 和 `BAIDU_SEARCH_API_KEY`，没有发起模型或搜索请求。
- 原工作区完整回归：`483 passed in 23.68s`。
- 干净发布副本：`471 passed, 12 skipped in 19.82s`。跳过项依赖未提交的真实年报/PDF、解析缓存或历史任务目录，属于预期的本机集成测试边界。
- 随仓库 Mock 报告可由 `ui_artifacts` 发现并读取，Word 离线导出成功。
- Streamlit 在独立副本启动后健康检查返回 HTTP 200，随后测试进程已停止。

Windows 注意事项：当前宿主在中文项目路径下使用项目内 `--basetemp` 时，曾有两项临时文件写入因路径编码失败；改用全新的纯 ASCII 系统临时目录后，同一代码回归全部通过。这是运行环境路径兼容性注意项，不是业务断言失败。

## 发布前再次确认

```powershell
git status --short --branch
git diff --cached --check
python -m pytest -q
```

首次远端建议创建 Private 仓库，不自动添加许可证。推送前由仓库所有者再次检查提交差异和远端地址。
