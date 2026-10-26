# 发布产物

最新签名界面包：**0.2.42**，修复安卓接管／跟随往返切换及桌面重复交接尺寸缓存；保留 0.2.41 高亮、灰色推荐文字、总览入口和默认十六宫格。既有 API2 原生壳可热更新，两端均应更新，无需安装 APK。验证与边界见 [两模式说明](../docs/ui-0.2.42.md)。

最新 Windows 构建：**0.2.47**，在历史裁剪后保留终端光标显隐状态，保留 0.2.46 文本粘贴修复、编辑框与独立文件助手；见 [光标修复与边界](../docs/cursor-caret-diagnosis.md)、[文本修复](../docs/windows-text-paste-0.2.46.md)。当前公开 Android APK 为 **0.2.36**。Android 异常／挂死回滚的修复后真机验收尚未完成，见 [TODO](../docs/TODO.md)。

- Windows 一键安装 ZIP：`windows/SuperMonoAgent-0.2.47-Setup.zip`（通用无凭据版）。自动注入默认 Token 和文件 enrollment 的专用包通过受限下载链接单独提供，不提交 Git。使用方法见 [一键安装说明](../docs/windows-oneclick-install.md)。

- Windows：`windows/0.2.47/`，保留完整目录，运行 `02-Orca-Workspace.vbs`。助手模型配置见 [说明](../docs/assistant-models.md)。
- Android：`android/0.2.36/SuperMonoAgent.apk`，支持保留应用数据的覆盖安装。
- 历史构建保留在各自目录；后续版本使用数字版本号。

所有公开 APK 均不再预填私人连接 Token，旧私人原包只在开发者本地私密备份。旧包重新生成了签名和校验文件，除去连接默认凭据外，其余程序负载逐字节保持。

`trial-state`、`.omx` 等本机运行数据、私钥和凭据不属于公开发布产物，不纳入 Git。`dist` 文件禁止 Git 换行转换，以保持签名资源和构建校验值。
