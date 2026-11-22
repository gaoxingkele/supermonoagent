# 发布产物

## 0.2.82 · 2026-11-23

| 平台 | 下载 | 版本与校验 |
| --- | --- | --- |
| Android 标准版 | [SuperMonoAgent.apk](android/0.2.82/SuperMonoAgent.apk) | 原包名、原签名、code84，可覆盖升级 0.2.78。[SHA-256](android/0.2.82/SuperMonoAgent.apk.sha256) |
| Windows 通用便携版 | [SuperMonoAgent-0.2.82-Portable.zip](windows/SuperMonoAgent-0.2.82-Portable.zip) | 包含新版进程退出确认及房间计数。[SHA-256](windows/SuperMonoAgent-0.2.82-Portable.zip.sha256) |

安卓从「☰ → 工作空间 → 管理当前终端」改名或结束会话。[0.2.82 修改说明](../docs/release-0.2.82.md) · [构建索引](versions.json) · [0.2.81 隔离试点归档](pilots/0.2.81/)

## 早期发布记录

2026-11-18 清理：本地只保留各平台**最新三个版本**，早于 0.2.78 的构建已删除（仍保留在 Git 历史中，可按提交恢复）。

## Windows（完整目录 + 公共安装包）

| 版本 | 完整目录 | 公共安装 ZIP | SHA-256 |
| --- | --- | --- | --- |
| **0.2.80** | [windows/0.2.80/](windows/0.2.80/) | [SuperMonoAgent-0.2.80-Setup.zip](windows/SuperMonoAgent-0.2.80-Setup.zip) | `cb4002c78e271d17bb1264f721194ee3fb116653460f99da27f0aa546ccfc2cf` |
| 0.2.79 | [windows/0.2.79/](windows/0.2.79/) | [SuperMonoAgent-0.2.79-Setup.zip](windows/SuperMonoAgent-0.2.79-Setup.zip) | `0499800ef8026366be359478deb1c4f27d50ba82c2ce7c066343558b35141657` |
| 0.2.78 | [windows/0.2.78/](windows/0.2.78/) | [SuperMonoAgent-0.2.78-Setup.zip](windows/SuperMonoAgent-0.2.78-Setup.zip) | `5359a63d1629af5595c45f8a5a305e5e1391460bc224e52997289802a979b121` |

三个 ZIP 均为**通用无凭据版**（164 个条目、158 个负载文件，无 `defaults.private.json`），已用 `c728238ab3ebabab` 私钥签名并通过 `openssl dgst -verify` 校验。完整目录内运行 `02-Orca-Workspace.vbs` 即可。

### 0.2.80 构建说明

- 构建时间 `2026-11-17T16:15:47Z`，源码 `05be7b4`（`codex/conversation-journal`）。
- 首次包含提交 `847dc2f` 的改动（管理式记录模式、Claude 原生翻页、URL 打开菜单）——0.2.79 产物早于该提交。
- 构建清单 97 个源文件哈希与当前工作树逐字节一致（0 差异）。
- 冻结产物包含 `managed_conversation`／`managed_codex`／`managed_claude`／`managed_process`／`conversation_store` 等模块；`journal-companion`、`journal-service`、`journal-hook` 三个冻结入口 `--help` 均退出 0。

## 含默认凭据的私人安装包

0.2.80 的**私人安装包**，内含默认中继 `wss://117.50.48.22/ws` 与本机 Token，安装即用：

`.omx/worktrees/conversation-journal/.omx/private-installers/0.2.80/SuperMonoAgent-0.2.80-Setup.zip`

SHA-256 `15d5e610f6c356841d7dad6be32f4a10d27d5d5581c765ecd57f6de68a68f0f9`，165 个条目，签名校验通过。它只在本地 `.omx`（已 gitignore）内保存。**含 Token，禁止提交 Git、上传公开更新频道或转发。**

## Android 与签名界面

| 平台 | 保留版本 | 位置 |
| --- | --- | --- |
| Android | 0.2.76 / 0.2.77 / **0.2.78** | `.omx/worktrees/conversation-journal/.omx/releases/android/` |
| 签名界面 | 0.2.76 / 0.2.77 / **0.2.78** | `.omx/worktrees/conversation-journal/.omx/ui-releases/stable/releases/` |

（0.2.79／0.2.80 只构建了 Windows 产物，没有对应的 Android APK 与签名界面快照。）

机器可读路径与校验值见 [versions.json](versions.json)。

## 边界

- 保留的构建仅表示产物存在、可执行文件与其 `build-manifest.json` 校验一致；**不表示**已部署或已通过真机验收。
- 在线更新频道仍指向 0.2.67；上述候选包均未提升到生产频道。
- 公开 APK 不含私人 Token；私有原包只在开发者本地私密备份。

`trial-state`、`.omx` 等本机运行数据、私钥和凭据不属于公开发布产物，不纳入 Git。`dist` 文件禁止 Git 换行转换，以保持签名资源和构建校验值。
