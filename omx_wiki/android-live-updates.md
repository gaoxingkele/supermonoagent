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

维护入口：[操作与验证说明](../docs/android-live-updates.md)。APK 内置个人默认连接参数，不将 Token 写回仓库或日志。

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

目前支持 USB 热更新，未部署互联网 OTA。物理铰链和所有 IME 尚未穷尽验收。手机原生壳为已安装的 0.2.4，通过热更新交付最新界面；0.2.5 已构建，系统要求指纹才能继续安装，因此未替换手机原生壳。避免反复触发安装弹窗，后续 UI 调整沿用 USB 更新和截图探针。
