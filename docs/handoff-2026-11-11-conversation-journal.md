# SuperMonoAgent 会话记录与独立记录模式交接

初始交接日期：2026-11-11；网络证据补充：2026-11-12。本文供用户及下一位开发者接手，SSH 路径以第 7 节的新证据为准，其余验证仍是原交接时的快照。**代码与候选安装包已有进展，但尚未部署到生产服务器或用户设备，不能报告为全部完成。**

## 1. 先确认工作目录与 Git 状态

| 项目 | 当前状态 |
| --- | --- |
| 主目录 | `D:/aicoding/supermonoagent` |
| 主目录分支 | `codex/tablet-adaptation-wip`；此前平板适配工作已由用户停止，不要自动恢复 |
| 本次功能工作树 | `D:/aicoding/supermonoagent/.omx/worktrees/conversation-journal` |
| 功能分支 | `codex/conversation-journal` |
| 功能分支已提交检查点 | `18ea567` — `Keep backend conversations available across desktop and phone` |
| 检查点之后 | 独立记录模式、对应界面与测试等仍有已修改及未跟踪文件，**尚未提交** |
| GitHub / 生产发布 | 本次新模式未推送、未发布；不要把本地检查点当成已上线版本 |
| Python | `D:/aicoding/supermonoagent/.venv/Scripts/python.exe` |

接手时先在**功能工作树**执行 `git status --short` 和 `git log -1 --oneline`。不要直接在主目录继续实现，也不要遗漏未跟踪的 `managed_*.py` 和对应测试。主目录原有 `omx_wiki/index.md`、`omx_wiki/relay-health-2026-11-10.md` 诊断改动与本功能分支分开保存。

直接运行 Python 脚本时，须将功能树的 `src` 放在 `sys.path` 首位，或显式设置相应 `PYTHONPATH`。共用虚拟环境的 editable 安装可能仍指向主目录；`pytest` 已通过项目配置使用当前工作树 `src`。

source_path：两处工作目录的 Git 状态；功能树 `.omx/artifacts/candidate-0.2.74-status.md`。

## 2. 用户确认过的需求与最新约束

1. 桌面、手机均增加代码显示／折叠、历史记录和独立对话记录、时间戳、复制与引用。
2. **按同一个后台 CLI 会话合并保存**，桌面与手机共用；关闭查看页不截断，真正结束后台会话后归档。
3. 保留原始历史，另存用户输入与有证据的最终回复。本地和服务器都要保存；服务器同步尚待实际部署。
4. 引用只追加到原窗口草稿，保留原文本，不自动发送、按 Enter 或执行。
5. 用户选择：**新增独立对话记录模式，保留原 CLI**。不得把现有会话静默切换到新模式。
6. 不实现思维导图；历史／对话只是浏览排版方式。
7. 用户明确：**本机没有 Claude CLI 登录，平时使用本机 WorkBuddy / Kimi 等工具**。不要继续要求用户登录官方 Claude 账号；不能把这些工具的账号等同于 Claude CLI 凭据，也不能宣称已经完成它们的专用适配。
8. 用户后来澄清：**没有独立已登录 SSH 会话，但其他程序的 SSH/SCP 和网页访问都可以**。不要再以“已有登录会话”为前提操作。
9. 用户提供过服务器 root 登录凭据及上传示例。**密码、私钥、Token 不写入本文、Wiki、Git 或公开产物。** 示例中的 `rm -rf /home/others/web` 等命令未执行；部署前须先确认现有目录内容与服务。

以上取自用户本轮原话及选择，优先于早期探测假设。

## 3. 已实现的功能

### 通用记录、浏览与同步程序

- 每后台会话一个私有 SQLite WAL 原始账本，FULL 同步；关闭查看页或停止记录器不伪造 CLI 已退出。
- 原始内容与语义事件分开保存，另有只包含已接受输入／已确认最终回复的对话投影及可重建 JSONL、Markdown 导出。
- 有界异步写入、缺口与故障状态、收尾排空、严格限制的延迟修复、恢复与幂等。
- 桌面和 Android 共用安全浏览器：分页、折叠代码、完整正文按需读取、时间戳、选段复制、草稿引用、历史／对话排版。
- 独立记录服务 `/journals/ws`、固定单一镜像目的地、完整内容及哈希持久化后才确认回执、断线重试和所有者／房间隔离。
- 合入此前经过测试的 Claude Read／Churned 高亮和桌面目录重连 revision 重置修复。

