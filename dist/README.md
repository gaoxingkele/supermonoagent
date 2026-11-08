# 发布产物

完整版本索引：[VERSIONS.md](VERSIONS.md)；机器可读路径、文件大小与 SHA-256：[versions.json](versions.json)。仓库保留 33 个 Android 数字版本、42 个 Windows 数字／hotfix 版本、28 个完整签名界面快照，以及原有旧命名构建目录。

最新 Android APK：**0.2.69（构建 71）**，修复链接脚本遗漏原生白名单导致终端页面无法打开，以及 Update 指向旧频道的问题；已在荣耀 FLC-AN00 保留数据覆盖安装并完成窗口切换、在线检查与草稿保留验证。0.2.67／0.2.68 原包作为历史版本保留，建议安装 0.2.69。详细证据与范围见 [Android 验收记录](../omx_wiki/android-live-updates.md)。

最新 Windows 构建：**0.2.67**，支持上传后复制全部保存路径，保留终端链接打开、复制与焦点修复。

最新签名界面候选：**0.2.70**，改善桌面和手机全部 Cursor CLI 模式的前缀状态高亮；候选包已验证，但尚未发布到在线更新频道，最后核对的在线界面仍为 **0.2.67**。归档历史签名包不表示该包曾上线；旧清单保留原签名和到期时间。说明见 [共享渲染配置](../docs/agent-render-modes.md)。

- [Windows 0.2.67 一键安装 ZIP](windows/SuperMonoAgent-0.2.67-Setup.zip)（通用无凭据版）。专用连接 Token 与文件 enrollment 不提交 Git；使用方法见 [一键安装说明](../docs/windows-oneclick-install.md)。
- [Windows 0.2.67 完整目录](windows/0.2.67/)，运行 `02-Orca-Workspace.vbs`。
- [Android 0.2.69 APK](android/0.2.69/SuperMonoAgent.apk)，支持保留应用数据的覆盖安装。
- [0.2.70 候选界面包](ui/0.2.70/)（桌面、Android、签名清单）。
- 历史版本位于各自目录，已存在的安装包和签名文件保持原始字节。

所有公开 APK 均不再预填私人连接 Token，旧私人原包只在开发者本地私密备份。旧包重新生成了签名和校验文件，除去连接默认凭据外，其余程序负载逐字节保持。

`trial-state`、`.omx` 等本机运行数据、私钥和凭据不属于公开发布产物，不纳入 Git。`dist` 文件禁止 Git 换行转换，以保持签名资源和构建校验值。
