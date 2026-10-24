# Codex 暂停目标菜单位置诊断

状态：原生布局已复现；用户要求的左下角输入栏上方布局仍未实现。

## 实测事实

在本机安装的 Codex 0.164.0 中，以独立 CODEX_HOME、凭据目录、合成会话和本地模型服务创建暂停目标。未使用真实登录凭据、用户会话或已有 PTY，没有模型 POST。

100 列 × 40 行下，坐标从 1 开始：

| 屏幕模式 | 原生标题位置 | 输入行 | 原生与同尺寸回放 |
| --- | --- | --- | --- |
| auto / always | 第 17 行、第 17 列 | 第 37 行 | 可见文本、缓冲区类型和标题位置一致 |
| never | 第 33 行、第 17 列 | 第 34 行 | 同上；选项落在输入区域下方，并非所需布局 |

因此，居中菜单可以直接来自 Codex 的原生绘制；不能仅凭截图把它认定为本项目的前端回放错位。本结论限于已测版本与尺寸，未证明所有 Codex 版本或用户当时会话均采用同一行为。

source_path: `.omx/artifacts/goal-popup/aggregate-evidence.json`, `tests/test_codex_goal_popup.py`

## 配置与范围

官方配置参考说明 `tui.alternate_screen` 控制备用屏幕模式。在本次检查的公开参考和配置 schema 中，未找到 goal/modal 的坐标选项；这不证明不存在未公开选项。当前没有改动用户的全局或项目配置。

source_url: https://learn.chatgpt.com/docs/config-file/config-reference?translationFallback=ja-JP

source_path: `.omx/artifacts/goal-popup/official-config-schema-20261025.json`

干净地改变该菜单位置需要 Codex 原生布局支持或进一步的独立显示适配设计。直接移动／遮盖终端文字会涉及背景恢复、复制选择和鼠标坐标，不应凭位置截图立即更改稳定的终端渲染。已向用户询问是否把该项扩大到 Codex 本体适配；该要求没有被标为完成。

## 与历史裁切问题的区别

另一个自有合成输出用例确实展示：裁切 256 KiB 历史后，初始光标位置被移除，菜单可从第 31 行漂到第 1 行，而绝对定位的输入行仍在第 38 行。这证明输入位置正常并不保证所有历史坐标正确，但不能用它替代真实 goal 菜单的原生绘制证据。

source_path: `src/supermonoagent/pump.py`, `.omx/artifacts/goal-popup/replay-evidence.json`

回归测试使用当前项目自带的 xterm 和真实 OutputPump，验证与原生绘制一致，不把“居中”写成永远正确的预期。未来 Codex 改变原生布局后，测试仍应检验镜像保真。
