# Windows 0.2.44：编辑框、附件交换与输入焦点

2026-10-26 完整 Windows 包已构建并发布至完整程序更新频道。新增原生文件助手配置入口，因此首次启用接收能力需安装专用配置包并重开 GUI；既有 daemon／PTY 不需要重启。共享 UI 频道仍为 0.2.42，本次没有发布来源不明的并行 Android 修改。

## 编辑与附件

- 顶部新建按钮附近增加“编辑”，弹出多行文本框，可使用标准 Ctrl+V 或粘贴文本按钮。关闭对话框保留本次会话草稿；插入使用终端粘贴，不额外按 Enter，具体提交行为由终端程序处理。
- 可多选图片、PDF、Word 等普通文件；浏览器来源沿用每批 32 个、单文件 64 MiB，逐文件准备，避免整批读入内存。
- 显式上传先进入中继私有暂存，再由目标电脑独立 helper 通过 HTTPS 拉取到项目内交换目录，默认启动项目的 `tmp`。下方目录栏可浏览和修改项目／交换目录，不向终端发送 `cd`。
- 文件名由服务生成 `日期-时分秒-原名.类型`；回执匹配长度、SHA256 和保存名称后才显示已保存。路径可显式加入草稿，不自动合成 shell 命令或声称已进入模型上下文。
- 发送绑定原电脑、窗口、会话和连接代次；切换到 B 不改变已开始的 A 传输。中继已提交但收件未知的任务保留 ID、标为待确认，不能假称取消成功或自动重复上传。
- 本次新编辑框上传不回落到旧 OP21 直传；原有终端文件粘贴兼容行为保留。旧外壳／缺私有 profile／接收机未安装 helper 会明确提示不可用。

## 输入问题的证据边界

已复现并修复：点击缩放终端周边空白区时只激活页签、不聚焦 xterm textarea，导致键盘或文本粘贴丢失。修复只作用于 screen 自身空白，不抢标题按钮、滚动和选择目标的焦点。

针对用户 `JINLONG3090 / IIA_Benchmark` 两次只读检查，后台上报 158×43、光标 `(0,41)` 在左下空白行；未见可信 Cursor agent 身份、提示行、DSR／DA／OSC 查询或可见错误信号。快照仅在内存解析，未保存正文／目录／凭据，没有向该会话发输入、应答、resize、focus 或关闭指令。

因此不能把焦点修复或自有 DSR toy 当作实际 Cursor TUI 光标已经修复。实际 Cursor 的启动命令／版本和更新后的显示仍待对照；现有设备应答过滤保持，不凭配色或窗口名授予额外自动输入。

## 安装与服务

- 客户端和每台接收电脑需要完整专用包中的私有 `file-exchange/profile.json`，安装器验证后写 current-user＋SYSTEM ACL；已有有效 profile、主机身份、Token、配置及日志保留。旧 GUI 至少需要关闭重开一次获得新原生方法，安装器不会强制终止它或后台会话。
- 通用公开更新包不含 Token／enrollment。专用包由限时受控链接提供，私有包与密钥不提交 Git。
- 独立文件 broker 位于 `/var/lib/supermono-files/file`，只监听 `127.0.0.1:18819/18820`；通过 `/file/ws` 和 `/file/download/<id>` 代理，下载需短期 Bearer，不设置公开 static alias。
- 默认候选 8819 由原 nginx 使用，本次没有停止或修改那个监听程序，改用已核实空闲端口。配置校验后平滑 reload，终端 relay PID 不变。MCP 服务亦保持原样。

## 验证

- 组件组合 96 passed；失败清理时序新增门控测试先红后绿，helper 全组 40 passed，收到 `failed` 时即无 partial。
- 私有安装与现有更新兼容组 114 passed；真实完整 ZIP 在隔离目录安装两次，profile 的私有 ACL、Token／profile 保留及安装 exe hash 均通过，未启动用户 GUI 或修改真实注册表。
- 实际隐藏 WebView2 显示编辑按钮，无可见新窗口、PTY 输入 0；合成 clipboard 事件测试不等于当前系统剪贴板已被验收。
- 实际编译版 helper 经公网 WSS、私有 enrollment 和 HTTPS 下载，将 332806 字节 PNG、179215 字节 PDF 原样保存到自有项目 tmp，SHA256 相同，模拟旧 parent 只收到 HELLO/PING，无终端输入。
- 文件 broker 增加 Python 3.10 兼容的分块散列，43 项 broker 回归通过；不更改原终端解释器。

原始证据：`.omx/artifacts/windows44-final-components.xml`、`windows44-integration.xml`（保留首次清理竞态失败）、`file-broker-py310-final.xml`、`frozen-file44-acceptance.json`、`native-composer44-acceptance.json`、`windows-files-0.2.44/full-installer-verification.json`、`cursor-input/`。
