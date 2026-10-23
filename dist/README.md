# 发布产物

最新构建：**0.2.31**。Android 异常／挂死回滚的修复后真机验收尚未完成，见 [TODO](../docs/TODO.md)。

- Windows：`windows/0.2.31/`，保留完整目录，运行 `02-Orca-Workspace.vbs`。
- Android：`android/0.2.31/SuperMonoAgent.apk`，支持保留应用数据的覆盖安装。
- 历史构建保留在各自目录；后续版本使用数字版本号。

所有公开 APK 均不再预填私人连接 Token，旧私人原包只在开发者本地私密备份。旧包重新生成了签名和校验文件，除去连接默认凭据外，其余程序负载逐字节保持。

`trial-state`、`.omx` 等本机运行数据、私钥和凭据不属于公开发布产物，不纳入 Git。`dist` 文件禁止 Git 换行转换，以保持签名资源和构建校验值。
