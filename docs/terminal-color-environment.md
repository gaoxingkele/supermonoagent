# 新 Windows PTY 的颜色环境

新后台创建 Windows PTY 时，默认使用 `interactive` 策略。它只复制并调整该子终端的环境，不修改 daemon／桌面程序／系统的环境，也不改变已运行的终端。

- `TERM` 缺失、空白或为 `dumb` 时设为 `xterm-256color`；其他明确值保留。
- `COLORTERM` 缺失或空白时设为 `truecolor`；其他明确值保留。
- 移除继承的 `NO_COLOR`。这是默认交互终端策略的明确选择，不是推断该变量一定为误设置。
- 不创建 `FORCE_COLOR` 或 `CLICOLOR_FORCE`；已有值完整保留，包括 `FORCE_COLOR=0`。其他环境变量不变。

高级兼容选项：为后台设置 `SUPERMONO_TERMINAL_COLOR=preserve`，新 PTY 完整保留原有颜色变量，包括缺失值。未知的非空策略也按 preserve 处理，不导致终端启动失败；空值与 `interactive` 均采用默认策略。

该选项需要目标电脑使用包含本改动的新版后台，并只影响之后创建的 PTY。这里不要求重启、关闭或修改当前用户会话；旧会话的颜色选择不会被追溯改变。

隔离验证使用自有 WinPty 与 Node 内置 TTY 检测：继承 `TERM=dumb`、空 `COLORTERM`、`NO_COLOR=1` 时，旧构造器虽有真实 TTY，仍检测为深度 1；新默认策略检测为深度 24，并实际输出颜色 SGR。preserve 仍为深度 1。该 Node 进程启动的 stdout pipe 子进程保持非 TTY，且没有新增强制颜色变量。测试没有调用真实编程智能体、登录或模型。

证据范围：这证明了继承环境导致单色的机制，并不证明用户实际 Grok／Kimi daemon 使用同样环境；更不恢复被裁剪的终端画面或历史。恢复布局与颜色检测是独立问题。

验证入口：`tests/test_terminal_color_env.py`、既有 `tests/test_pty.py`。官方机制说明：[Node TTY 颜色检测](https://nodejs.org/api/tty.html#writeStreamgetColorDepthenv)。
