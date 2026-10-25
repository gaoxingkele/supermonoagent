# Android 总览回退修复：UI 0.2.40

0.2.39 发布时为桌面修改复用了公网 0.2.34 Android ZIP。手机上已有的九／十六宫格功能没有进入这个旧 ZIP；共享频道的新 revision 仍被 Android 判为可更新，造成旧内容覆盖已有界面。签名正确、与旧频道字节相同，只证明完整性，不能证明设备功能没有倒退。

0.2.40 从当前双端源码完整生成界面包，恢复右上角“调试”总览入口。HTML 默认选中十六宫格、应用初始容量 16、核心异常值回退 16、CSS 初始四列；用户仍能切换九宫格。显示、分页、状态灯、提示滚动及只读订阅保留。

发布护栏禁止向当前共享生产根目录用 reuse／preserve 模式正式提升旧 Android 负载；在任何 SSH 调用前拒绝。本地构建、暂存、自定义平台根目录及正常双端完整发布仍可用。此前的描述符 CAS 只能保护服务器现有负载，无法发现设备内置／本地已有的新功能，因此不能把它当作避免功能回退的保证。

新增标准 pytest 回归，从真正生成的 Android ZIP 提供页面资源，检查默认十六宫格及八种窗口／排列组合，验证分页、离线、后台恢复、同 session ID 跨电脑隔离和关闭预览。测试只允许读取／订阅操作，没有终端输入或 resize，当前终端维持原尺寸。

验证：60 项 pytest 通过（含发布护栏），Node 总览单测 4 项通过；当前源码、待发布 ZIP、从公网重新下载验签的 ZIP 都通过相同界面测试。Windows 资源与 0.2.39 仅 Git checkout 换行符差异，规范化后文本一致。没有改 Android 原生代码，不需要重装 APK。

发布序号：`1792906914361268`。Android ZIP SHA-256：`60ce81a5835744e561852e4015cbcbb35d23965490b54b892a050365cf74bd3b`。公网双平台签名与文件目录回读通过；当前手机 USB 连接不可用，未宣称已在该手机上完成实际更新。设备侧通过“Check UI updates／检查界面更新”下载 0.2.40 并保存草稿重载。

source_path: `android/assets/mobile/{index.html,app.js,app.css,mobile-core.js}`, `android/tests/debug-grid-ui.py`, `android/tests/debug-grid.test.cjs`, `tests/test_android_overview_release.py`, `scripts/ui_update_release.py`, `tests/test_ui_release_reuse.py`。

证据：`.omx/artifacts/android-overview-0.2.40/{candidate-verification.json,publication.json,https-verification.json}`、`.omx/artifacts/android-overview-regression-fixed.xml`、`.omx/artifacts/android-debug-grid/browser/verification.json`。
