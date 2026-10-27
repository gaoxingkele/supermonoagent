---
title: "Windows 0.2.52 发布与交接"
type: Reference
category: reference
status: active
owner: llm
created: 2026-10-27
updated: 2026-10-27
tags: [windows, release, handoff, folder-transfer, history]
source_paths: ["docs/handoff-2026-10-27-0.2.52.md", ".omx/artifacts/release-0.2.52/final-release.json", ".omx/artifacts/release-0.2.52/HTTPSverification-52.json", ".omx/artifacts/release-0.2.52/server-final-check.json"]
source_urls: ["https://github.com/gaoxingkele/supermonoagent/tree/v0.2.52"]
links: ["clipboard-and-mylib-assistant.md", "runtime-config-and-recovery.md", "terminal-readability.md", "android-live-updates.md", "client-fixes-and-crypto.md", "task-notifications.md"]
confidence: high
schemaVersion: 1
---

# Windows 0.2.52 发布与交接

入口：[完整交接文档](../docs/handoff-2026-10-27-0.2.52.md)。本页记录发布验收事实，不代表原长期目标全部完成或运行进程永久保持该状态。

## 唯一发布基线

源码 `__version__`、Python 项目元数据、Windows EXE、运行时和安装清单均为 **0.2.52**。标签 [`v0.2.52`](https://github.com/gaoxingkele/supermonoagent/tree/v0.2.52) 指向发布提交 `c9d43d8fcd3e191c0f31c2874c63a71cf4bd7dc1`；构建清单固定源提交 `0f48295419685bbebce800084cbb310973f079d9`。发布提交仅追加文档与完整产物，产品源码已比对。

公共 ZIP SHA-256：`8f3cf94b0dc16cd450bc5e32d0d6c77bf4bdb0a8a162708bb2c1f876d64f40b1`。公共安装包、含默认配置的受限安装包和服务器 V2 文件服务已同步发布并回读验证。私有下载 URL／Token 不写入 Wiki；本机入口仅见 `.omx/artifacts/windows-oneclick/download-link.private.json`。

source_path: `.omx/artifacts/release-0.2.52/{release-inputs.json,final-release.json,HTTPSverification-52.json,private-publication-52-independent-verification.json}`。

## 已交付与未证明部分

Windows 编辑器显式文件夹入口、受限来源快照、固定项目批次和中继 HTTP 接收已发布。保留目录层级、空目录和原文件名；丢失 ACK 后查原批次，取消不删除已保存文件。真实公网 Windows helper 验证了中文／emoji、同名子文件、零字节文件、SHA 和项目 tmp；旧 0.2.51 helper 普通文件兼容通过。

长历史后台 OP23／epoch／区间 resume 已打包；队列 32、原有惰性调度和四个实际 footer 读取槽保持。协议支持不等于 Android／Windows 消费端已全部接线，不是完整 VT 检查点，也不恢复 Grok 已丢失正文。

source_path: `.omx/artifacts/release-0.2.52/{server-public-folder52.json,server-public-legacy51.json}`；标签内 `docs/{folder-transfer-development.md,history-context-integration.md}`。

合并回归 **1139 passed / 5 skipped**，冻结模块 46、源指纹 82、静态资源 30 匹配；两次隔离安装保留配置、隐藏 WebView2 通过。真实 OS 剪贴板／目录对话框及六种 CLI 全面交互尚未验收。编辑器显式粘贴文件夹不能算终端自动 Ctrl+V 文件夹已接线。

source_path: `.omx/artifacts/release-0.2.52/{combined-tests.xml,frozen-verification-52.json,assets-and-secret-scan-52.json,full-installer-verification-52.json,native-smoke-52.json}`。

## 运维不变量

发布只重启 V2 文件服务一次，未重启用户终端。活动代码在 `/opt/supermono-files-v2/releases/0.2.52-0f4829541968`，标记为 `active-release.json`；V1、认证和 Nginx 保留。验收之后已有新文件夹任务，**不能直接恢复旧 DB 或重复执行升级脚本**；回退需重新核实兼容性并保留新数据。

主目录存在其他未提交工作，不能视为标签源码。不要覆盖它，也不要移动 `v0.2.52` 或改写已发布包。后续版本继续由源版本生成，并核对构建源提交、冻结模块与签名下载产物。

source_path: `.omx/artifacts/release-0.2.52/{server-upgrade-result.json,server-final-check.json}`；标签内 `scripts/build-windows.ps1`、`tests/test_release_version.py`。

## 恢复工作条件

原长期目标仍暂停；用户明确恢复后再处理实际运行版本激活、真实剪贴板／CLI、Android 目录树和历史、自动项目目录、mylib／metaRSI 及 goal 菜单位置。此前物理 Escape 停止了桌面控制，不能仅凭自动目标继续消息恢复操作。

相关知识：[[clipboard-and-mylib-assistant]]、[[runtime-config-and-recovery]]、[[terminal-readability]]。完整剩余范围和证据索引见交接文档与 TODO。

其他既有知识：[[android-live-updates]]、[[client-fixes-and-crypto]]、[[task-notifications]]；这些链接不表示本轮重新验收了历史页面的全部功能。

source_path: `docs/{handoff-2026-10-27-0.2.52.md,TODO.md}`；`.omx/artifacts/six-cli/clipboard-cua-20261027.json`。
