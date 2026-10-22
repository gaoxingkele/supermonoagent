# 窗口自适应修复

2026-10-21。经典页现在以工作区可用尺寸创建终端窗，工作区变化时按比例调整窗口位置、宽高和最大化前的还原尺寸。继续支持手动拖动、调整大小和最大化。`ResizeObserver` 覆盖侧栏、工具栏引起的尺寸变化。

新版工作区把终端内边距放到 xterm 元素，让 FitAddon 同时扣除内边距与滚动条宽度，避免右侧字符被裁切。字体大小保持可读，随窗口变化的是终端行列数。

代码：[`web.py`](../src/supermonoagent/web.py)、[`workspace.css`](../src/supermonoagent/static/workspace.css)。

验证：[`test_responsive_layout.py`](../tests/test_responsive_layout.py) 在隔离的后台和本机无界面 Edge 中测试 1100×760 → 1920×1080 → 850×700 → 390×844 → 1440×900；断言页面无横向溢出、终端填满工作区、列宽不被裁切、实际 PTY 收到对应尺寸，并验证最大化/还原及侧栏切换。截图和几何数据见 `.omx/artifacts/responsive-layout/`。

Windows 新包在 `dist/windows-responsive/`，不覆盖正在运行的旧包。双击 `02-Workspace.cmd` 试用 Windows 工作区，或 `01-Browser.cmd` 试用经典网页；试用入口使用独立端口 6779 和包内 `trial-state`，已有会话未迁移。新包已通过隔离后台、修正资源和隐藏 WebView2 加载检查。

验证结果：Python 67 项测试及 6 项子测试通过，JavaScript 9 项测试通过，Python 编译和 JavaScript 语法检查通过。

网页资源由后台或中继提供：使用公网网页时，需要服务端更新后刷新；仅更新本地客户端不能改变旧中继提供的页面。正在运行的旧后台未自动重启。浏览器验证覆盖共享前端；未对用户的桌面窗口或公网中继执行交互验收。
