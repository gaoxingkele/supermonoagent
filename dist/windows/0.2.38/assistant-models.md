# mylib 助手模型对话

Windows 0.2.38 新增右侧模型对话。右侧本地技能建议已在 0.2.37 提供；模型对话需使用包含原生模型桥接的新版本桌面外壳。

点击顶部「mylib 助理」，在侧栏点击「模型配置」。保存配置不会调用模型；点击「发送」或「分析当前窗口」才发出请求。

| 入口 | 填写内容 | 条件 |
| --- | --- | --- |
| 已有模型 API | Chat Completions 兼容地址、模型名称、API 密钥 | 远程地址使用 HTTPS；本机 localhost、127.0.0.1、::1 可使用 HTTP。地址可以填写 `/v1` 基址或完整 `/chat/completions` 路径。无鉴权本机模型可留空密钥。 |
| 本机 Codex 登录 | 模型名称可留空 | 本机原生 Codex 可执行文件已有登录；目前验证 Windows/.NET、CLI 0.164.0、标准 ChatGPT 工作区。配置了 MCP／插件、自定义认证或未知版本时会拒绝调用。 |

API 密钥只保存在本次桌面进程内，关闭应用、切换入口或更改地址后需要重新输入。设置窗口留空表示保留当前密钥，页面不会读取已保存密钥。中继 Token 用于连接电脑，与模型密钥分别处理。Codex 入口由独立 CLI 进程使用已有登录，助手不读取或复制登录文件，不修改用户 CLI 配置。

默认不附带终端内容。需要时勾选「附带当前窗口最近输出」，或点击「分析当前窗口」；后者会发送当前窗口最近有限输出，并保留未发送的提问草稿。「模型配置」中的自动附带选项可以改变默认值。发送前可展开「本次上下文」检查本地采集内容；已知 Token 与模型密钥会在模型请求中脱敏，但不能自动发现所有其他秘密。

对话与草稿按窗口分开，仅保存在内存。切换窗口后，回复归属原窗口；原窗口关闭或连接身份改变时，旧请求结果不再采用。修改模型配置会取消原请求并清除旧对话。点击「取消」后，会等待原请求结束，再允许发送下一条；已经发送到服务端的内容无法撤回。

助手只返回文本、技能建议和待验证的改进线索。它不能向终端输入命令、修改文件或批准 metaRSI 规则。目标项目账本读取仍需该电脑新版后台支持；没有该能力时只显示本机建议。

当前验证使用自有 loopback 假模型、合成凭据与独立终端夹具，覆盖真实隐藏 WebView2 和 CLI。未将假模型成功当成真实账号或云服务验收。Codex 的本地配置属于可信边界；并发修改配置可能初始化其中的 MCP，工具请求及响应仍会被私有网关拒绝。详见 [设计与边界](mylib-assistant-design.md)。

source_path: `src/supermonoagent/assistant_service.py`, `src/supermonoagent/assistant_api.py`, `src/supermonoagent/assistant_codex.py`, `src/supermonoagent/assistant_codex_gateway.py`, `src/supermonoagent/desktop_assistant.py`, `src/supermonoagent/static/mylib-assistant.js`, `src/supermonoagent/static/assistant-chat.js`。

验证证据：`.omx/artifacts/assistant-native/native-assistant-evidence.json`、`.omx/artifacts/assistant-codex/capability-evidence.md`、`tests/test_assistant_chat_integration.py`。
