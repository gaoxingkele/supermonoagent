# Windows 0.2.46 文本粘贴修复

用户报告 Claude CLI 文本无法粘贴后，隔离测试复现：终端 paste 事件已携带非空 text/plain、没有浏览器 File，但旧 native 文件格式 beacon 为 true，捕获层仍 preventDefault 并等待 clipboard_has_files。原生判断挂起时文字永远不到 PTY；误报文件时则错误进入文件读取流程。

修复让本次事件中的纯文本直接沿原 xterm paste 路径处理，不等待原生附件探测，不读取文件剪贴板，也不依赖文件传输是否繁忙。真实浏览器文件事件仍保留原附件路径。保留的异步文件 fallback 额外检查原连接代次／凭据及活动 pane，避免延迟回调串写。

三类新回归覆盖 native 判断挂起／误报文件／抛异常，旧源先真实失败、修复后通过。与文件粘贴、编辑框、焦点组合共 **26 passed**，确认普通文字只到目标一次，不额外发送 CR／LF。原生编译包脚本与修复源逐字节一致，真实隐藏 WebView2 正常加载编辑入口且 PTY 输入 0。

Windows 完整程序升级为 0.2.46，原有文件交换 profile 和 Token 保留；不重启现有 daemon／PTY。共享 UI 频道保持 0.2.42，未将未知并行 Android WIP 随此次修复发布。此检查不等于已在用户电脑实测 OS 剪贴板；现有 Cursor caret 的独立待确认项没有被冒称解决。

源修改：`src/supermonoagent/static/clipboard-files.js`；回归：`tests/test_text_paste_priority.py`。证据：`.omx/artifacts/text-paste-priority-{red,green}.xml`、`native-composer46-acceptance.json`。

另经裸 xterm 对照确认 Windows Control+V 原键映射会发出 SYN(0x16)。产品仅在 Windows 为未带 Shift/Alt/Meta 的 Ctrl+V 退出 xterm 键编码，让浏览器／原生快捷键接手，不主动读取 OS 剪贴板。真实 Edge 物理按键验证窗口冒泡未被产品取消、PTY 不再收到 0x16；测试在最后取消默认动作以免读取用户剪贴板，再用自有 ClipboardEvent 验证文本精确一次到达。裸 xterm 对照仍发 0x16。键级旧源也已先红后绿。证据：`.omx/artifacts/windows-ctrl-v-{key-red,final}.xml`。