主要代码位于功能树 `src/supermonoagent/conversation_{contract,store,capture,sources,hooks,companion,service,sync,manager}.py`、`static/terminal-client.js`、桌面／Android 原有界面资产。部署模板在 `deploy/journal/`。这些是程序实现，**不是生产部署证据**。

### 新增独立对话记录模式

- 从已授权的原 CLI 窗口显式新建独立窗口，使用该窗口记录的**启动目录**；不是猜测实时 shell cwd，也不恢复其原提供商线程。
- 当前驱动为 Codex app-server 与 Claude stream-json。沿用正常 CLI 环境、工具、项目设置、API 接入与权限政策，不读取／复制认证文件，不添加绕过权限参数。
- 输入、nonce、派发意图在同一规范账本事务提交后才返回保存回执；断线保留草稿，未知派发不自动重放。
- 严格校验根会话、用户消息标识、最终阶段与成功终结事件；中间输出、工具、推理、零轮 Hook 拦截不伪装成最终回复。
- 一次只运行一个提交；取消具有持久标记与物理发送前检查；权限只允许一次性允许／拒绝，过期、撤回、取消、未知交互不能获得肯定授权。
- 新模式的终端画面只读；OP4/17、旧 OP9 关闭与分组迁移等旁路已加保护。原 CLI 行为保留。
- 已处理回执丢失、原锚点关闭后的创建重试、闭会话回执读取、后台退出、空窗口清理、Task 回调、权限数据类型、未确认 Claude 根会话授权等问题。
- 失败结果单独显示“检查所选工具的接入配置与连接”，不会伪造助手结论，也不强制官方登录。

主要未提交代码：

| 文件／区域 | 责任 |
| --- | --- |
| `managed_process.py` | 有界 JSONL、严格解析、私有子进程清理、只读 ManagedPty |
| `managed_codex.py` | Codex 根线程／轮次／输入关联、最终阶段、权限与取消 |
| `managed_claude.py` | Claude 初始化、根身份、成功终结、权限与取消 |
| `managed_conversation.py` | 规范账本协调、nonce、授权、派发、收尾、状态 |
| `conversation_store.py` | 规范账本内的原子创建／提交／取消／终结／权限记录 |
| `daemon.py`、`hub.py`、`conversation_manager.py` | OP24、能力协商、权限检查、只读与生命周期集成 |
| `static/terminal-client.js` | 共用 managed controller、未知回执恢复、状态与引用辅助 |
| 桌面与 Android 原有 JS/CSS/HTML | 新建、编辑、权限卡片、中断、关闭、历史浏览 |

source_path：功能树 `.omx/plans/managed-recording-{prd,architecture,test-spec}.md`、`managed-recording-contract-v1.json`、`managed-store-api.json`；对应实现及测试。

## 4. 测试与证据，注意区分范围

| 范围 | 已收集结果／限制 |
| --- | --- |
| 0.2.74 检查点全 Python 执行 | 1859 通过、154 跳过、72 subtests 通过；发现 3 个测试问题，修复后相关 3 文件复测 31 通过。不能说原始全量运行零失败 |
| managed 后端集成 | 146 通过；整理后重复验证 146 通过 |
| 最新整套 Node | 158 通过；后续仅失败文案的定向 Node／浏览器检查通过 |
| 驱动／账本独立复核 | APPROVE，所列专项 118 通过；后续 Task 关闭修复也有专门回归 |
| 协调与集成独立复核 | APPROVE，包含真实 Hub＋假提供商的实际关闭流程，保留原 CLI 且无事件循环异常 |
| 原始捕获性能 | 实际 ConPTY＋Hub，10 MiB、0／1 查看者各三组；原字节与源 ID 完整、零丢失，吞吐及 p95 达标。采用固定最多 3 ms 聚合，未提高队列上限 |
| 高速无节制管道压力 | 旧证据仍为失败且有明确缺口；不能拿自然 ConPTY 的通过结果冒充无限吞吐承诺 |
| 桌面／手机／平板布局 | 有合成浏览器及多尺寸截图、结构化 verdict；不是用户设备真机验收 |
| 原生 CUA | 当前桌面锁屏，未完成真实设备／桌面最终验收；之前浏览器访问宿主也曾不可用 |
| lint / typecheck | Ruff、mypy、Pyright 不在当前环境；未新增依赖，只有语法、导入、编译、diff 和运行回归证据 |

