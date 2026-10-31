# Android 真机调试与 USB 界面热更新

## 移动端文本高亮

Android 专用终端默认开启文本高亮，右上角“⋯ → 文本高亮”可以关闭，选择保存在当前设备。文件名、数字、引号内容、Markdown 行内代码、函数名和错误提示会补充颜色；应用自身输出的 ANSI 前景色、背景色、反色和隐藏文字保持原样。

实现复用 `vendor/terminal-client.js` 的 `createHighlighter`，`mobile-core.js` 保留相同接口；Windows 工作区也使用这一实现。六份 `agent-modes` JSON 同样由两端共享，Android 的 APK、USB 与签名更新直接打包 Windows 所用文件，保证主题、ANSI 调色板和 token 规则一致。原始字节仍直接交给 xterm，在解析后的可见字格上注册 foreground decoration，不往终端流中插入 SGR，不改变字符、光标、复制内容或后台会话。装饰只保留当前可见行，随输出、滚动、尺寸和缓冲区变化刷新，切换会话时销毁；DOM 与 WebGL 使用同一套规则。这是文字模式的辅助着色，不是完整的编程语言语法解析器；跨终端自动换行的单个 token 可能只按各行分别匹配。

回归：`python -m pytest -q tests/test_android_highlighting.py`。隔离后台和隐藏 Edge 覆盖冷启动历史、逐字节 UTF-8/SGR/OSC、中文及 emoji 字格、原生调色板/真彩色、光标覆盖、备用屏、历史滚动、旋转、开关保存和会话切换；DOM 检查实际文字颜色，双渲染器截图位于 `.omx/artifacts/mobile-highlighting/`。这些是桌面 Chromium 验证，不代表已更新或验证真机 WebView。

## 输入、滚屏与持续输出

底部快捷输入框可直接调出手机输入法，与多行编辑弹窗共用同一终端草稿、IME 预编辑保护和回显确认状态机。弹窗在输入被接受后立即关闭；只有确认新回显后才发送唯一一次 Enter。无回显或断连时保留草稿并提示，旧回调不能关闭重新打开的编辑会话。输入栏中的“收起／展开”按钮同时切换历史操作栏与辅助按键栏并保存状态，输入框和发送按钮始终保留。

普通终端保留 xterm scrollback，触摸滚动具有速度跟踪和最长 900ms 的惯性；新触摸、切会话、暂停及历史按钮会取消旧动画。Codex 备用屏与 Windows 一样发送程序自身的 PageUp/PageDown/Ctrl+Home/Ctrl+End，其他全屏程序只浏览本地有界历史，不接收意外翻页键。

本地全屏历史在读取字格前统一限频，不再因存在 scrollback 而对每次输出重建整屏；终端/电脑目录只有显示数据变化时才重建 DOM。`MobileTerminal.metrics` 提供无文本、无凭据的采样计数，供真机对比持续输出开销。输出积压和 ping 探测超时使用可恢复的 4000 关闭码并按退避重连；4001 仅表示认证失败并停止重试。

0.2.55（versionCode 57）已在 Honor FLC-AN00 / Android 16 真机覆盖安装并验收：原生 IME 与 Unicode 输入、弹窗发送后关闭、两层工具栏折叠且保留输入、300 次刷新 CPU profile、6500 行历史、Codex 按钮与触摸翻页、横竖屏及 13 个界面检查均通过。300 次刷新期间全屏历史重建为 0 次，采样无忙等热点。显式 WebGL 在 DPR 3 下画布、字格尺寸一致，Kimi 实际页面未见叠字，测试后恢复原会话及自动渲染。证据见 `.omx/artifacts/android-055-complete/verification.json`、`.omx/artifacts/android-055-ui/verification.json` 与 `.omx/artifacts/refresh-diagnosis/android-renderer/report.json`；临时电脑和 ADB 映射已清理。折叠回内屏后，当前页与四个弹窗的只读检查通过，见 `.omx/artifacts/android-055-final-ui/verification.json`；正常前台连接约 30 秒的 10 次采样平均 CPU 0.55%、峰值 2.3%，原始记录为 `.omx/artifacts/android-055-foreground-cpu.txt`。

## 当前行为

Android 使用系统 WebView、xterm、Unicode11 和可选 WebGL。自动模式默认 DOM；显式 WebGL 保留给需要 GPU 的场景。共享 WebGL 附加组件包含设备像素观察保护：当浏览器把 CSS 尺寸误报为物理像素时，按计算样式乘 DPR 恢复一致的 backing canvas，避免裁切和叠字；正常的亚像素舍入仍沿用上游结果。共享 xterm 也会在字格高度为有限正值前忽略原生滚动事件，避免初始布局把视口变成 NaN。`tests/test_webgl_canvas_pixels.py` 在 DPR 1、1.25、1.5、2、3 下验证真实字格、画布和中文／emoji 重绘，真机验收记录见上文；不能仅由桌面 Chromium 推断真机结果。2026-10-22 修正跨端尺寸争抢：手机默认保持清晰字号并在终端区域内滑动观看共享网格，可选全屏总览缩放，切换会话、旋转、打开编辑框均不自动接管；Windows 普通标签切换也不抢占另一端。只有明确点击“接管”才交接共享 PTY 尺寸控制权，当前所有者可随后适配自己的视口。后台确认后才调整本地网格。隐藏或零尺寸窗口不请求最小网格。历史数据若先于尺寸到达，会先进入有界等待队列，随后按确认网格重放。同一终端流的连续实时输出可以跨尺寸 epoch 接收，避免首次连接或账号切换时正文被跳过；流身份变化和字节位置缺口仍会触发恢复。

