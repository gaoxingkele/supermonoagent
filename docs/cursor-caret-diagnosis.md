# Cursor CLI 底部白块：诊断证据与边界

日期：2026-10-26。目标：Windows 客户端 `JINLONG3090 / IIA_Benchmark`。已完成最小后端源码修复，尚未部署到远端；没有向实际会话注入输入或设备查询应答。通过现有客户端打开该视图，未点击接管、刷新或结束。

## 已验证的机制

`src/supermonoagent/pump.py` 的 `_snapshot_locked()` 从 256 KiB 原始字节环生成快照。修复前 `src/supermonoagent/ansi_state.py` 的 `bounded_snapshot()` 恢复 SGR 样式并避开不完整编码／控制序列，但没有恢复 DECTCEM（`CSI ? 25 h/l`）的光标可见状态，也不是完整终端屏幕状态快照。`src/supermonoagent/static/workspace.js` 的 `drainPane()` 在历史恢复时先 `term.reset()`，再写入快照。

自有测试先隐藏原生光标，输出超过环大小的内容，在第 20 行绘制自有输入标签，最后把原生光标停在第 42 行左端。完整流与裁剪快照在真实 xterm 中具有相同的输入标签和 `(0,41)` 光标坐标，但完整流 `isCursorHidden=true`，快照为 `false`。这可产生“输入行在中部、底部有额外白块”的画面。

另两项真实 Edge 测试分别使用 DOM 和 WebGL：在 144×100 的受缩放网格内，以 ANSI CUP 将光标置于 `(31,11)`。隐藏 textarea 的屏幕坐标与目标网格误差小于 0.15 格，焦点正确，没有 PTY 输入。此结果排除了该隔离场景中的通用缩放／textarea 锚点错位；它不等于 Windows 原生 IME 候选窗口已经验收。

验证命令：`.venv/Scripts/python.exe -m pytest tests/test_cursor_geometry.py -q`，结果 **3 passed**。测试只使用自有虚拟 PTY、合成输出及临时浏览器。

## 实际目标的证据边界

早期只读记录 `.omx/artifacts/cursor-input/iia-readonly-evidence.json` 的后台网格为 158×43；其中 `(0,41)` 是本地解析受裁剪快照后的结果，而非后台独立采集的远端光标。最新只读检查由主代理记录为 145×42、快照 262134 字节、重建光标 `(0,41)`，5 秒无实时输出；实际 GUI 的输入标签高于底部白块。快照内未保留隐藏／显示光标命令，不能据此断言早期完整流也没有这些命令。

Inference: 丢失早期隐藏光标状态与现有症状高度吻合，但原始指令已经超出历史环，不能回溯证明实际 Cursor 曾发出该指令。后续定位应比较完整会话的状态与恢复快照，不能从窗口名、配色或自绘输入标签推断可信进程身份。

用户随后提供同机新开的“窗口 5”。其只读恢复快照仅 7,036 字节，包含 Cursor Agent 启动标签、31 次隐藏及 30 次显示指令，真实 xterm 解析后硬件光标为隐藏，坐标 `(0,34)`；这直接证明新窗口的实际输出会隐藏停在左侧空行的硬件光标，与旧窗口缺失显隐指令的差异一致。未发送按键或设备应答。该快照没有提取到可确认的版本号；检查时本机进入 Windows 锁屏，未绕过锁屏继续操作。证据 `.omx/artifacts/cursor-input/window5-readonly-evidence.json`。

## 已完成的最小修复

在后端有限状态快照中保留并恢复 DECTCEM，状态未知时维持兼容默认；按历史裁剪边界准确恢复状态，避免覆盖尾部较新的显示／隐藏命令。不记录或猜测光标位置，不修改实时字节流。增加分块控制序列、OSC/DCS 隔离、隐藏后重新显示、重置与双重裁剪回归；原失败用例现要求完整流和裁剪重放均保持隐藏状态。

组合验证：`tests/test_pump.py tests/test_cursor_geometry.py tests/test_cursor_input.py tests/test_text_paste_priority.py tests/test_text_paste_regression.py`，**21 passed / 23.60s**，证据 `.omx/artifacts/cursor-input/caret-state-green.xml`。这是后端修复，单独更新前端 UI 不能补回旧 daemon 已经丢弃的状态；已有活跃会话中被裁掉的状态无法凭空恢复，不能声称新实现自动修复其旧历史。当前未重启用户 daemon 或 Cursor，会话实测仍待验证。

独立审查后补齐 RIS 保持当前 bundled xterm 显隐状态、DECSTR 显式显示、空 CSI 参数及前 32 参数边界。最终验证另外覆盖协议与客户端集成，**31 passed / 31.96s**，结果登记于 `.omx/artifacts/cursor-input/caret-final.xml`。这仍是显隐状态恢复，不是光标位置重写或完整屏幕状态恢复。

不要根据屏幕文字移动光标，不要统一隐藏所有终端光标，也不要为此放开 DSR／DA／OSC 应答过滤。那些行为会改变用户输入语义或掩盖实际终端状态。

## 完整 Windows 0.2.47 发布

完整更新频道已发布 **0.2.47**（序号 `1793012544264657`）。通用安装包为 `dist/windows/SuperMonoAgent-0.2.47-Setup.zip`；专用包保留现有默认 Token、文件助手配置及限时下载入口，私有配置未提交 Git。共享 UI 仍为 0.2.42，未混入并行 Android 改动。

实际编译 EXE 中提取的快照模块验证隐藏／显示／软重置行为正确，构建源指纹匹配；隐藏 WebView2 启动及隔离目录两次安装、配置 ACL／Token 保留通过。公网签名清单和下载前 1,024 字节验证通过，未重复完整公网下载。证据目录 `.omx/artifacts/windows-files-0.2.47/`。

该修复需要运行在产生历史快照的电脑后台；客户端前端单独更新无效。目前未替换或重启 JINLONG3090 的后台，也未关闭用户 Cursor。其旧历史中已丢弃的状态无法回溯恢复，实际会话仍待更新后确认。