证据均在**功能树** `.omx/artifacts/`：

- `final-full-pytest.txt`、`final-failed-families-rerun.txt`
- `managed-backend-integrated.txt`、`managed-all-node.txt`
- `managed-code-driver-store-review.md`、`managed-code-integration-review.md`
- `conversation-journal-review.md`、`conversation-journal-cleanup.md`、`managed-recording-cleanup.md`
- `conversation-journal-performance/hub-real-pty-coalesced-3ms.json` 及同目录说明；旧失败证据保留。
- `desktop-journal/retests/`、`desktop-managed/`、`android-managed/`；部分日志为 PowerShell UTF-16LE，读取时先识别 BOM。

不要将不同范围的通过数量相加成“全部唯一测试数”。0.2.75 尚未完成一次同进程全量 Python 绿灯与最终完整发行验收。

## 5. 真实 CLI 验证结论

### Codex：一条真实无工具请求通过

- 实际使用安装的 Codex 0.167.0，新建私有测试会话。
- 原输入精确保存；重复同一 nonce 返回同一回执，没有重复执行。
- 得到并保存一条具备 `provider_final_phase` 证据的最终回复。
- 没有修改全局设置、复制认证或调用编程工具。

### Claude：启动与失败记录通过，成功最终回复未验证

- 实际 Claude 2.1.310 初始化成功；先有应用提交，后由真实 `system/init` 确认根身份。
- 原输入精确保存，实际请求返回认证失败，没有伪造最终回复。
- 只读 `claude auth status --json` 返回退出码 1、`loggedIn=false`、`authMethod=none`。这份输出是有效状态信息，不应当作未知命令失败反复重试。
- 用户已说明主要使用 WorkBuddy / Kimi，**本机官方 Claude 登录不是要求**。应核对实际工具的公开 CLI/API 接入，不能抽取它们的密钥或把账号直接搬给另一端点。
- 目前实现了 Codex／Claude 驱动，**不等于已经接入 WorkBuddy 或 Kimi 桌面程序**。Kimi CLI 2.5.2 仅探测过版本，未实现其新模式驱动，也未发起模型请求。

source_path：`managed-real-startup-only.json`、`managed-real-acceptance-latest.json`、`managed-claude-real-failure-diagnostic.json`、`managed-claude-auth-status-redacted.json`。真实测试脚本为 `.omx/artifacts/managed_real_acceptance.py`；不要无理由重复付费模型请求。

## 6. 候选包与当前核对状态

全部路径相对于**功能工作树**，产物在忽略的 `.omx/`，不会随普通 Git 提交自动上传。

| 产物 | 路径／状态 |
| --- | --- |
| Android 0.2.75，versionCode 77 | `.omx/releases/android/0.2.75/SuperMonoAgent.apk` |
| APK SHA-256 | `4f93b8e85a857ed6d8d051c64c4d80a86c607b47519e406d996a4ab07f85e07e` |
| APK 校验 | 构建签名校验通过；本次交接再核对 CRC、ZIP 本地头、全部 24 个打包资产哈希和空连接默认值，均正常 |
| Windows 0.2.75 | `.omx/releases/windows/0.2.75/SuperMonoAgent/SuperMonoAgent.exe`，配套文件在同级发行目录 |
| EXE SHA-256 | `eefa81e6f77a5bcc5b3839dee22623de3ff2971eff5295e5265ad6a978a1e1d6` |
| Windows 当前源码快照 | 构建清单哈希与现有源码一致；不等于已完成 0.2.75 冻结 EXE 端到端 smoke |
| Windows Authenticode | 未配置签名，不能称 EXE 已有 Authenticode 签名 |
| 已构建签名 UI | 功能树 `.omx/ui-releases/stable/releases/0.2.74/`，当时 API2 资源为桌面 18／Android 19 个；未发布 |
| 0.2.75 签名 UI | **尚未生成／验收** |
| 安装与服务器发布 | **均未执行** |

source_path：`handoff-artifact-snapshot-0.2.75.json`、`build-{android,windows}-0.2.75*.txt`、对应发行目录的哈希／构建／资产清单。0.2.74 有过完整冻结 smoke，但不可把该证据用于 0.2.75。

## 7. SSH / 服务器状态与未解决问题

### 2026-11-12 用户实测补充：本机网络接口路径不同

