# 复制粘贴文件至目标电脑

开发状态：接收核心、直连/中继协议、Windows 剪贴板入口及上传界面源码已接通；隔离浏览器使用测试文件的上传链路已通过，真实系统剪贴板和已安装客户端仍未验收，不代表已安装版本可用。

## 已实现的后端

- `protocol.py` 新增 OP_FILE_TRANSFER=21 和 `file_transfer` 能力。旧后台不声明此能力，客户端须提示升级，不能将二进制文件写入终端输入。
- `daemon.py` 将该操作路由至独立 `FileTransferRPC`，只允许非委托的后台所有者；每次操作检查目标 session 的存活、所有者及频道范围。
- 默认接收目录为当前后台状态目录下的 `received-files`；可在后台启动时通过 `--receive-dir` 指定。网络请求不能指定落盘目录。
- 接收器最多 4 个并行文件，单文件最多 512 MiB，分块最多 96 KiB；校验长度与 SHA-256 后，使用同文件系统硬链接原子发布。重名自动编号，不覆盖；不支持硬链接的文件系统返回错误。
- 连接断开取消其未完成传输；已完成文件保留。目标窗口关闭后不能继续提交。空闲超过 5 分钟的未完成传输会回收，覆盖开始响应丢失的情况。
- 文件请求使用独立有界队列和专用执行器；慢磁盘不会占用终端输入的线程池。文件已原子发布后，暂存清理失败不再误报传输失败，后续会重试清理。

## 请求与响应

所有消息使用既有二进制帧封装，opcode 为 21，payload 为 UTF-8 JSON。每次请求都带最多 64 字符的 `request`，用于响应匹配；并带原始 session ID。

```json
{"request":"u1","action":"begin","session":"SESSION","name":"report.pdf","size":123,"sha256":"64位十六进制摘要"}
```

成功响应包含 `ok:true`、`request`、`id`、`received:0`。后续操作必须来自同一连接且绑定同一 session。

```json
{"request":"u2","action":"chunk","session":"SESSION","id":"UPLOAD_ID","offset":0,"data":"严格Base64分块"}
{"request":"u3","action":"finish","session":"SESSION","id":"UPLOAD_ID"}
{"request":"u4","action":"cancel","session":"SESSION","id":"UPLOAD_ID"}
```

分块响应带累计 `received`；提交响应带保存后的 `path`。失败响应为 `ok:false`、`error_code` 和通用错误信息。客户端应固定开始时的电脑/session/连接 generation，切换页签不改变目标；连接重建后不能复用旧传输 ID。

## 验证与剩余工作

`tests/test_daemon_file_transfer.py` 使用真实 loopback WebSocket，分别经直连和隔离中继上传 150 KiB 多分块文件并逐字节核对，同时检查断线暂存清理及终端输入为空。接收核心与 RPC 的边界、越界路径、Windows junction、配额、错误所有者等用例位于 `test_file_transfer*.py`。

`windows_clipboard.py` 提供原生 CF_HDROP、PNG、24/32 位 BI_RGB，以及标准 32 位掩码的 DIB/V4/V5 读取，使用随机源 ID 分块读取，不接受页面指定本地路径。`DesktopFiles` 仅在粘贴操作时读取内容，后台只查询格式是否存在；格式未改变时不重复注入页面。`clipboard-files.js` 在终端文件粘贴事件中固定目标，提供进度、取消及保存路径，普通文本继续走原来的 xterm 粘贴。顶部菜单也有“粘贴文件到当前窗口”入口。浏览器 File 数据逐个准备与释放，最多 32 文件，内存散列路径限单文件 64 MiB；Windows 原生路径保持 512 MiB 限额。客户端已装版本需更新原生程序，接收电脑需更新后台。

`tests/test_clipboard_file_ui.py` 在真实 Edge 页面和 WebSocket 后台中，通过注入的测试剪贴板提供文件，验证完整内容落盘、文件不进入终端输入、随后普通文本仍只作为文本输入。`tests/test_windows_clipboard.py` 测试格式转换及源句柄生命周期，未修改真实系统剪贴板。

原生隐藏 WebView2 已完成真实桥接上传与建议显示，192000 字节及 SHA-256 一致，PTY 输入为 0，测试子进程全部退出。取消、切换目标、重连、上传中普通文字、分批内存读取及慢磁盘响应均有回归；`test_file_transfer_responsiveness.py` 还将默认线程池限制为一个线程，确认同连接输入和心跳仍及时响应。

真实系统剪贴板仍未验收：本环境 `CreateWindowStationW` 及只读 `OpenClipboard(None)` 均返回拒绝访问；没有覆盖或修改用户剪贴板。证据见 `.omx/artifacts/file-paste/{native-clipboard-evidence.json,clipboard-metadata-preflight.json}`。后续完成实际剪贴板验收及打包发布；接收目录当前支持后台参数配置，尚无图形设置入口。不得以注入的测试剪贴板替代实际系统验收。
