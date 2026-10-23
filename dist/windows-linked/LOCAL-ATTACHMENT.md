# 新界面关联已有 daemon

默认启动入口使用本机 `127.0.0.1:6769` 的现有 daemon；没有 daemon 时才启动后台。不再把试用入口固定到空的 `6789/trial-state`。仍尊重用户显式传入的 `--port`、`--state-dir` 和远端 `--url`。

为了保留已有 Kimi/Codex 进程，同时使用最新布局和右键色卡菜单，`frontend.py` 在回环地址提供当前页面，并将 WebSocket 连接转到原 daemon。它不创建 PTY，不迁移/复制会话，也不发送会话终止命令。相同后端和界面版本的 frontend 会被复用。启动器见 `desktop.py`、`launcher.py`，页面初次连接后会为已有会话自动打开页签，见 `static/workspace.js`。

旧 daemon 不支持 `window_themes` 时，frontend 在本机保存配色覆盖并同步给连接该 frontend 的视图；窗口名称和终端字节仍由原 daemon 处理。新版 daemon 声明该能力后，配色也由 daemon 统一处理。旧公网页面不会自动获得本地覆盖的颜色；终端会话仍是同一批。

当前入口：`dist/windows-linked/02-Workspace.cmd`（桌面）、`01-Browser.cmd`（浏览器）。本工作区中旧 `windows-tabs`、`windows-responsive` 的同名快捷脚本已改为转到这个新版包；这些旧脚本依赖同级的 `windows-linked`，分发时应直接使用完整新版包。

验证：`tests/test_frontend.py` 覆盖新旧后端、窗口保留、字节输出、配色兼容、frontend 复用和自动打开已有页签；`tests/test_desktop.py` 覆盖启动器接入。完整测试为 77 项 Python、6 项子测试和 10 项 JavaScript 通过，语法检查通过。

本机实际验收：新打包 EXE 默认关联原 daemon `6769`，复用 frontend `6771`；查询时原后台有 3 个会话，新界面映射 3 个页签并收到已有输出。关闭测试浏览器和隐藏 WebView2 后，原会话 ID 集合未变；Kimi PID 39896、Codex PID 14488、daemon PID 31128 仍保持原启动时间。原始验收摘要：`.omx/artifacts/local-attach/verification.json`，不记录 token 或终端正文。

## Codex 文本宽度与状态栏修正

后续实测发现，显示区域已变宽时，后台 Codex 仍持有另一客户端的 77×44 尺寸，而当前视图为 145×45。现在用户选择页签、点击终端或调整窗口大小时，会为当前终端请求尺寸接管；后台观察者的普通重绘不会接管。历史快照先等待后台提供行列数，再按该尺寸顺序解析，最后适配当前窗口，避免隐藏页签以默认 80×24 误解析全屏输出。

满长度历史尾部可能从 ANSI 转义序列中间截断，造成 `38;49m` 等残片显示。`static/terminal-client.js` 的 `snapshotBytes` 将被截断的快照起点移到下一完整控制序列或换行；未截断的历史原样保留。原始有界历史仍不能保证任意全屏应用的完整状态序列化，但本次 Codex 底栏已经实际复核。

验证：78 项 Python 测试、6 项子测试、11 项 JavaScript 测试通过。真实可见 WebView2 中同一 Codex 会话随窗口放大、缩小，前后台分别一致为 102×34 → 152×46 → 90×31。截图及几何记录：`.omx/artifacts/terminal-sizing/`。已更新 `windows-linked` 包中的两个静态 JS 文件；无须重启 daemon 或终止 Kimi/Codex。