发起连接的是这台 Windows 电脑；`WLAN` 是它的本机 Wi-Fi 网卡，`Meta` 是它的虚拟网卡。云服务器仍在远端，两次连接的目标均为 **`117.50.48.22:22`**，没有更换服务器或端口。

| 本机发起路径 | 用户实测结果 |
| --- | --- |
| 默认路径，经 Meta 虚拟网卡 | TCP 建立后断开，未完成 SSH 握手 |
| 指定本机 WLAN 网卡 | 收到云服务器的 SSH 协议标识，密钥交换成功 |

确认的差异是**本机从哪个网络接口发起连接**。尚不能确定关闭发生在 Meta 关联的转发环节，还是云端对两条路径的出口 IP 有不同处理；也尚未确认两条路径的实际出口 IP 是否不同。握手成功不等于 root 认证、文件上传或部署完成。

接手后先核对本机 WLAN 当前源地址及路由，沿用用户已经验证的 WLAN 绑定方式，再验证认证与只读管理命令，并保留主机密钥校验。若采用 SSH 的源地址绑定参数，地址应来自**本机 WLAN**；远端目标仍是上述云服务器。当前没有记录用户实测使用的完整命令，不要把未经验证的接口名参数当成已测命令，也不要为此盲目关闭 Meta、修改全局路由或更改服务器配置。

source：用户 2026-11-12 对两条路径的实测说明；本轮只更新交接，未独立重跑该对照。

### 后续独立复核：WLAN 已验证密钥认证与只读管理

在后续 EWAN 输入卡顿排查中，已读取本机 WLAN 当前 IPv4，并使用 Windows OpenSSH 的本机源地址绑定连接同一目标，保留 `StrictHostKeyChecking=yes`，以既有发布密钥成功执行只读 `uname -s`，返回 Linux。因此 **WLAN 路径的 SSH 认证与只读管理已实际验证**；没有使用或写出用户提供的密码，没有部署或重启服务，也没有修改全局网络配置。

另以不带应用认证的 WebSocket 控制 PING 做三次对照，不发送终端输入、订阅或尺寸帧：默认路径中位数约180.5ms，WLAN约48.0ms。这是客户端到中继 WebSocket 栈的往返，不能等同于模型/API时间；EWAN界面约460–500ms则是到远端daemon的应用PING，两种指标边界不同。

只读 systemd 信息显示 `sma-relay.service` active/running，主进程864始于2026-11-10 10:25:33 CST，NRestarts=0。启动时间不是完整升级审计，不能单据此排除磁盘文件或配置变动；当前没有证据将输入问题归因于一次新的中继重启。

source_path：功能树 `.omx/artifacts/unified-network-interfaces.json`（私有本机地址）、`unified-relay-control-ping.json`、`unified-relay-runtime-readonly.json`、`unified-relay-process-readonly.json`。接手应读取当时实际源地址记录并重新核对，不硬编码私网地址。Meta 与 WLAN 断开差异的具体根因仍未确认。

### 2026-11-11 历史探测记录

- 用户确认目标 `117.50.48.22`、用户 `root`、端口 **22 没改过**，并反馈其他程序目前 SSH/SCP 与网页两种方式都能访问。
- 当时 Git OpenSSH 10.5、Windows OpenSSH 9.5 的默认连接都能建立 TCP，随后在收到远端 SSH banner／进入密钥交换、主机密钥或密码认证前被关闭。这些失败不能再概括成“所有本机路径都无法 SSH”。
- 独立原始 TCP 检查亦在约 0.096 秒收到 EOF。**还没有机会验证用户提供的密码，不能说密码错误。**
- HTTPS `/health` 为 200、role=relay。不能说整个服务器宕机；HTTPS 正常也不证明 SSH 管理连接正常。
- Kimi 的结构化历史 Shell 记录证明：同一 22 端口曾经普通 `ssh/scp` 成功，最新明确服务状态证据为 **2026-11-06**。未发现显式代理、跳板、其他端口或 WSL。历史裸命令无法确定当时 PATH 实际解析的客户端。
- WorkBuddy 带有 OpenSSH 10.3p1 客户端，仅探测过版本，尚未用它复现当前连接。当前环境没有 HTTP/HTTPS/ALL proxy，Windows ProxyEnable=false。尚未确定其他工具当前成功路径与本执行环境的差别。
- 接入限流、IP 规则、转发或服务配置都只是待核对假设；结合新证据先区分本机接口路径，不能直接写成服务器故障或 Meta 的确定根因。
- 用户当时澄清没有独立已登录 SSH 会话；11 月 11 日观察时桌面锁屏，当前未重新检查。不要重复要求“切到已登录会话”，也不要尝试解锁或向其他 AI 程序发送包含凭据的提示。
- 未删除、上传、解压 `/home/others/web`，未改服务器端口、防火墙、sshd、nginx 或现有服务。