同一 CLI 的 PTY 只有一套行列数，双方不能各自无约束改变它；本机布局、缩放和显示选择独立，接管是明确的共享尺寸操作。`tests/test_cross_device_layout.py` 用真实桌面和手机页面验证了双向不抢占及明确交接。旧手机页面仍含自动接管逻辑，必须激活新版界面才生效；不能只更新 Windows 就宣布问题全部修复。

来源：`android/assets/mobile/app.js`、`mobile-core.js`；Orca 对照源码 `study/orca/mobile/src/terminal/document/fit-scale.ts`。首次连接、重连、切换会话的回归在 `tests/test_android_replay.py`；共享网格及折叠尺寸回归在 `tests/test_android_mobile_ui.py`。

连接首页显示 APK 版本、构建号、界面版本与最近更新；“测试连接”执行真实 WebSocket、中继连接码和 Token 握手，不执行终端命令。每次 APK 构建自动递增版本；每次界面热更新生成独立时间戳和内容摘要版本。

## USB 热更新

首次需要安装支持热更新的原生 APK。之后 HTML/CSS/JS 与 xterm 资源通过 USB 推送，无需重新安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/push-android-ui.ps1
```

多设备时加 `-Serial <设备序列号>`。脚本分发完整、不可变的版本目录，最后原子切换指针；原生端逐文件验证 SHA256、12 个固定文件名和 5 MiB 总上限。全部校验通过后才加载该版本，不混用两个版本；失败则整体回退 APK。切换版本只重建显示端连接，后台 shell 继续运行。

实现：`HotAssets.java`、`package-android-ui.py`、`push-android-ui.ps1`。这是 USB 开发热更新；签名远程界面更新使用现有中继的 HTTPS /updates/stable/manifest.json，可从右上角 Update 手动检查、下载并验签后激活。原生 Java、权限、签名和 Manifest 的变化仍需升级 APK。

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

打开或切换终端在历史重放完成后滚到底部；大历史分块只在最后一块及提交标记解析完成后恢复输入并定位最新行。手机正常缓冲区最多保留 20,000 行，并忽略仅清空滚动历史的 CSI 3J；清屏 CSI 2J 仍保持终端语义。触摸滑动使用实际字格高度和显示缩放比例，不足一行的像素只累积，不重复触发整行重绘。较早、后台已丢弃的输出无法凭客户端设置补回；重新连接和切换后的重放仍受源主机 2 MiB 有界尾部限制。

真机证据：`.omx/artifacts/android-stream-20261022-003010/verification.json`，包含 300 次刷新、6,498 行可回看历史、真实触摸滚动、输入与关闭同步；测试结束已移除临时电脑并恢复真实中继。

新启动图标源文件为 `android/branding/launcher-master.png`；Android 各密度与自适应资源在 `android/res/`。带图标的个人包为 `dist/android/SuperMonoAgent-Mobile-v0.2.12-icon.apk`，SHA256 `04cba9fdb8f999629bc093926f58d3296a23ac048164bc882a093e1be8cf62e6`；已通过 Android 资源编译和 v2/v3 签名验证。手机系统要求本人指纹才能升级安装，不能将包已生成当作图标已安装。

Windows Codex 右侧空白已在运行界面定位：旧前端保留宽本地网格，而手机曾把后台网格调窄。重新选择 Codex 页签后后台恢复 158×44。源码 `workspace.js` 的 `openView` 已允许主动重选当前页签时重新申请尺寸，后台目录更新仍不抢占；`tests/test_workspace_runtime.py` 同时覆盖新旧后台确认能力。

## 2026-10-22 后续提交与重影修复

手机现已安装原生 0.2.13，本轮界面修复通过 USB 热更新交付，不再安装 APK。编辑发送改为等待远端新回显后再发唯一一次 Enter，支持 Android 输入法事件；无回显 5 秒停止并提示直输。Win3 已切换当前显示服务，原有 4 个 daemon 会话保留。最新真机输入与布局证据分别在 `.omx/artifacts/android-stream-20261022-014319/` 和 `.omx/artifacts/android-device-probe-20261022-014405/`。原因、测试与加密评估见 [客户端评估](transport-crypto-review.md)。


## Relay computer discovery (2026-10-23)

Opening the terminal selector refreshes the existing relay directory connection.
The home connection and manually saved profiles grant access only to their own
relay endpoint and Tokens. The directory automatically lists every online
terminal room authorized by those Tokens, and mobile connects to each computer
for its window catalog. Unselected terminals do not subscribe to output or take
PTY size control. The active terminal and unsent draft stay unchanged when a new
computer appears.

Discovered computers and their credentials remain in page memory; they never
become additional credential roots or persist in localStorage or exported UI
state. A signed UI update restores an automatically discovered selection only
after the new page receives a fresh authorized directory snapshot. Offline or
revoked rooms are removed together with their sockets and catalogs; if the
selected computer disappears, mobile returns to the home computer and retains
the previous computer's unsent draft. Older directory servers can still report
known rooms, but unknown rooms require explicit token-discovery capability and
a valid token index. Repeated selector openings reuse the directory socket and
coalesce refreshes to at most one request per second.
