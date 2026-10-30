# 项目文档索引

- [Windows 0.2.52 发布交接（2026-10-27）](handoff-2026-10-27-0.2.52.md)：源码／安装包版本对应、验收证据、服务器恢复边界及暂停后的接手入口。

- [六种 Agent 跨端共享渲染模式](agent-render-modes.md)：Windows 与 Android 共用六份配置、选择、修改与调试。

- [免费任务完成通知](task-notifications.md)：现有中继、原生后台通知、保留 Windows 会话的兼容桥与送达边界。

- [桌面与手机界面热更新](ui-hot-updates.md)：检查、签名包、一条命令发布、实体备份及启动失败恢复。

- [统一中继账号与自动选择电脑](relay-account-setup.md)：默认地址、登录、自动填充、授权绑定和 HTTPS/WSS 部署。

- [Windows 左栏状态灯](sidebar-activity.md)：窗口状态颜色、分组缩进、验证与最新 Windows 包。

- [项目 Wiki 与 LLM 日志](../omx_wiki/index.md)：可追溯维护知识；[隐藏空控制台决策](../omx_wiki/windows-hidden-startup.md)、[2026-10-21 LLM 修改日志](../omx_wiki/llm-log-2026-10-21.md)。

- [架构说明](architecture.md)：进程、模块、数据流与会话不变量。
- [线协议](protocol.md)：消息格式、中继、限额与当前安全状态。
- [客户端改进与加密评估](transport-crypto-review.md)：手机提交、Win3 重影修复，Token 派生方案与 Windows/Android 算法开销实测。
- [WebView 布局与 Windows 原生终端评估（2026-10-21）](webview-terminal-review-2026-10-21.md)：现状证据、界面建议、客户端候选、迁移路径及待执行验收。
- [全局桌面工作区互联分析](global-workspace-analysis.md)：多电脑并行感知、聚合操作、目录发现、安全边界与分阶段实施建议。
- [多电脑安全与身份设计](global-workspace-security.md)：已知房间鉴权、独立主机登记、TLS 迁移、统一身份设计与资源测量边界。
- [TD-001～TD-006 验证记录](todo-completion.md)：本轮完成状态、回归证据与最终安装限制。
- [三套 Windows 客户端试用](windows-clients.md)：打包入口、启动命令、共享后台与构建方法。
- [Android APK](android.md)：安装、连接、轻量 WebView 与重建签名。
- [Android 真机与热更新](android-live-updates.md)：手机自适应、刷屏网格修复、USB 热更新、截图探针和隔离测试。
- [Android v0.2.0 与 Orca Mobile 对照](android-mobile-upgrade.md)：手机专用终端、输入/滚动/恢复、内置资源和验证边界。
- [中继基准](terminal-benchmarks.md)：直连/中继与旧版重复扇出对比。
- [DOM/WebGL 实测](renderer-comparison.md)：有效渲染器、Unicode/ANSI 一致性与上下文丢失回退。
- [本轮交付状态](implementation-status.md)：产物、验证证据与试用边界。
- [修改 TODO](TODO.md)：待实施事项、全局多电脑互联 TD-006 及各项验收标准；已完成事项保留证据和状态。

初期评估文档是方案记录；已实施范围和实测结果以交付状态及对应验证文档为准。