source_path：`ssh-diagnostic-20261111.md`、`ssh-tool-route-20261111.md`、`ssh-proxy-metadata-redacted.json`；主目录原有 `omx_wiki/relay-health-2026-11-10.md`。不要复制原始其他工具聊天／SSH 输出或其中凭据到新文档。

## 8. 接手顺序与完成条件

1. **保全代码**：先读 Git 状态及未跟踪文件，在功能 worktree 工作。按范围提交已验证的新模式；不要把主目录平板 WIP／无关诊断一起合并。提交遵守 AGENTS 的 Lore trailers 与 OmX co-author 规则。
2. **核对实际模型工具接入**：用户使用本机 WorkBuddy/Kimi；先识别其公开支持的 CLI/API 和现有接入方式。现有原 CLI 保留，不能把缺少官方 Claude 登录作为强制阻塞，更不能未经明确选择搬运其他工具密钥。
3. **沿已验证的 WLAN 路径继续 SSH**：目标固定为 `117.50.48.22:22`；WLAN绑定与既有密钥的只读登录已通过，后续先重新核对本机源地址。部署前仍须只读检查目录、服务、监听和nginx。默认Meta路径与WLAN的根因差异待日志及出口信息核对，不要反复猜端口／密码或先改服务器配置，再落实 `deploy/journal/README.md` 的增量部署。
4. **补发行验收**：重跑需要的相关回归，生成并核验 0.2.75 签名 UI，完成冻结 EXE 新模式 smoke、实际目标主机模型接入和手机／桌面／平板横竖屏真机验收。当前截图是合成测试。
5. **核对升级方式**：现有运行中的 0.2.67 后台没有新 OP24／managed 能力。UI 更新不等于后台升级。须先设计并验证不杀现有 PTY 的来源侧安装／兼容路径；不要直接重启当前 20 个后台 CLI。
6. **实际服务器与跨设备验收**：规范账本、TLS/Origin、所有者／房间与源主机登记正确；断开源主机后仍可从服务器读到完整已确认内容；明确本地保存与服务器确认序号。
7. **最后才报告完成／发布**：明确已提交、已推送、已安装、已部署分别是什么状态。没有证据的步骤继续列为待办。

若用户只要求交接，先交付文档，不自动恢复上述开发、模型调用、桌面操作或部署。

### 常用验证命令

```powershell
Set-Location 'D:/aicoding/supermonoagent/.omx/worktrees/conversation-journal'
git status --short
git log -1 --oneline

& 'D:/aicoding/supermonoagent/.venv/Scripts/python.exe' -m pytest tests/test_managed_store.py tests/test_managed_process.py tests/test_managed_claude.py tests/test_managed_codex.py tests/test_managed_conversation.py tests/test_managed_rpc.py tests/test_managed_hub.py -q --tb=short
node --test tests/*.test.cjs
git diff --check
```

构建日志中的 javac 废弃 API 提示不等于编译失败。Windows PowerShell 的 `> ... 2>&1` 曾把 native stderr 包装成终止错误；此前成功构建采用 `Start-Process powershell.exe -WindowStyle Hidden -Wait -PassThru`，分别重定向 stdout/stderr。脚本会自动递增 Android 版本，重建前先检查现有版本，不要覆盖既有不可变发行目录。中文文档用 UTF-8 安全写入；不要通过默认编码的 PowerShell→Python 管道写中文，曾导致字面 `?` 且已修复。

## 9. 导航与运行清理状态

- 本文为交接的规范入口；主目录 `HANDOFF.md` 和 Wiki 只负责路由。
- 功能树操作说明：`docs/conversation-journal.md`；服务模板：`deploy/journal/README.md`。
- 初始批准方案在主目录 `.omx/plans/`；新模式批准方案和冻结接口在功能树 `.omx/plans/`。
- 两轮独立代码审查与两轮 STANDARD 整理记录均保存在功能树 `.omx/artifacts/`。
- 原合成预览端口 64069、8767 已关闭；真实探测只创建并关闭自己的私有会话，没有重启用户现有后台。
- 子代理当前均已完成。前轮有意中断过工具执行，接手仍应核实本地进程、监听与构建状态，不能仅凭会话 ID 推断进程还在运行。
- 持久状态为功能树 `.omx/state/ralph-state.json`。交接不代表功能目标完成；没有创建 Codex goal。

