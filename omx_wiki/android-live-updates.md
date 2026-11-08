---
type: Decision
category: decision
status: active
created: 2026-10-22
source_paths:
  - android/assets/mobile/app.js
  - android/assets/mobile/mobile-core.js
  - android/src/dev/supermonoagent/mobile/MainActivity.java
  - android/src/dev/supermonoagent/mobile/HotAssets.java
  - scripts/append-apk-dex.py
  - scripts/test-android-ui.py
  - scripts/test-android-stream.py
  - .omx/artifacts/android-stream-20261022-000153/verification.json
  - .omx/artifacts/android-device-probe-20261022-000901/verification.json
confidence: high
---

# Android 热更新、网格一致性与真机验收

维护入口：[操作与验证说明](../docs/android-live-updates.md)。当前交付状态以 [运行时配置与回滚验收](runtime-config-and-recovery.md) 为准；以下早期版本事实仅作历史证据。公开 APK 不嵌入个人 Token。

## 保持的约束

- 手机默认请求后台采用手机网格；收到后台尺寸后才改变 xterm 行列数。共享模式只缩放，不自动改变后台尺寸。来源：`android/assets/mobile/app.js`。
- 初次订阅的 snapshot 可能先于 controls 到达。必须有界等待该会话尺寸，不能先用默认 80×24 解析再事后 resize。批次不能跨越不同网格的字节边界。来源：`daemon.py` 的 OP_SUBSCRIBE、`mobile-core.js`、`test_android_replay.py`。
- Orca 移动端同为 WebView/xterm/WebGL；其固定网格缩放原则有参考价值，不应把行列失配归咎于 GPU。来源：`study/orca/mobile/src/terminal/document/fit-scale.ts`。
- `jar uf` 会重写 Android 不可读的压缩资源头；使用标准库 ZIP 追加 DEX 并验证本地头。来源：`append-apk-dex.py`；实机 404 经诊断为 AssetManager 读取内置 index.html 失败，修正后加载和中继连接通过。
- USB 更新仅加载经过白名单、SHA256 和总大小校验的完整不可变版本。版本指针最后切换；任何校验失败整体回退 APK。来源：`HotAssets.java`、`push-android-ui.ps1`。
- 每次 APK 构建递增 versionName/versionCode；每次界面更新使用时间戳与内容摘要。来源：`build-android.ps1`、`package-android-ui.py`。

## 真机事实

Honor FLC-AN00，Android 16。13 组实际页面/截图检查已覆盖横竖屏、真实键盘、四种弹窗、首页连接测试，验证报告无错误。300 次 ANSI 刷新在 120×30 共享网格中保持第 100 列、中间和底部标记位置；中文、ASCII、Ctrl-C 和后台删除同步通过。来源为本页列出的两份原始 verification.json。

测试必须恢复原电脑、尺寸模式、输入状态和 ADB 转发。失败路径不能用隐藏元素的可访问性计数判定配置不存在：测试电脑曾因此残留并显示等待重连，现已修正清理并完成通过的复测。来源：`test-android-stream.py` 和其验证报告 cleanup 字段。

桌面工作区“×”保留后台终端，“结束”才删除；真正删除已验证会同步到手机。不得在未改变后台语义时，单方面隐藏手机条目冒充关闭。来源：`workspace.js`、`hub.py` 与真机 closure 回归。

## 边界

2026-10-22 后续：终端列表已改紧凑单行和活动圆点；默认保留 20,000 行，忽略应用 CSI 3J 清除手机历史，触摸按真实缩放字格计算。实机 6,498 行滑动、300 次刷新、输入和关闭同步通过，证据：`.omx/artifacts/android-stream-20261022-003010/verification.json`。新图标资源在 `android/branding/` 与 `android/res/`，个人 APK `dist/android/SuperMonoAgent-Mobile-v0.2.12-icon.apk` 已编译签名，等待手机本人指纹安装。Windows Codex 经页签重选恢复 158×44，源码补齐重选当前页签时申请尺寸，并有 runtime 回归覆盖。

历史状态（2026-10-22）：当时仅验证 USB 热更新，手机原生壳为 0.2.4。2026-10-24 已部署 HTTPS 签名更新，并完成 API2 原生壳安装与正常更新；异常回滚的最新真机边界见新状态页，不沿用本段旧版本结论。物理铰链和所有 IME 尚未穷尽验收。

## 2026-11-08：0.2.67 终端页面无法打开

