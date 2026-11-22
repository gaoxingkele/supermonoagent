---
title: "Multi-Agent Hub 驾驶舱"
category: decision
status: active
updated: 2026-11-22
tags: [desktop, multi-agent, mylib, rsi]
source_paths: ["docs/multi-agent-hub.md", "DESIGN.md", ".omx/plans/multi-agent-hub.md"]
links: ["windows-release-0.2.80.md"]
---

# Multi-Agent Hub 驾驶舱

2026-11-22 后续：实际 Linux 中转机的私有 Unix-socket 服务已运行并验证重启；Windows 与中转机通过测试项目交接。Android API 35 模拟器上的最终 QA 包已验证确认、丢回执核对与后台失效。美国采集机已指定为 Ewan，真实账号及物理设备验收待完成。 [现场验收记录](../.omx/worktrees/agent-hub-pilot/docs/hub-field-validation.md)

三轮设计评审的裁决和渐进交付次序见 [mylib 联合评审方案](/D:/aicoding/mylib/tools/agent-hub/rollout-design.md)。覆盖双端只读首版、宿主能力分级、一次人工确认、额度与任务交接，以及 RSI 评测和加载回执。

开发工具通过 Hook 向业务 MCP 提交关键结果。每条事件入库后触发对应计费账号的额度查询，周余量和下次重置写回数据库。驾驶舱对话查询任务、额度和 mylib 固定版本，终端字节流按需补充现场证据。

已生成 0.2.81 本地隔离试点包，客户端本地分支 `codex/agent-hub-pilot`。两类客户端文档及真实回环 MCP/SQLite 完成合成跨项目联调，Windows 冻结 EXE 通过 WebView2 启动检查。安卓 APK 使用独立包名与测试签名。实际跨机、真实账号、安卓真机及生产推广分别验收。 [试点交付和本地验证记录](/D:/aicoding/mylib/tools/agent-hub/pilot-status.md) · [原有技能 MCP 历史部署记录](../docs/central-mylib-mcp.md)

凝练对话沿用原会话记录的精简视图作为证据来源。Hub 每个窗口维护任务摘要，总管接收摘要及按需展开的证据。 [原会话记录](../.omx/worktrees/conversation-journal/docs/conversation-journal.md#L7)

通过已有中央 mylib 接口检索技能。记录技能版本、工具和验证结果，经跨工具回放与人工采纳后进入 mylib 的版本发布流程。

驾驶舱设常驻 mylib 入口。RSI 候选在库内评测与发布，窗口上报实际加载版本。向具体 Coding 窗口投递建议或应用新版，均由用户逐次选择目标、核对内容并确认。 [mylib 端设计](/D:/aicoding/mylib/tools/agent-hub/README.md)

- [架构与冻结标签](../docs/multi-agent-hub.md)
- [桌面行为](../DESIGN.md)
- [实施里程碑和验收](../docs/multi-agent-hub-plan.md)

冻结点的包与验证边界见 [[windows-release-0.2.80]]。
