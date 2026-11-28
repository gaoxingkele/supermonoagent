# 发布产物

## 0.2.85 · 2026-11-29

| 产物 | 下载 | 说明 |
| --- | --- | --- |
| Windows 通用安装版 | [SuperMonoAgent-0.2.85-Setup.zip](https://117.50.48.22/updates/installers/0.2.85/SuperMonoAgent-0.2.85-Setup.zip) | 原生后台和桌面外壳已发布。[Windows 签名清单](https://117.50.48.22/updates/windows/stable/manifest.json) |
| Windows / Android 签名界面 | [双端 stable 清单](https://117.50.48.22/updates/stable/manifest.json) | 托管运行时事件与状态恢复。Android 原生包仍为 0.2.83（构建 85）。[实施与发布记录](../docs/orca-upgrade-rollout.md) |

## 0.2.84 · 2026-11-25

| 平台 | 下载 | 版本与校验 |
| --- | --- | --- |
| Windows / Android 签名界面 | [0.2.84](ui/0.2.84/) | Hermes 高亮 stable 双端热更新；原生安装包继续使用 0.2.83。[修改说明](../docs/release-0.2.84.md) |

## 0.2.83 · 2026-11-25

| 平台 | 下载 | 版本与校验 |
| --- | --- | --- |
| Android 标准版 | [SuperMonoAgent.apk](android/0.2.83/SuperMonoAgent.apk) | 原包名、原签名、code85，可覆盖升级。[SHA-256](android/0.2.83/SuperMonoAgent.apk.sha256) |
| Windows 通用安装版 | [SuperMonoAgent-0.2.83-Setup.zip](windows/SuperMonoAgent-0.2.83-Setup.zip) | 完整后台、桌面外壳及自动更新清单。[SHA-256](windows/SuperMonoAgent-0.2.83-Setup.zip.sha256) |
| Windows 通用便携版 | [SuperMonoAgent-0.2.83-Portable.zip](windows/SuperMonoAgent-0.2.83-Portable.zip) | 无需安装的完整运行目录。[SHA-256](windows/SuperMonoAgent-0.2.83-Portable.zip.sha256) |
| Windows / Android 签名界面 | [0.2.83](ui/0.2.83/) | stable 双端完整快照，已发布。[修改说明](../docs/release-0.2.83.md) |

[构建索引](versions.json)

## 早期发布记录

完整历史版本索引：[VERSIONS.md](VERSIONS.md)。历史安装包和签名文件保持原始字节；归档签名包不代表仍在线上频道。

- [Windows 0.2.67 一键安装 ZIP](windows/SuperMonoAgent-0.2.67-Setup.zip)（通用无凭据版）。
- [Android 0.2.82 APK](android/0.2.82/SuperMonoAgent.apk)，支持保留应用数据的覆盖安装。
- [0.2.70 历史界面包](ui/0.2.70/)（桌面、Android、签名清单）。

所有公开 APK 均不再预填私人连接 Token，旧私人原包只在开发者本地私密备份。旧包重新生成了签名和校验文件，除去连接默认凭据外，其余程序负载逐字节保持。

`trial-state`、`.omx` 等本机运行数据、私钥和凭据不属于公开发布产物，不纳入 Git。`dist` 文件禁止 Git 换行转换，以保持签名资源和构建校验值。