证据：对实际 `android/assets/mobile/index.html` 调用原生 `UpdateDocument.html`，在修复前抛出 `java.io.IOException: Unlisted resource refused`。0.2.66 新增 `vendor/xterm-links.js` 时，只更新了页面、APK 打包和远程发布脚本，遗漏 `LocalAssets.java`、`RemoteUpdates.java` 与 USB 打包清单。`MainActivity.shouldInterceptRequest` 捕获页面重写异常后返回 404，终端页面因此无法加载。上轮仅校验 APK 签名与打包字节，没有执行实际原生页面重写，未覆盖此故障。

修复版为 `dist/android/0.2.68/SuperMonoAgent.apk`（versionCode 70）。修改 `android/src/dev/supermonoagent/mobile/{LocalAssets,HotAssets,RemoteUpdates}.java`、`scripts/package-android-ui.py`，同步完整 19 文件界面；签名更新仍接受精确的旧版 18 文件快照，升级与回滚按各自清单处理，USB 不完整快照整体回退 APK。没有新增依赖或资源混用。版本记录在 `android/AndroidManifest.xml` 和 `android/CHANGELOG.txt`；回归在 `tests/test_android.py`、`tests/test_android_hot_update.py`、`tests/test_android_remote_updates.py`。

验证：原生 HTML 重写修复前失败、修复后通过；Android/USB 14 项通过、1 项跳过（Windows 符号链接权限），另有 19 个子测试通过；签名更新 28 项通过；移动端窗口切换、多电脑和链接 5 项通过。APK v2/v3 签名、ZIP CRC、源文件 SHA256、原生白名单与 APK/快照清单一致性通过。证据：`.omx/artifacts/android-window-open-0.2.68/verification.json`。

补充验证：智能体模式与渲染器恢复 8 项通过，本轮相关回归共 55 项通过。额外的房间发现组合测试在 24 项通过后停滞，已停止该测试进程，未将完整房间发现套件列为通过；未改动房间发现实现。

边界：本轮 ADB 没有检测到手机，尚未将 0.2.68 安装到手机，也未核对手机当前安装版本与现场日志。上述根因已由真实页面和原生 Java 复现，但与用户手机现场的对应关系仍待 USB 连接后确认。原生白名单需要 APK 升级，单独发送新版界面不能修复旧原生壳。

2026-11-08 后续真机验收：手机重新连接后确认原安装为 0.2.67（构建 69），保留数据覆盖安装 0.2.68（构建 70）成功。手机实际加载 bundled 页面，`xterm-links.js` 返回 200 且 SHA256 与源码一致；终端列表 18 项，触摸切换到另一窗口成功，连接及 ready 正常，无页面异常；随后返回原窗口，未发送终端输入。证据：`.omx/artifacts/android-window-open-0.2.68/device-verification.json` 与 `window-open-physical.png`。后续报告的 Update 检查失败是独立的旧更新地址问题，不能据此判定窗口仍未修复。

## 2026-11-08：0.2.69 修复 Update 地址并完成真机验收

根因：`RemoteUpdates.DEFAULT_FEED` 仍指向 `https://117.50.48.22/updates/stable/android/manifest.json`，该地址返回 0.2.48、16 文件的旧签名清单；实际 Java 校验抛出 `Unexpected asset set`。正式发布器使用 `https://117.50.48.22/updates/stable/manifest.json`，返回 0.2.67、19 文件，实际 HTTPS 下载和验签成功。两份清单密钥和有效期正常；不是关闭 HTTPS 或签名校验才能解决的问题。只替换旧地址的 manifest 还会使相对 ZIP 路径落到不存在的 `android/releases/`，因此源码改用正式频道。原始公开清单与 JVM 输出保存在 `.omx/artifacts/android-update-feed-20261108/`。

修改 `android/src/dev/supermonoagent/mobile/RemoteUpdates.java` 的默认地址，新增 `tests/test_android_remote_updates.py` 回归，锁定原生地址与发布器默认地址一致及 ZIP 相对路径正确。修改前失败，修复后远程更新套件 29 项通过；未调整公网服务器。

最终手机原生版本 **0.2.69 / 构建 71**，保留数据覆盖安装成功；APK v2/v3 签名、ZIP CRC、资源 SHA256 和 DEX 内正式更新地址检查通过。手机实际点击 Update 显示 `UI is current`，活动签名界面为 0.2.67；原生 APK 版本与界面版本独立。再次触摸切换另一终端成功，列表 18 项，连接和 ready 正常，无页面异常，随后返回原窗口；已有草稿仍在，没有发送终端输入。临时 ADB 转发已删除，原有 tcp:5999 保留。证据：`.omx/artifacts/android-update-feed-0.2.69/verification.json`、`device-verification.json`、`draft-verification.json`、`update-check-dialog.json` 与 `window-open-physical.png`。
