---
type: Decision
category: decision
status: active
created: 2026-10-22
confidence: high
source_paths:
  - android/assets/mobile/app.js
  - android/assets/mobile/mobile-core.js
  - src/supermonoagent/static/workspace.js
  - tests/test_android_submit.py
  - tests/test_workspace_grid_order.py
  - scripts/CryptoBenchmark.java
  - .omx/artifacts/client-fixes-20261022/verification.json
  - .omx/artifacts/android-composer-submit/verification.json
  - .omx/artifacts/android-stream-20261022-014319/verification.json
  - .omx/artifacts/android-device-probe-20261022-014405/verification.json
source_urls:
  - https://www.rfc-editor.org/rfc/rfc8446.html
  - https://www.rfc-editor.org/rfc/rfc5869.html
  - https://www.w3.org/TR/2017/REC-WebCryptoAPI-20170126/
---

# 客户端提交、跨网格重影与加密边界

维护入口：[完整评估与性能数据](../docs/transport-crypto-review.md)。

## 手机提交：不能只依赖客户端延时

真实 Codex 0.163.0 会将粘贴突发期间的 Enter 作为编辑输入；ConPTY 会剥离 bracketed-paste 包装。即使客户端把粘贴和回车隔开 200ms，后台忙时仍可能一次消费，因此单纯延时不是可靠修复。

当前编辑提交先观察 xterm 新解析的远端输入回显，再等待 200ms 发唯一一次 Enter。所有编辑提交均使用屏障，不能只检查 agent 名称或 bracketedPasteMode：用户窗口可以命名“手机端”，长时间后的有限快照也可能不再含原始模式指令。5 秒无回显则取消回车并提示改用直输；切换主机/会话、断线和后台会取消；不重发、不再次粘贴。等待时按钮显示“提交中…”。

证据：真实 `/quit` 本地命令对照，旧路径失败；新的六条路径包含后台积压 600ms、失去模式指令、自定义标题、普通 Enter、229 Enter 和 beforeinput。没有向用户既有 CLI 或模型发送测试命令。见 composer-submit 验证文件。

## Win3 重影：先查网格，不能盲目换渲染器

独立 DOM/WebGL 清屏与改尺寸均正常。实际队列复现为 120 列收到的旧字节按最新 60 列解析。修复逐帧捕获接收时网格，旧帧未完成前不提前应用最新尺寸。

运行的 6773 旧 frontend 已替换为当前显示服务，后台仍为原 6769；旧显示窗口已关闭，新显示窗口恢复同一真实终端。4 个后台会话保留，未重启 daemon/CLI。新冻结包 `dist/windows-render-fixed/` 已通过隐藏 WebView2 smoke；原 `windows-workspaces-current/02-Workspace.cmd/.vbs` 已指向它，旧入口备份在 win3-update 证据目录。

边界：协议仍未携带字节生成时的网格代次。本轮解决接收队列顺序，不能宣称已经消除所有后端 resize 与输出生成竞争。

## 加密：标准优先，明确 Token 和信任边界

先迁移 WSS/TLS，保留每用户独立 Token 和会话权限。如果追加 Token 派生的应用层加密，采用 HKDF-SHA256 + AES-256-GCM、每连接新 salt、上下行独立 key/nonce、认证标签与序号，拒绝重放及认证失败。不要直接截取/补齐 Token 当 AES key。

同一 Token 分给不同用户就不能声称用户间保密；relay 已知 Token 时也能推导对应密钥，不能据此声称对 relay 端到端保密。该方案为评估，未修改线上帧、未新增生产依赖、未部署公网加密。

Windows 与实际 Android JCE 基准中，1 KiB 加密中位数约 0.9 / 6.1 微秒。测量包含密码库初始化和缓冲分配，不包含网络、WebView、TLS 握手或电池功耗。原始结果与可复现入口见评估文档。

## 验证与测试工具约束

功能回归 137 项 Python、21 个子测试通过，1 项因符号链接权限跳过；最后 UI 提示改动经 28 项 Node 与真机探针验证。物理设备 300 次刷新、约 6,500 行历史、触摸、输入、关闭同步通过；13 组布局/横竖屏/键盘截图无错误。

原生设置页会清空内存 Token，通用 APK 不一定内置 Token。因此 `test-android-ui.py --native-connection-test` 必须显式传入开发凭据文件；不得无保护地切换页面后留下验证失败连接。密码输入用逐字符方式，取消可选密码管理器更新弹窗，不保存或打印凭据。打开软键盘要在原生 Android 输入框坐标触摸：CDP 仅渲染器触摸无法恢复被 clearFocus 的原生 WebView 焦点。采集截图前同时等待网格、连接与 ready 稳定。
