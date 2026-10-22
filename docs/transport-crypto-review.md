# Windows / Android 传输加密与客户端改进评估

2026-10-22。范围：仓库实现、当前运行客户端、标准算法原语实测。不是公网加密部署声明。

## 建议采用的顺序

先统一使用 HTTPS/WSS，优先协商 TLS 1.3，保留 Android 8 等旧设备需要的安全 TLS 1.2 兼容；继续按每个用户、每台电脑的独立 Token 校验目录和输入权限。源码已有 TLS、Origin、Host 与 Token 边界，不必为了网络传输保密再造一套密码协议。运行中的旧公网链路尚未迁移，不能把源码支持 TLS 当作已启用。

依据：[TLS 1.3 / RFC 8446](https://www.rfc-editor.org/rfc/rfc8446.html)；本地 `security.py:tls_context/request_allowed`、`daemon.py`、`relay.py`、`docs/global-workspace-security.md`。TLS 保护网络窃听与篡改；Token 授权决定谁能看到哪个窗口。两者承担不同边界。

若要求“应用消息也由 Token 派生的钥匙加密”，建议固定一种方案：**HKDF-SHA256 + AES-256-GCM**。使用系统密码库，不手写 AES，不对每个字符做慢速密码拉伸，不采用 XOR、ECB 或固定 IV。Windows/Android 均有标准实现；浏览器端可用 Web Crypto，但需要安全上下文，因此不能用当前公网 HTTP 页面直接当作可靠的 Web Crypto 执行环境。

依据：[HKDF / RFC 5869](https://www.rfc-editor.org/rfc/rfc5869.html)、[Web Cryptography API Recommendation](https://www.w3.org/TR/2017/REC-WebCryptoAPI-20170126/)。

## Token 派生方案的必要条件

这是后续协议设计，**尚未接入线上帧**：

- 输入密钥材料来自每个用户独立、高熵的 Token；HKDF 输出 32 字节 AES 密钥。不能让不同用户共享同一个 Token 后，还宣称互相隔离；公开的用户 ID 或房间名不能补救共享秘密。
- 每次连接使用新的随机 salt，派生上下行不同的 key 和 nonce 前缀。HKDF info 绑定协议版本、主机/授权域、连接代次和方向。密钥派生只做一次，不在每条输出上重新计算。
- AES-GCM 使用 128 位认证标签；12 字节 nonce 可由每方向派生的 4 字节前缀和 8 字节递增序号组成。相同 key 下绝不重复 nonce，重连必须换 salt/key；序号和上下文纳入认证附加数据。
- WebSocket 有序传输中校验预期序号，拒绝重放、跳序、认证失败与跨主机帧。加密不能代替会话所有权和路由校验，也不能在“协商失败”时静默回落明文。
- 当前 relay 会收到访问 Token。若同一个 Token 又用于派生数据密钥，中继就能计算该密钥；这种做法不等于“中继不可见”的端到端加密。真正的端到端方案需要端点独享秘密、独立的中继准入凭证和握手证明，不能只在现有数据外包一层 AES 就做该承诺。

因此本轮没有把未经迁移的应用层加密直接塞进现有会话；也没有新增生产依赖、重启 daemon 或变更公网中继。

## 加解密开销实测

工具：`scripts/CryptoBenchmark.java`，复现入口 `scripts/benchmark-crypto.ps1`。同一份 Java 基准在 Windows JDK SunJCE 和已连接 Honor FLC-AN00 / Android 16 的 AndroidOpenSSL 上执行。每个尺寸先预热 200 次，再测 1,000–3,000 次；每条使用唯一 nonce，包含 Cipher 初始化、认证附加数据和加解密缓冲区分配。

原始结果保留在 `.omx/artifacts/crypto-benchmark/windows.json`、`android.json`。以下为单次操作中位数：

| 明文大小 | Windows 加密 / 解密 | 手机加密 / 解密 |
|---|---:|---:|
| 64 B | 1.6 / 1.6 微秒 | 5.3 / 5.3 微秒 |
| 1 KiB | 0.9 / 0.9 微秒 | 6.1 / 6.4 微秒 |
| 8 KiB | 4.9 / 5.0 微秒 | 16.2 / 16.0 微秒 |
| 32 KiB | 17.5 / 17.5 微秒 | 36.5 / 35.7 微秒 |

32 KiB 的测量吞吐约 Windows 1889 / 1856 MiB/s，手机约 759 / 770 MiB/s。HKDF 每次派生均值约 Windows 28 微秒、手机 7 微秒。不同数据尺寸之间包含 JIT、分配与调度差异，不应将个别小包数值解释成严格的算法规模规律。

每条往返均检查明文一致；修改密文和使用不同 HKDF 上下文的密钥都会被认证标签拒绝。这是原语正确性与开销测量，不包含 WebSocket、JNI/JS 调度、WebView、TLS 握手、公网 RTT 或耗电，不是最终协议或移动续航基准。

若未来采用“1 字节版本 + 8 字节序号 + 密文 + 16 字节标签”的记录包络，固定增加约 25 字节。对 1 KiB 约 2.4%，32 KiB 约 0.08%；对单个按键比例大但绝对流量小。首个按键保持立即发送，输出沿用现有有界批处理即可，不应为了加密人为增加长时间聚合等待。

## 本轮已落实的客户端问题

1. **Win3 脏画面**：独立 DOM/WebGL 150 次清屏与改尺寸实验未发现旧字形残留；实际工作区队列可复现“120 列收到、60 列解析”。每帧现保存接收时确认的网格，旧队列处理完前不提前换网格。来源：`workspace.js`、`tests/test_workspace_grid_order.py`、`.omx/artifacts/win3-render-diagnosis/`。
2. **手机编辑回车**：Codex 的粘贴突发处理会吞掉紧邻粘贴的 CR。即使客户端隔开 200ms，后台积压时 Codex 也可能一起读取。修复先等待 xterm 解析到远端输入回显，再等待 200ms 发送唯一一次回车，并绑定原主机、会话和连接代次。所有编辑提交均要求回显，不依赖有限历史里是否仍有 bracketed-paste 模式指令，也不依赖窗口是否叫 Codex；5 秒未回显则明确提示未按回车，无回显输入使用直输。支持 Android IME beforeinput 与结束组合后的 229 Enter。来源：`mobile-core.js`、`app.js`、`tests/test_android_submit.py` 与 `.omx/artifacts/android-composer-submit/`。

## 下一批有价值的改进

| 优先级 | 改进 | 原因 |
|---|---|---|
| P0 | 公网迁移 WSS，显示实际连接安全状态 | 当前旧服务与源码版本不同；用户应能区分“支持加密”和“此连接已经加密” |
| P1 | 输出帧携带生成时的 grid revision | 本轮修复接收顺序；生成时网格需要协议级标记才能彻底覆盖 resize 前已缓存输出 |
| P1 | Windows 标签先显示窗口名/Agent，再显示电脑/项目 | 实际界面中多个标签前缀相同，重要名称会被截掉；应保留多电脑归属但突出任务名 |
| P1 | 更明确的历史范围与提交完成反馈 | 有界尾部重放不等于完整历史；发送请求、输入送达、命令执行是三个不同状态 |
| P2 | 加密长连接与弱网续航实测 | 原语很快，但无线唤醒、重连与渲染通常更影响手机体验；不能用 AES 吞吐代替耗电证据 |
