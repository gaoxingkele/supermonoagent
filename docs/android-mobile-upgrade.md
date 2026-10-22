---
type: Review
title: Android v0.2.0 — Orca Mobile 终端参考与升级
status: active
updated: 2026-10-21
owner: llm
tags: [android, terminal, mobile]
source_paths:
  - study/orca/mobile/src/terminal/TerminalWebView.tsx
  - study/orca/mobile/src/terminal/document/terminal-init.ts
  - study/orca/mobile/src/terminal/terminal-write-coalescer.ts
  - study/orca/mobile/mobile-terminal-direct-input-default.md
  - android/assets/mobile/app.js
  - android/src/dev/supermonoagent/mobile/MainActivity.java
confidence: high
---

# 安卓端升级：从远端页面外壳到本地手机终端

本轮只改 Android 应用、APK 构建和对应测试/文档。Windows 客户端、共享 workspace 页面、daemon、中继和协议没有因本轮升级而改动。

## Orca Mobile 如何还原终端

源码证据：`study/orca/mobile/src/terminal/TerminalWebView.tsx:79` 使用 WebView，开启 nested scroll 并固定 Android textZoom=100；`document/terminal-init.ts:27` 创建 xterm、WebGL 与 Unicode11，保留 ANSI、备用屏及字格语义。它不是把终端输出转换成普通聊天文本，也不是用原生文字控件绘制整张终端。

`mobile-terminal-direct-input-default.md` 描述默认直接输入、可切回整行编辑，并要求切换终端时不自动弹键盘。Orca 的原生 TextInput 接管 IME，WebView 主要负责终端显示；本项目保留更轻的系统 WebView，使用 xterm 自身的输入/组合文字处理，加独立整行编辑框，未移植 React Native TextInput。

`src/terminal/terminal-write-coalescer.ts:1` 的 48ms 合并用于降低 RN↔WebView postMessage 开销。我们的 WebSocket 已直接进入页面，没有这段跨框架 IPC，因此没有照搬该定时器。采用空闲首包立即写入，繁忙输出按帧合并，单批不超过 32KiB、积压不超过 512KiB；溢出明确断线重连，避免后台无限积压。

`terminal-output-streaming-findings.md` 记录 WebView/xterm 就绪前消息可能丢失。本应用先初始化终端，再订阅会话，并串行解析 snapshot/live 输出；连接代次和销毁标记防止旧回调写入新终端。历史仍是后台已有的有限尾部重放，不宣称任意 TUI 的完整屏幕状态恢复。

## 已实现的安卓体验

- APK 内置手机专用 HTML/CSS/JS 及 xterm/Unicode11/WebGL 资源，不再下载桌面 workspace 页面；通过现有 WebSocket 直连后台或中继，同一真实 shell 所有权不变。
- 单终端主视图，抽屉式会话切换/新建，顶部连接状态与尺寸接管；切换会话不主动聚焦键盘。
- 直接输入与整行编辑切换；辅助 Esc、Tab、Ctrl、方向键；组合输入期间 Enter 不提交编辑框，完成组合后再处理排队控制键。真实中文 IME 行为仍需手机验证。
- 滑动手势与点击唤键盘分离；历史滚动按行、按帧合并，提供“回到底部”。备用屏滑动映射方向键，不冒充支持所有鼠标 TUI 手势。
- 字号调节、DOM/WebGL 选择、WebGL 丢失回退；复制选中/可见文本通过可长按的文本框完成。
- 原生 textZoom=100，关闭自动缩放；系统栏与软键盘 Insets 避让；返回键优先收键盘。
- 进入后台主动断开显示端连接，服务器 shell 继续运行；回前台重新建立显示和订阅，清理旧输入及旧渲染回调。网络错误与渲染进程退出提供明确恢复入口。

## 本地页面与安全边界

采用窄范围的本地资源拦截，设计参考 [Android 官方本地内容指引](https://developer.android.com/develop/ui/views/layout/webapps/load-local-content)。文档来源绑定用户配置的服务 origin，但 `/mobile/` 下的页面和资源只由 APK 提供，不向服务端下载页面。仅允许打包清单中的 10 个文件，禁止任意文件路径、file/content 访问、外站页面和原生 JavaScript bridge。

HTTP 端点对应 HTTP 本地文档来源，HTTPS 端点对应 HTTPS 来源，仍保持 `MIXED_CONTENT_NEVER_ALLOW`。没有使用固定公网合成域名，减少与服务无关的跨来源行为。桌面 Edge 对拦截响应没有真实私网响应地址，测试时还需要向该测试来源显式授予 local-network-access 权限；来源绑定本身不等于通过所有网络权限检查。根据 [Android 官方权限说明](https://developer.android.com/privacy-and-security/local-network-permission)，WebView 继承宿主权限，当前 targetSdk35 的 INTERNET 权限提供局域网访问；将来提升至 target37 时需要另行适配运行时权限，当前未假称已测。连接配置由本地 fragment 传入，页面读取后立即清除；令牌仅存内存，不写原生偏好设置。系统 WebView 仍需安装；未捆绑完整 Chromium。

## 产物与验证

试用包：`dist/android/SuperMonoAgent-Mobile-v0.2.0.apk`，132,237 字节，沿用原调试签名，可升级安装原试用 APK。versionCode=2、versionName=0.2.0、minSdk=26、targetSdk=35。旧版 APK 留在 `.omx/android-v1/`。

验证覆盖：实际 Java/Android SDK 编译、DEX、签名 v2/v3、ZIP 对齐、全部打包资源与源码哈希一致；Java URL/资源白名单测试；Node 队列/触控/控制键/组合提交/实际 WebSocket close API；隐藏 Edge 的 390×844、844×390、320×420 布局、中文直接输入、Ctrl、整行编辑、历史滚动、定向会话切换和暂停/恢复。

队列压力回归将 1,000 个 512 字节片段无损合并为少于 20 次解析调用，单批仍不超过 32KiB。这证明批处理与上限，不是安卓设备 CPU/耗电或输入时延的实测结论。截图与 JSON：`.omx/artifacts/android-v2/`。

复现：

```powershell
node --test tests/android-mobile.test.cjs
.venv/Scripts/python.exe -m pytest tests/test_android.py tests/test_android_mobile_ui.py -q
powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1
```

本机 adb 未发现设备。APK 安装启动、Android System WebView 的实际中文输入法、触摸惯性、键盘动画、GPU/耗电和真实网络切换仍需实体设备验收。没有修改或重启公网服务；内置页面无需服务器提供 `/workspace`，但后台/中继仍须支持上一轮的多会话协议。
