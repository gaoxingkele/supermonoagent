# 文件交换组件：源码交付状态

2026-10-26 更新：独立 broker、Windows 原生桥／隐藏 helper、编辑框、签名私有安装配置已接入 0.2.44，并通过编译版公网收件与隔离安装验收。实际接收电脑仍需完整专用包并重开 GUI；不能只热更新旧外壳。当前边界见 [0.2.44 说明](windows-editor-0.2.44.md)。下面保留 2026-10-25 源码归档时状态。

2026-10-25 按用户“全部提交 GitHub”要求归档当前实现。这些组件尚未接入正式桌面资源、原生安装器或生产中继；当前 UI 0.2.42 不包含它们，不能把源码提交称为文件交换已经上线。

| 文件 | 已实现的责任 |
| --- | --- |
| `file_workspaces.py` | 主机侧项目／交换目录设置、目录浏览、CAS 修订及发布锁；默认项目启动目录下 `tmp`，交换目录必须是项目真子目录，不修改终端 cwd |
| `file_broker.py` | 独立鉴权中继暂存、配额、摘要校验、持久任务、主机登记及短期 Bearer 下载凭据；HTTP 下载服务只监听 loopback，公网须经 HTTPS 代理 |
| `file_companion.py` | 只读检查本机 daemon 所有者与会话、默认通过 HTTPS HTTP 下载、SHA256 校验、发布前绑定复核及持久 ACK 日志 |
| `static/file-client.js` | 独立文件服务请求、连接和请求超时、端点检查及回复关联；不自动创建连接 |
| `static/file-workspace-ui.js` | 原生配置门控、底部目录栏、目录浏览／CAS 保存及目标绑定；普通切换页签不改变已捕获传输目标 |
| `file_transfer.py` | 新增显式 `strict_name` 发布策略：重名拒绝且不创建别名，原调用默认行为不变 |

主机登记使用独立安装密钥与已绑定的主机密钥，不把查看 Token 当作主机注册凭据。文件先到中继，再由目标电脑拉取到其项目内的交换目录；完成暂存与目标确认保存是不同状态。凭据通过私有文件读取，未提交真实 Token、登记密钥、运行数据库或暂存文件。

## 验证

```powershell
.venv/Scripts/python.exe -m pytest tests/test_file_workspaces.py tests/test_file_broker.py tests/test_file_companion.py tests/test_file_transfer.py tests/test_file_workspace_ui.py -q
node --check src/supermonoagent/static/file-client.js
node --check src/supermonoagent/static/file-workspace-ui.js
```

真实隔离测试覆盖两个目标电脑具有相同窗口／会话 ID、切页签后原目标继续上传、中继 HTTP 下载、不同电脑目录隔离、凭据／代次变化失效、目录修订冲突、错误 Token、缺失 helper、窄屏和无终端输入。Python 编译与两个独立 CLI 的 `--help` 已检查。

## 剩余工作与边界

- 正式桌面资源、原生配置桥、剪贴板 adapter、安装及进程生命周期集成；独立中继路由与主机安装部署；生产端到端验收均未完成。
- Android 多附件／相册／文件夹选择、长历史缓存、MCP 服务不属于这些组件的已交付范围。
- 主机被强制终止在创建空 `.part` 与记录 intent 的极短间隔时，可能留下零字节孤儿；任何文件内容下载前已有持久 intent。不能宣称任意系统强杀下都无遗留文件。
- Windows 无权限创建文件 symlink 的相关测试跳过，不把跳过当作已验收。
- 既有终端服务、TTY 输入和 UI 发布清单未因本次源码归档改变；没有新增依赖。
