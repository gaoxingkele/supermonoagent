# 免费任务完成通知

复用现有中继，不购买推送套餐，不要求 Google Play 或厂商推送账号。手机使用用户主动开启的原生前台监测服务，通知栏有常驻状态和“停止监测”。它独立于终端 WebView，网页退到后台照常暂停，手机不订阅终端屏幕字节流。

## 当前支持范围

- 支持有 OSC 7501 明确工作→空闲状态信号的终端程序；已核实 Codex `working → idle`。启动 idle、历史回放、clear、重连、关闭窗口、仅仅无输出不算完成。现有进行中的任务若在监测开启前已经开始，不根据回放补猜其状态。
- 提醒含电脑/项目、窗口名及通用结果提示，点击回到对应电脑的既有终端。当前不从 TUI 最后几行猜测结果摘要，不宣称测试或业务执行成功；没有明确完成信号的 Kimi/普通 Shell 不作静默推断。
- Android 当前同时监测所选电脑的全部终端。再次启用另一台电脑会替换监测目标；多电脑并行监测仍需后续扩展。
- 本机主机持久保存最多 256 条事件，单次补收最多 24 条；首次启用设静默基线，重连按游标补收，手机保存有界去重记录。通知权限被关闭时不把未展示事件记成已展示。它是有界恢复，不是无限期消息归档。
- 不持久保存新的原生监测 token；服务被系统结束或用户强制停止后需重新启用。普通断线会自动重连。厂商省电/Doze 可能限制网络，不能承诺长期锁屏百分百实时送达。

源码：[TaskNotifications.java](../android/src/dev/supermonoagent/mobile/TaskNotifications.java)、[EventSocket.java](../android/src/dev/supermonoagent/mobile/EventSocket.java)、[task_events.py](../src/supermonoagent/task_events.py)。

## 手机使用

首次升级 APK，授予系统通知权限。在连接首页的“任务完成通知”，或者终端设置中的同名入口，点“启用通知”。通知栏显示“正在监测任务结果”才表示连接完成；“连接中断，正在重连”不表示已经收到通知。

旧 Windows 后台选择“兼容旧版 Windows 后台（保留现有终端）”，需要运行下述电脑通知桥。已升级且直接支持 task_events 的后台取消此选项。停止时可点击常驻通知的“停止监测”，也可在同一设置中“停止通知”。

事件通知点开后只选择已经授权且存在的终端，不执行正文、不创建 shell。终端已关闭或当前无法连接会提示检查终端；当前未实现关闭终端后的完整结果阅读器。

## 保留旧 Windows 会话

旧后台无需重启。兼容桥只在本机订阅原后台的终端输出，识别状态后经原房间加 `-events` 的独立通知通道传送小事件。原终端房间和 PTY 不变。手机选择兼容模式后自动使用通知房间，点击时仍进入原终端房间。

```powershell
# 在源码工作区运行；token 只从既有状态文件读取，不传入命令行。
.venv/Scripts/pythonw.exe -m supermonoagent.notification_bridge `
  --backend http://127.0.0.1:6769 `
  --state-dir "$env:USERPROFILE/.supermonoagent" `
  --relay ws://117.50.48.22:8817/ws `
  --code-file "$env:USERPROFILE/.supermonoagent/relay-code" --port 6790
```

新 Windows 打包程序也提供 `SuperMonoAgent.exe notifications`，参数同上。它是独立进程，本轮没有注册开机启动任务；电脑重启后需重新启动兼容桥。`http://127.0.0.1:6790/health` 的 `backend_connected` 与 `relay_connected` 应同时为 true，该接口不返回凭据。

现有公网地址仍是 WS，本轮没有更改公网 TLS 部署，也没有新增端到端加密。因此不要把此版本描述为加密推送；后续可将同一套链路切换到经过证书验证的 WSS。事件及授权在服务端按所有者/频道隔离，设备凭据不写入通知正文、URL 或测试记录。

## 验证与维护

- `tests/test_task_events.py`：分片状态、无订阅输出、启动/回放去误报、权限与持久补收。
- `tests/test_notification_bridge.py`：旧后台 Authorization 请求头、真实隔离中继、只读连接、不创建 PTY。
- `tests/test_android_notifications.py`：真实 JVM WebSocket 掩码、握手、帧限额、分片、控制帧、空闲保活。
- `scripts/test-android-notifications.py --doze`：在当前公网中继创建临时隔离测试房间，用假 PTY 验证手机后台/锁屏/Doze。测试不向用户终端发送指令；完成后必须恢复真实监测目标。不会绕过手机锁屏或安装指纹确认。

本轮构建：Android 0.2.17 / code 19；Windows `dist/windows-notifications/`。Android 私人安装包使用了本机既有连接默认值，不能当作通用公开包转发。完整测试记录与实机状态以 `.omx/artifacts/terminal-notifications/` 为准，尚未做 8 小时耗电和长时间锁屏送达率评估。
