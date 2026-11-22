# 业务 MCP 与 Android 反馈试点验收

2026-11-22。用户已指定 Ewan 为美国采集机，Windows 发起端为 `redmi-16-2026`。优先使用独立项目 `agent-hub-pilot`。以下记录区分实际主机、Android 模拟器及仍需现场核对的部分。

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 中央服务 | 在实际 Linux 中转服务器部署私有 MCP。独立非特权容器、只读根文件系统、私有 Unix socket、零真实授权。重启后匿名查询返回 401，SQLite 完整性检查通过。原中继返回 200，原 mylib MCP 返回 401 并保持 active。 | [私有实例校验](/D:/aicoding/supermonoagent/.omx/artifacts/agent-hub-0.2.81-pilot-20261122-052838-r2/linux-private-pilot.json) · [停用和接入说明](/D:/aicoding/supermonoagent/.omx/artifacts/agent-hub-0.2.81-pilot-20261122-052838-r2/linux-private-ops.md) |
| 跨机链路 | Windows 与实际 Linux 中转机各自生成一个测试执行。经固定主机密钥的 SSH 通道，两个本地 wiki 的正文分别与中央库匹配，任务交接到 Linux 执行，两个测试额度任务均有回执。越权项目查询和只读身份写入均被拒绝。 | [Linux 双机联调](/D:/aicoding/supermonoagent/.omx/artifacts/agent-hub-0.2.81-pilot-20261122-052838-r2/linux-relay-validation.json) |
| Android OS | Android 15 / API 35 模拟器实际安装并启动 APK。正常 0.2.81 应用和独立反馈 QA 应用使用不同包名。QA 中的测试项目和主机由构建配置固定，每次启用需在前台显式勾选。 | [包校验](/D:/aicoding/supermonoagent/.omx/worktrees/agent-hub-pilot/.omx/artifacts/android-feedbackqa-final-20261122-231650/package-verification.json) · [生命周期检查](/D:/aicoding/supermonoagent/.omx/artifacts/android-os-pilot-0.2.81-r2-20261122/qa-final-lifecycle.json) |
| 确认与核对 | 最终 QA APK 上向单一合成执行确认发送，故意丢弃首次响应后核对原请求。发送、代理受理、核对各一次；原始终端输入、尺寸调整和 PTY 写入均为零。后台切换使未确认草稿失效。 | [Android 实测](/D:/aicoding/supermonoagent/.omx/artifacts/android-os-pilot-0.2.81-r2-20261122/qa-final-feedback-e2e.json) · [中央与源端回执](/D:/aicoding/supermonoagent/.omx/worktrees/agent-hub-pilot/.omx/artifacts/agent-hub/avd-confirmation/final-apk-central-source-receipt.json) |
| Ewan 接入 | 只读目录和宿主握手确认 Ewan 在线，有 12 个窗口并声明支持文件传输；握手未声明管理式会话或新版反馈能力。可先在独立项目运行主机 Hook 和额度进程；向旧版普通终端回馈走用户确认的草稿路径。 | 现场只读检查；业务侧仍需 Ewan 的独立试点运行环境和实际计费账号绑定。 |

**现场放行条件。** 在 Ewan 本机或其指定管理通道配置试点进程和真实账号，登记可达的私有 MCP 路径，再核对一次 Hook 入库后的真实周余量及下次重置。物理手机另行验证真实输入法、旋转、挂起恢复及用户确认。面向公网的业务路由和生产频道需单独放行。