## 10. 2026-11-12 统一终端候选包最新状态

2026-11-16 桌面修正已在新查看窗口启用0.2.79：项目设置上移顶部，原底栏目录状态移至标签栏并明确标为启动目录；补缺失／不可用状态显示。实际旧.77仍在用，延后加载目录控件可挤占输入左侧，此次已复现并补回归。66项浏览器及192项Node通过；新前端6772连接原6769后台，原GUI/CLI/文件助手保持运行，旧GUI留作草稿保全。原生AX／服务资源验证通过，原生截图工具返壁纸，勿误报原生像素检查。手机0.2.78未动，历史/高亮问题未在本轮修复。最新功能树证据`.omx/artifacts/desktop-live-footer/verification.json`。

2026-11-15 真机后续已完成：Honor FLC-AN00覆盖安装0.2.78/code80并启用APK自带UI；ADB重接后核验connected/ready=true、默认原始输入、单行底栏、无文档横向溢出，18份非HTML实时资源哈希与安装包一致。保留用户当时窗口，没有再次恢复旧导航。仅手机完成安装激活，服务器正式更新通道未变，桌面状态不据此推断。历史翻屏/历史高亮缺口已分析但尚未修改。最新证据位于功能树 `.omx/artifacts/android-installed-0.2.78/verification.json`，私有状态备份不得打印或提交。

2026-11-13 最新更正：候选现为0.2.78/code80，按用户要求改为原始CLI默认原始输入、左对齐且加长的底栏输入框、顶部工具栏项目／交换目录设置。源码与192项Node、桌面50项、目录11项及安卓9项相关浏览器用例验证通过；APK／Windows／签名UI已构建核验并上传备用目录。生产通道未变，未重载安装。功能树 `.omx/artifacts/raw-wide-candidate-status.md` 为本轮最新证据，早先默认本地输入的设计已被明确覆盖。

2026-11-13 后续：按用户要求将底部输入与按钮压为单行，最新候选更新为0.2.77（code79），已构建验证并上传备用目录，生产通道仍0.2.67且未重载安装。桌面50项、安卓7项浏览器回归及191项Node检查通过。功能树 `HANDOFF.md` 和 `.omx/artifacts/compact-candidate-status.md` 为最新记录。先前6771健康检查地址当前不可达，只读进程列表出现0.2.76候选程序，不能继续推断当前界面仍为旧bundled版本。

功能树已完成 Cursor／Claude／Grok／Codex 共用输入、历史滚动和高亮的实现，最终独立源码复审通过。191 项 Node、27 项界面、146 项 Managed 后端回归通过；后端组合测试有一条 Hub 读取线程在测试事件循环关闭后的警告，独立用例以线程警告作为错误运行通过，组合清理时序仍有验证边界。

0.2.76 APK（code78）、Windows 程序和签名 UI 包已构建并核验。独立包审计验证签名、对齐、ZIP／资源及源码哈希；三个冻结 `journal-* --help` 均通过。构建时的 19 条 ZIP 路径分隔符警告只发生于中间包，最终 APK 已修正且载荷字节未变。

签名 UI 包已通过本机 WLAN 绑定和既有密钥上传到远端 `/var/lib/supermono-updates/stable/releases/0.2.76`，三个公共文件哈希一致。生产通道 manifest 字节未变，仍为 0.2.67；本机仍为 bundled／generation0。未执行安装、重载、后台 CLI／中继重启。ADB 当前没有设备，实体安卓和 EWAN“专利量化”的实际交互仍待验收。

旧 0.2.67 未发送草稿、文件与回执仅在内存，必须先保留并完成上传，才能安装重载。UI 上传不代表独立记录服务已部署、镜像同步已配置或 Claude 提供商成功验收。接手以功能树 `HANDOFF.md`、`docs/unified-terminal.md` 和 `.omx/artifacts/unified-candidate-0.2.76-status.md` 为最新入口；第 8 节中的 0.2.75 待构建事项已由此次候选包准备取代。
