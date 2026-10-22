# Android 真机调试与 USB 界面热更新

## 当前行为

Android 使用系统 WebView、xterm、Unicode11 和可选 WebGL。默认“适应手机屏幕”向后台申请手机行列数，后台确认后才调整本地网格；“共享电脑原尺寸”保持后台网格并缩放显示。隐藏或零尺寸窗口不请求最小网格。历史数据若先于尺寸到达，会先进入有界等待队列，随后按确认网格重放。

来源：`android/assets/mobile/app.js`、`mobile-core.js`；Orca 对照源码 `study/orca/mobile/src/terminal/document/fit-scale.ts`。首次连接、重连、切换会话的回归在 `tests/test_android_replay.py`；共享网格及折叠尺寸回归在 `tests/test_android_mobile_ui.py`。

连接首页显示 APK 版本、构建号、界面版本与最近更新；“测试连接”执行真实 WebSocket、中继连接码和 Token 握手，不执行终端命令。每次 APK 构建自动递增版本；每次界面热更新生成独立时间戳和内容摘要版本。

## USB 热更新

首次需要安装支持热更新的原生 APK。之后 HTML/CSS/JS 与 xterm 资源通过 USB 推送，无需重新安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/push-android-ui.ps1
```

多设备时加 `-Serial <设备序列号>`。脚本分发完整、不可变的版本目录，最后原子切换指针；原生端逐文件验证 SHA256、10 个固定文件名和 5 MiB 总上限。全部校验通过后才加载该版本，不混用两个版本；失败则整体回退 APK。切换版本只重建显示端连接，后台 shell 继续运行。

实现：`HotAssets.java`、`package-android-ui.py`、`push-android-ui.ps1`。这是 USB 开发热更新；尚未提供互联网 OTA 服务。原生 Java、权限、签名和 Manifest 的变化仍需升级 APK。

## 真机自动化

```powershell
.venv/Scripts/python.exe scripts/test-android-ui.py --rotate --keyboard
.venv/Scripts/python.exe scripts/test-android-stream.py
```

第一条通过 ADB 与 WebView 调试协议采集终端网格、缩放、元素边界、横竖屏、真实键盘、四个弹窗与原生连接测试，保存 WebView 和整屏截图。结束后恢复旋转、输入模式并移除调试转发。

原生连接页会清除仅存在内存中的访问令牌。测试该页面时必须显式提供对应的开发凭据文件，避免测试后断开工作连接：`scripts/test-android-ui.py --native-connection-test --native-credentials .omx/android-connection-defaults.json`。脚本核对端点与房间、逐字符填入令牌，不在日志中输出令牌，并取消可选的系统密码保存/更新提示。未提供文件时拒绝执行这项可选测试。普通布局测试不需要该参数。

第二条通过 USB 反向端口创建隔离的内存测试后台，测试 300 次 ANSI 刷新、中文/ASCII 输入、Ctrl-C、会话和窗口删除。没有启动真实 shell，也不向用户终端发送测试命令。结束后移除临时电脑、恢复原尺寸设置、返回默认电脑并确认连接。不要以隐藏菜单中的可访问性计数判断测试配置不存在，否则清理会漏掉它。

原生 WebView 仅在带 `ui_probe=true` 的调试启动中开放探针。脚本不输出 Token；连接首页截图中的密码字段保持掩码。用户要求的个人包从忽略目录 `.omx/android-connection-defaults.json` 读取默认参数，包中包含访问凭据，不应公开分发。

## 404 修复与关闭语义

旧构建使用 `jar uf` 追加 DEX，会重写资源 ZIP 本地头，实机 `AssetManager.open("mobile/index.html")` 失败，最终显示 404。现在使用 `append-apk-dex.py` 保留资源原始本地头，额外校验 CRC 与压缩尺寸；实机已能读取页面并连接中继。

后台实际删除会话/窗口后，手机目录会同步删除。桌面工作区的“×”目前只关闭本地视图并保留后台；“结束”才终止会话。不能把视图收起伪装成后台进程删除。相关协议在 `hub.py`、`daemon.py` 和 `workspace.js`，自动删除同步已由隔离测试覆盖。

## 验证证据

- 真机：Honor FLC-AN00，Android 16，2156×2344、480 dpi。
- 布局与横竖屏截图：`.omx/artifacts/android-device-probe-20261021-235059/`。
- 实机 300 次刷新、输入和删除同步：`.omx/artifacts/android-stream-20261022-000153/verification.json`。
- 原生连接按钮、版本标识与恢复：`.omx/artifacts/android-native-connection/verification.json`。
- 真正的物理折叠动作、铰链遮挡和所有第三方 IME 尚未穷尽验证。

## 后续体验修正（2026-10-22）

终端选择弹窗保留；每项压缩为一行“状态点 / 窗口名称 / Agent 类型”，省略工作路径和重复窗口标题。绿色闪烁表示最近 1.8 秒收到实时输出，黄色表示连接正常且暂未观测到新输出，红色表示连接断开。黄色是活动推断，不能证明任意 CLI 真正正在等待输入。目录并行连接，当前源码遵守 TD-006D/I，仅订阅并渲染活动终端，未打开终端的精确活动状态仍需后台轻量遥测支持；会话 ID 始终按电脑隔离。

打开或切换终端在历史重放完成后滚到底部。手机正常缓冲区最多保留 20,000 行，并忽略仅清空滚动历史的 CSI 3J；清屏 CSI 2J 仍保持终端语义。触摸滑动使用实际字格高度和显示缩放比例，避免共享网格缩放后滑动距离失真。较早、后台已丢弃的输出无法凭客户端设置补回；重新连接和切换后的重放仍受服务器约 256 KiB 尾部范围限制。

真机证据：`.omx/artifacts/android-stream-20261022-003010/verification.json`，包含 300 次刷新、6,498 行可回看历史、真实触摸滚动、输入与关闭同步；测试结束已移除临时电脑并恢复真实中继。

新启动图标源文件为 `android/branding/launcher-master.png`；Android 各密度与自适应资源在 `android/res/`。带图标的个人包为 `dist/android/SuperMonoAgent-Mobile-v0.2.12-icon.apk`，SHA256 `04cba9fdb8f999629bc093926f58d3296a23ac048164bc882a093e1be8cf62e6`；已通过 Android 资源编译和 v2/v3 签名验证。手机系统要求本人指纹才能升级安装，不能将包已生成当作图标已安装。

Windows Codex 右侧空白已在运行界面定位：旧前端保留宽本地网格，而手机曾把后台网格调窄。重新选择 Codex 页签后后台恢复 158×44。源码 `workspace.js` 的 `openView` 已允许主动重选当前页签时重新申请尺寸，后台目录更新仍不抢占；`tests/test_workspace_runtime.py` 同时覆盖新旧后台确认能力。

## 2026-10-22 后续提交与重影修复

手机现已安装原生 0.2.13，本轮界面修复通过 USB 热更新交付，不再安装 APK。编辑发送改为等待远端新回显后再发唯一一次 Enter，支持 Android 输入法事件；无回显 5 秒停止并提示直输。Win3 已切换当前显示服务，原有 4 个 daemon 会话保留。最新真机输入与布局证据分别在 `.omx/artifacts/android-stream-20261022-014319/` 和 `.omx/artifacts/android-device-probe-20261022-014405/`。原因、测试与加密评估见 [客户端评估](transport-crypto-review.md)。
