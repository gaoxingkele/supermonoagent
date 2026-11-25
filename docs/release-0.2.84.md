# 0.2.84 Hermes 高亮热更新

发布日期：2026-11-25。本次只发布 Windows 与 Android 的签名 stable 界面快照，原生安装包继续使用 0.2.83 / Android code 85，不要求重新安装外壳。

## Hermes 显示改进

- 新增 Hermes 自动识别和手动模式；窗口标题、启动命令或独特的 `☤ Hermes` + `❯` 界面组合均可识别。
- 品牌与思考状态使用蓝色，工具调用使用紫色，命令和链接使用青色，路径、字符串、数字及上下文百分比使用黄色。
- `│` 差异块的新增、删除和区块头分别使用绿色、红色和黄色；`YOLO`、错误和成功状态分别着色。
- 普通正文不整行染色，程序原生 ANSI、真彩色、暗淡提示和输入区保持原样；切换模式不写入终端、不重启 Hermes。

Hermes 规则内嵌在双端已有的 `terminal-client.js`，不新增资源路径，保持 Native API 2 与原有白名单兼容。规则依据 [Hermes CLI 官方文档](https://hermes-agent.nousresearch.com/docs/user-guide/cli) 和 NousResearch/hermes-agent `f85f33bf534d0a4ac88c348d34a4c9ddb424dcb7`。

## 验证与边界

- 共享 Node 回归 18 项通过，包含 Hermes 结构、差异、上下文、正文与原生样式。
- Android 真实 Chromium 回归 5 项通过，覆盖七种模式、DOM/WebGL、持久化和零终端输入。
- Windows Hermes 独立真实浏览器回归通过，确认无元数据时自动识别、内置配置无额外 JSON 请求、原生 RGB 保留且零终端输入；另 1 项跨主机隔离回归通过。
- 发布工具 60 项、固定资源与 API2 打包 2 项通过。公网回读验签及候选字节一致性证据为 `.omx/artifacts/release-0.2.84/publication-verified.json`。
- Windows 全文件另有 3 个 0.2.83 基线已存在的 Cursor 块形/条形光标断言失败；本次未修改该无关行为，不能将全文件写成全部通过。附加按钮矩阵在首个既有 Codex 用例的隐藏 `history-bottom` 按钮处超时，未进入 Hermes，也不计为通过。
- 未直接操作 `JINLONG3090` 的 Hermes 进程，也未声称实体设备已经下载并激活；客户端检查更新并重载后生效。
