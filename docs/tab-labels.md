# 窗口标签名称与配色

Windows 工作区顶部页签只显示窗口名称，终端标题显示窗口名称与终端类型；不再重复项目、默认、本机或主机名前缀。电脑与项目信息保留在侧栏菜单的电脑卡片和目录中。

右键点击工作区的页签、窗口标题栏或侧栏窗口名称，选择“修改名称…”或直接点击配色色卡；“恢复默认配色”清除自定义颜色。经典网页版在窗口标题栏和侧栏提供同一菜单。原有侧栏省略号菜单仍提供关闭、移动等操作。

| 方案 | 背景 | 文字 |
| --- | --- | --- |
| 黑底白字 | `#101010` | `#ffffff` |
| 红底白字 | `#9b1c31` | `#ffffff` |
| 蓝底黄字 | `#153e9f` | `#ffe866` |
| 绿底白字 | `#14532d` | `#ffffff` |
| 紫底白字 | `#581c87` | `#ffffff` |
| 橙底黑字 | `#ffbf69` | `#101010` |
| 青底黑字 | `#7dd3fc` | `#101010` |

七种方案的文字对比度都至少为 7:1。仅改变窗口标签、标题和侧栏标识，不改变终端输出颜色。菜单显示当前选中色卡的勾选标记，支持方向键、Enter、Escape 和点击外部关闭。

名称与配色属于后台窗口，同一窗口的多个视图共享设置，并同步给已经连接的客户端。刷新或重新打开页面后仍会保留；后台窗口结束后设置随窗口销毁，当前项目没有跨后台重启恢复会话的能力。

源码：`src/supermonoagent/static/tab-themes.js`、`tab-themes.css`，两套前端在 `workspace.js` 和 `web.py` 中接入。后台在 `hub.py` 的 Window 中保留 `tab_theme`，通过 `daemon.py` 的 `tab-theme` 操作更新，限制为窗口所有者和预设方案。

验证：`tests/test_tab_themes.py` 覆盖所有者校验、非法方案、两套真实浏览器页面之间的选色/改名同步、刷新保留、重置及菜单关闭；`tests/workspace-client.test.cjs` 校验七种方案的文字对比度。菜单截图：`.omx/artifacts/tab-themes/tab-color-menu.png`。

完整验证结果：69 项 Python 测试及 6 项子测试通过，10 项 JavaScript 测试通过；语法/编译检查通过；新打包 EXE 的色卡资源、配色更新广播和隐藏 WebView2 加载检查通过。前端复用一个色卡菜单组件，未增加依赖。

变更文件：`hub.py`、`daemon.py`、`web.py`、`workspace.py`、`static/workspace.js`、新增 `static/tab-themes.js` 和 `static/tab-themes.css`，以及上述测试和根目录 `DESIGN.md`。

试用包：`dist/windows-tabs/02-Workspace.cmd`（桌面）或 `01-Browser.cmd`（经典网页），使用独立端口 6789 和包内 `trial-state`，不会迁移现有终端会话。现有后台和公网中继未自动更新；旧服务端需要更新才能提供新资源并处理配色操作。
