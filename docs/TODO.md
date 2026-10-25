# 修改 TODO

## 2026-10-26：中央 mylib MCP 第一期已上线

- [x] `https://117.50.48.22/mylib/mcp` 已提供标准 Streamable HTTP／Bearer 接口；实际公网 TLS 1.3、官方 SDK 初始化／8 工具／资源读取及拒绝无效权限均通过。未重启终端中继，未修改用户 CLI 全局配置。
- [x] 可信 `main` 及四个 pinned 子仓库已同步，首个活动版本 `e4599f4` 含 1,855 个技能入口、15,428 个文件。独立同步 timer 已启用；重复同步保持活动快照不变，并继续增量结构审阅。
- [x] 用户明确选择先同步＋结构检查、模型稍后配置。独立固定 metaRSI 引擎／账本、候选格式验证、资源预算和私有权限均已接通；首轮 3 条候选只证明格式及源保留，语义效果未验证，不自动应用或准入。
- [ ] Windows／Android 右侧栏与 CLI Hook 的中央 MCP 接线仍待实现；不得把服务可用称为现有客户端已经切换。模型分析按用户选择后续配置。
- 验证：四模块联合 148 passed；实际 Linux loader 修复后完整审阅组 49 passed；服务器全库结构审阅、重复同步、HTTPS SDK 与反向鉴权通过。见 [服务与边界](central-mylib-mcp.md)。

## 2026-10-25：当前文件交换源码归档至 GitHub

- [x] 目录领域层、独立 broker／companion、文件服务客户端和目录 UI 模块及测试已实现，按用户要求一并提交；严格重名策略保持原默认行为。见 [组件交付与剩余工作](file-exchange-components.md)。
- [ ] 正式桌面／原生桥／剪贴板及安装器集成、生产部署和实机完整传输尚未完成；这些源码不在已发布的 UI 0.2.42 中。

## 2026-10-25：安卓接管／跟随切换 UI 0.2.42

- [x] UI **0.2.42** 双端已发布并从公网回读验签：安卓“接管”后可点击“跟随”释放尺寸控制，跟随保持远端网格并可横拖／输入；无桌面时提示等待。Windows 匹配 ACK 后清除已完成请求缓存，修复相同尺寸第二轮交接停滞。刷新与交接已互斥，不改 daemon。见 [两模式说明及验证](ui-0.2.42.md)。
- [ ] 用户 Windows 与手机更新到 UI 0.2.42 后现场验收；隔离真实浏览器通过不能替代已安装手机验证。默认十六宫格及 0.2.41 高亮修复均保留。

## 2026-10-25：高亮与推荐文字热更新 0.2.41

- [x] UI **0.2.41** 已发布并从公网回读验签：Android Codex 增补论文场景的路径／代码／数字等窄规则，Windows/Android 保留 CLI 灰色推荐文字，屏幕方向键跟随终端光标模式；保留右上角总览与默认十六宫格。20 项 pytest、14 项 Node 和候选 ZIP 八种布局通过。见 [交付与未完成项](ui-0.2.41.md)。
- [ ] 手机下载、重载后的现场验收：由用户在手机执行热更新；尚未把服务器发布成功当作手机已经更新。
- [ ] 新文件交换的后端模块已有本地测试，图形界面／安装集成和生产部署尚未完成；Android 多附件、长历史缓存仍待交付。中央 MCP 第一期状态已由 2026-10-26 节更新，客户端接线及模型分析仍待完成。

## 2026-10-25：Android 总览回退紧急修复（旧长期目标暂停期间的独立任务）

- [x] UI **0.2.40** 已发布并从公网回读验签：恢复右上角总览入口，默认十六宫格，保留九宫格切换。源码、候选 ZIP、实际公网下载 ZIP 均通过八种布局及只读监测检查；60 项 pytest、4 项 Node 总览单测通过。
- [x] 已确认回退根因：0.2.39 用旧 0.2.34 Android ZIP 升高共享 revision，覆盖了设备已有的新功能；不再把“旧负载字节不变”称为 Android 不受影响。共享生产频道正式提升 reuse／preserve 负载已在发起 SSH 前阻止，默认完整双端发布保留。详见 [回退修复记录](android-overview-regression.md)。
- 当前未通过 USB 实测该手机更新，需在设备“检查界面更新”下载 0.2.40 并重载；不需要安装 APK。原长期目标仍暂停，不继续其手机安装、原生 Codex 或后台升级任务。

## 新增：mylib 助理与文件粘贴（2026-10-25，开发中）

- [x] Windows **0.2.39** 完整包已发布至原中继完整程序频道，序号 `1792890276205080`；公开 ZIP 不含 Token，专用包通过受限链接注入默认配置。TLS 回读、RSA 验签、归档 hash、一键安装及重复安装保留凭据均通过，中继未重启。源码和完整 Windows／UI dist 随本次版本提交。证据：`.omx/artifacts/windows-metarsi-0.2.39/{final-publication.json,https-download-verification.json,full-installer-verification.json,public-archive-audit.json}`；上一版 0.2.38 与 0.2.37 的提交及验收仍保留。

- [x] 标签导航发布验收：新建、左右/上下排列、最大化已移至顶部菜单行；标签行仅留页签及左右滚动按钮。顶部“全部窗口”按电脑列出当前已开标签，可切换并滚入可见区。18 标签、430px 窄屏、键盘 Escape、无终端输入的隔离测试通过，已包含在 0.2.37 中。

- [x] 按用户要求同步本地 mylib 的远程库：`cd100e3 → 78c7e3b`，fast-forward；保留本地 `Graphic/` 未跟踪目录。
- [x] 独立本地技能检索模块：维护清单、路径约束、有限上下文、来源及匹配理由；当前本机索引 50 项。关联测试 16 passed、6 subtests passed（含桌面启动回归）。
- [x] 右侧助理栏源码与原生桥接验收：可折叠/暂停/复制建议，窗口切换异步结果隔离及 HTML 文本显示通过；真实隐藏 WebView2 已显示技能建议，已随 0.2.37 发布。
- [x] metaRSI 账本只读接入：OP22 从目标终端启动目录读取固定 wiki 页面、显示目标技能库可用性；故障输出生成带线索与可证伪条件的待验证建议，不推断实时工作目录或自动批准变更。
- [x] 模型对话与智能分析实现交付：0.2.38 已接通 API／本机 Codex 两种文本引擎、独立窗口对话、显式上下文、取消及凭据内存保存。真实隐藏 WebView2、loopback API 和真实 CLI 假登录已验证，未读取用户真实登录文件或调用云模型。独立审查修复凭据历史重放、取消先到及配置保存竞态，35 项复核通过。用户需自行选择并配置实际模型服务；生产账号调用未验收。配置条件与边界见 [使用说明](assistant-models.md)。
- [x] metaRSI 建议质量实现交付（0.2.39）：已接通旧记录匹配及模型只读账本片段，目标后台固定页增加 nodes／ideas；独立发送开关默认关闭，保持 0.2.38 模型桥兼容。窗口、凭据、代次、通道及会话共同绑定；截断不保留不完整密钥片段。真实隐藏 WebView2／OP22／假模型已验证，门控重连先离线再同代次自动恢复，无需刷新，PTY 输入 0、项目内容和 mtime／ctime 不变。原生验收未使用生产模型账号，不把建议当验证结论；实验登记与准入仍见下项。依据见 [助理设计](mylib-assistant-design.md)、`.omx/artifacts/assistant-native-ledger/final-verdict.json`。
- [ ] metaRSI 第三期登记与验证：实验／假设登记和验证结果关联尚未接入助手；保持项目隔离及原生账本为事实源，受保护准入仍需明确决定，不自动执行规则批准。此项属于已有设计第三期，不能把已发布的文本建议称为完成执行闭环。
- [ ] Windows 文件/截图粘贴完整验收：OP21、原生桥接、分块/校验、进度/取消及目标绑定已接通；直连／中继、真实 WebView2 测试源、取消／切换／重连及打包 EXE 的真实自有 cmd 后台均通过。真实系统剪贴板打开仍返回访问拒绝，未覆盖用户剪贴板，未宣称该环节通过。接收目录可通过后台参数指定，暂无图形设置。详见 [文件粘贴](clipboard-files.md)。
- [x] 本轮终端稳定性回归：整套 816 passed、3 skipped、57 subtests；随后签名 UI 接入与原生版本比较的定向验证通过。慢磁盘、默认线程池仅 1 线程时，同一条直连／中继连接仍可处理输入和心跳。没有操作用户活跃 PTY。
- [x] 0.2.38 发布回归：冻结产品源码的整套结果为 1027 passed、3 skipped、57 subtests，另 1 项原生恢复测试因旧加载事件竞态失败。仅修复测试等待条件后，完整更新测试组 12 passed，真实 WebView2 挂死恢复独立通过两次，匹配新 NavigationId／文档标记及连接，约 20 秒恢复且 PTY 输入 0。Node 44 passed；不能把上述分组相加冒充一次整套绿灯。原始失败与修后证据均保留：`.omx/artifacts/assistant-full-suite-frozen.xml`、`windows-assistant-0.2.38/native-hang-frozen-suite-failure.json`、`ui-updates/native-hung-renderer-recovery.json`。
- [x] 0.2.39 相邻回归与热更新：163 passed、3 skipped、9 subtests，Node 44 passed。旧 publisher 测试改为临时自有密钥；翻屏测试等待新快照再验证，原断言保留，未改终端解析或输入核心。UI 0.2.39 已发布，序号 `1792889463746380`；Android ZIP／目录保持公网 0.2.34 原样，仅路径换至新版本。发布锁内比较 Android 描述符，防止覆盖并发手机更新。公网双平台验签／目录回读通过；Android 因全局版本仍可能提示相同内容更新，原生 APK 仍为 0.2.36。证据：`.omx/artifacts/windows-metarsi-0.2.39/{ui-publication.json,ui-https-verification.json}`。
- [x] 新模块纳入现有签名 UI 资源：复用 workspace.js，保持 17/16 资源格式及 API2 兼容；后续侧栏和文件界面可热更新。新装程序不再把低于／等于内置数字版本的 UI 提示为更新，继承观察序号不阻止更高版本。
- [x] 用户指定的 0.2.36 安装目录原包：已在隔离目录实测完整安装与默认凭据匹配。新版启动器补激活已有主窗及成功反馈，仍不强开第二个主窗；未擅自安装到真实用户目录或关闭运行后台。
- [ ] Codex 恢复 goal 菜单位置：真实 Codex 0.164 的 `auto/always` 原生首次绘制就居中；100×40 时标题在第17行/列、输入在第37行，同尺寸回放的可见文本和坐标一致。`never` 也未得到干净的输入栏上方布局。上方布局仍未实现，已询问是否扩大到 Codex 本体适配；未修改终端渲染或用户配置。证据和边界见 [菜单诊断](codex-goal-menu.md)、`tests/test_codex_goal_popup.py`。

方案见 [助理设计](mylib-assistant-design.md)。0.2.38 外壳可热更新至 0.2.39 账本界面；新装完整包为 0.2.39。文件接收和项目账本查询需目标电脑新版后台，旧三页后台可降级。未勾选项仍未完成。

### 2026-10-25：剩余上线与真机事项核实

- [ ] **目标后台升级**：默认 Token 的新鲜 WSS 目录可见 USA、61914c、JINLONG3090 三个在线房间；只读 HELLO 确认各有 3 个终端会话，三个后台均未声明 `file_transfer` 或 `assistant_context`。因此新客户端的文件接收／账本能力尚未在这些运行后台上线。为保留现有会话，没有重启、关闭或向终端输入命令。升级需对应电脑的可信本地安装与会话维护安排。
- [ ] **严格房间登记迁移**：空凭据目录返回 0 台，默认 Token 可见 3 台，生产仍报告 `host_enrollment:false`。中继服务 active，当前进程未显式带 `--enrollment-file`；这不等于已证明默认文件不存在。全体主机的 owner secret 交付与迁移就绪状态仍未确认，没有切换严格模式或重启中继。
- [ ] **Android 修复后实机回滚与 OS 输入法**：独立 QA 0.0.3 包名与 SHA-256 已核对，生产版数据未动。81224、22808、92077 均已终态失败；92077 先到荣耀指纹页，后返回 `INSTALL_FAILED_ABORTED: User rejected permissions`，这不证明用户主动点了取消。新鲜查询 QA 仍为 0.0.2，未执行 throw3／hang3。已询问用户现在是否方便完成新的系统物理验证；不重复索取已给出的保留数据安装授权，不把旧会话当作活进程或静默循环重装。

本次仅发送目录认证和 HELLO，没有发送输入、resize、打开／关闭终端命令，也没有操作生产手机数据。证据：`.omx/artifacts/remaining-todo-audit-20261025/{directory-capabilities.json,host-readiness.json,relay-service.json,phone-readiness.json}`。

## 历史：2026-10-24 状态摘要

- [x] 最新一键安装与完整程序自动更新交付为 **0.2.36**：专用 ZIP 自动配置默认中继/私有 Token，新机用目标电脑名注册；安装版启动后从同一中继 `/updates/windows/stable/manifest.json` 检查、自动下载校验并提醒安装。保留当前主窗口、配置和 PTY，拒绝同版更新后 24 小时抑制；原生主窗每 15 分钟检查，浏览器后备启动时单次检查。已修复时区、失败序列高水位、半包恢复、worker 交接及 Windows 深目录暂存路径。真实 HTTPS 完整包下载与隔离升级到 0.2.36 已通过，保留上一版本与凭据；未在真实用户目录安装或展示测试更新框。后续较早版本记录作历史证据保留。验证见 `.omx/artifacts/windows-oneclick/`。

- [x] 完整 Windows 包压缩与一键安装：0.2.34 完整 ZIP 已提供通用无凭据版与受限专用配置版；专用版从本地私有 token.txt 注入默认凭据，使用目标电脑名称注册，安装到用户目录并保留既有 Token/房间/活跃后台与单一主窗口。专用 HTTPS 下载使用 fragment/Bearer 凭据与限时哈希登记，不在公开目录/Git存放私有包。21 项安装回归、9 项打包/下载回归，以及公网实际包的隔离完整安装与本机名注册均通过。见 [安装说明](windows-oneclick-install.md) 和 `.omx/artifacts/windows-oneclick/`。NPM/winget、完整原生自动更新/回退及多台实机安装仍非本次已验收项。

- [x] 守护进程运行日志：后台写 UTF-8 轮转文件，**不自动弹出任何日志窗口**。根据用户最新要求，在唯一的 SuperMono 主窗口内集中展示所有已连接电脑的 daemon 状态，并选择查看其日志；所有者才可读取日志，普通账号/委托会话不能读取全局日志。保留高级 `logs` 命令；记录连接/重试、终端数量与 15 秒心跳，不记录终端内容或 Token。旧后台不强制重启，仍可查看连接状态，运行日志需新版后台正常启动。源码见 `daemon.py`、`daemon_logs.py`；验证记录见 `.omx/artifacts/daemon-live-logs/`。
- [x] Windows 安装方式评估与方案：见 [安装方案](windows-installation-plan.md)，已核实完整目录、内置运行时、外部依赖与签名状态，比较用户级安装器、winget、npm；推荐先实现用户级安装入口，再接 winget。安装器及包管理仓库发布为待实施方案，不宣称本轮已经安装到多台电脑。

本轮集中监控交付：0.2.34 单主窗口 UI、日志后台与完整包已构建；125 项关联回归、44 项 Node 测试通过，真实双后台面板测试确认不打开新窗口、不改变当前终端或发送 PTY 操作。打包 EXE 的所有者日志 RPC 与 `logs --once` 已在隔离后台验证；公网 17/16 文件快照回读验签通过。日志需各电脑使用新版后台，旧后台仍显示状态而不被强制重启。证据：`.omx/artifacts/daemon-live-logs/`、`.omx/artifacts/daemon-monitor-ui/`。

- [x] 连接设置简化：Windows/Android 主入口采用中继 IP/地址、默认端口和默认记忆的 Token，测试后列出全部授权房间并默认全选；手填房间移入高级设置。已取消房间保持取消，新授权房间自动同步。Windows 无模式启动支持 `SuperMonoAgent.exe --relay URL --token TOKEN`，后台默认 `127.0.0.1:6769`；新状态房间默认本机名，已有房间与运行后台保留。代码与定向证据见 `tests/test_workspace_simple_connect.py`、`tests/test_machine_room_startup.py`、`tests/test_launcher_default_options.py` 及 `.omx/artifacts/simple-relay-setup/`；Android 原生页面仍需使用包含该改动的新 APK，不以源码验证替代真机安装。

- [x] 本轮并发回归修复：Windows UI 存储、打包器及发布事务取消获取文件锁之前的空文件初始化写入，避免另一进程已经锁住空文件时触发 PermissionError。两个真实子进程的确定性用例先复现失败，修复后更新存储/发布 23 项、9 子测试通过，含真实 WebView 恢复的较宽回归 33 项通过。整套修复前基线为 538 passed、1 skipped、57 subtests，唯一失败即此并发项；不将该基线写成最终整套全绿。证据：`.omx/artifacts/simple-relay-setup/{full-suite.xml,update-lock.xml,locks-final.xml}`。

- [x] Windows 左上角菜单新增“刷新中继”：重新查询全部已配置中继的授权房间和各电脑窗口清单；保持在线终端及当前视图，支持目录断线重试，连续点击限流。两个真实隔离中继的目录漏推送、窗口漏推送与重试测试通过；工作区相关回归 30 项、Node 42 项通过。证据：`.omx/artifacts/relay-menu-refresh/`。

本节覆盖下面的历史快照；全目标仍未通过完整完成审计。最新双端构建为 **0.2.36**，版本目录不自动等于正式验收：Android 0.2.24（code26）已有本轮真机基础验证，Windows 0.2.28 已完成默认 Token 的实际打包 UI 定向验证；随后观察到外部安装 Android 0.2.27。产物索引见 `dist/README.md`。

| 事项 | 当前结果 | 权威依据与未关闭边界 |
| --- | --- | --- |
| 授权方式 | [x] 按用户最新选择使用 Token＋房间号 | 用户已取消统一邮箱账号交付要求；保留的统一账号高级源码不再构成生产开户或邮箱绑定交付门槛。 |
| 默认 Token | [x] 已接入用户指定文件 | 实际文件为 `dist/windows/0.2.24/token.txt`；私有 `~/.supermonoagent/token-source.json` 只保存路径。实际 6769 后台和 Windows 0.2.28 打包 UI 已验证同一默认凭据鉴权、显式房间范围及跨目标清除预填，测试守卫下输入/resize 发送均为 0。证据：`.omx/artifacts/token-room-provision/{runtime-source.json,live-owner-wire.json,packaged-default-ui.json}`；不记录 Token 内容。 |
| 翻屏与历史提示 | [x] 已完成针对性修复和验证 | 修复重连后按钮仍捕获旧 generation 的闭包，并在 Windows 保留 CSI 3J 清理前的历史；普通历史在本地翻页。确认实际运行的 Codex 备用屏才发送 PageUp/PageDown/Ctrl+Home/Ctrl+End，手动选择配色不当作运行身份；其他无本地历史的全屏程序给出明确提示，不猜按键。提示已合并到操作栏。依据：`tests/test_workspace_paging.py`、`tests/test_codex_owned_paging.py`、`.omx/artifacts/codex-owned-paging/real-codex.json`（真实 Codex 0.164.0、隔离环境、四键各一次、无模型或用户 PTY 输入）。 |
| 生产加密与发布 | [x] HTTPS/WSS 和签名更新已部署 | 不再以“缺 SSH/TLS”作为当前阻塞。公网证书、WSS 和更新包回读验签见 `.omx/artifacts/wss-migration/public-tls.json`、`.omx/artifacts/ui-menu-profiles/publication-verified.json`；续期 timer 的新鲜只读证据为 `.omx/artifacts/goal-completion-audit/cert-renew-timer.json`（active/enabled、service success、exit0）。 |
| 生产房间所有权登记 | [ ] 严格登记尚未迁移上线 | 当前真实 WSS 空目录订阅返回 `token_discovery:true`、`host_enrollment:false`，见 `.omx/artifacts/goal-completion-audit/production-directory-capabilities.json`。独立 owner secret 的源码与隔离测试不能证明生产已启用；该项不同于已取消的统一邮箱账号。迁移需先为对应主机配置所有权凭据，按 [迁移说明](global-workspace-security.md) 执行，不静默替换现有主机连接。 |
| Android 实机基础验证 | [x] API2 0.2.24 安装及定向验证已完成 | 保留数据安装、六种模式、655 字符中英草稿、临时 WSS 多主机切换及草稿保留已实测；原主机、空草稿和原生 sender 已恢复，临时房间已清理。依据：`.omx/artifacts/android-api2-live/{initial-state.json,multihost-validation.json,restored-after-probe.json}`。 |
| Android 输入法边界 | 未实测真实 OS IME | 已做真实 WebView 的合成 composition 事件守卫，不能替代系统输入法候选、确认或实际软键盘验收；不宣称这一项通过。 |
| Android HTTPS 直连 | [x] 已修复并通过定向验证 | 目录发现失败不能视为直连成功；同一 endpoint/Token 必须完成真实 HELLO 鉴权，修改地址、Token 或房间会作废回执。真实 TLS 非 loopback DNS fixture 已验证成功直连、错误 Token 和 relay 认证失败，见 `.omx/artifacts/android-credential-direct-fixes/verification.json` 及 `goal-completion-audit/post-fix.xml`。 |
| Android 已保存 Token | [x] 已修复并通过定向验证 | 程序自动预填、临时解密失败和未编辑的 onPause 不触发密文写入/删除；只有用户编辑、明确清空或取消保存才提交变更。JVM 保存策略、AES/GCM 与目标绑定回归通过，证据同上；不读取或记录真实凭据正文。 |
| Android 故障回退 | [ ] 仍待修复后真机复验 | 旧独立 QA 0.0.2 的 good 更新及草稿/配置保留通过；throw2 实际退到 bundled，而非预期 good2，good2 缓存 16 文件完整且 Java 验签通过。Native 退休/已知良好视图与状态字段修复已落地，但修复 QA 0.0.3 的安装会话 81224 已终态 `INSTALL_FAILED_ABORTED: User rejected permissions`，设备未安装该修复包；throw3/hang3 未执行，不能以源码/JVM通过替代真机通过。依据：`.omx/artifacts/android-ui-rollback-qa/qa-20261023T153711Z-00fddc0833a0/{physical-verification.json,cached-good2-integrity.json,cached-good2-java-verification.txt,fixed-install-terminal.json}`。 |
| 新一轮整套验证 | 整套基线及后续定向修复通过 | `.omx/artifacts/goal-completion-audit/full-suite.{xml,txt}` 为 499 passed、1 skipped、57 subtests（505.96 秒），Node 42 passed。随后发现的两项 Android 连接回归已修复，41 项定向测试及全部 Java 编译通过，见 `.omx/artifacts/android-credential-direct-fixes/verification.json`。0.2.30 双端已构建；不把测试或构建当作手机故障回滚验收。 |

### 历史记录阅读规则

补充复验：Root 在修复后扩大运行 Android 探测、保存凭据、房间选择与恢复测试，**46 passed、8 subtests passed**；证据为 `.omx/artifacts/goal-completion-audit/post-fix.{xml,txt}`。0.2.30 双端构建成功，Windows 46 个源文件哈希一致，Android 公开默认凭据为空。

以下内容保留原日期与当时检查结果。“当前”“最终”、旧版本号、旧安装会话及未勾选项均只描述各节当时的状态，不代表今天仍缺 Token 文件、没有 SSH/TLS 或仍在等待旧安装进程。最新未关闭的故障回退门槛以上表为准。

## 历史：2026-10-23 发布快照

当时发布记录为 **0.2.25**（Windows/Android 同号）；后续构建和真机验收应分别看当前摘要与 `dist/README.md`，不能由版本目录推断全部通过。

## 2026-10-23：最新授权方式调整

用户明确改为 Token＋房间号，不再要求统一邮箱账号登录。统一账号源码保留为兼容高级能力，不再把其生产开户、grants 与邮箱绑定当作当前交付门槛。默认 Token 应在运行时读取用户指定的 `token.txt`，不写入公开 APK/EXE/界面包；当前尚待确认文件完整路径（项目及常用配置目录未找到）。

- [ ] 接入指定 token.txt 的默认 Token，验证目标绑定与公开产物不含凭据；文件已加入 Git 忽略。

## 2026-10-23：底部历史提示与翻屏反馈

- [x] “已恢复有限输出历史……”合并到上翻屏/下翻屏所在操作栏，长提示省略显示并保留完整悬停文字，不再单独占一行。
- [ ] 用户反馈底部翻屏按钮没有效果；需继续区分普通历史缓冲区与 agent 全屏/备用屏的行为并验证，尚不标记为已修复。

## 2026-10-23：0.2.22 菜单、多中继与独立 Agent 模板

- [x] 左上角折叠按钮右侧、supermono 左侧加入“菜单”，含检查更新、下载并更新、连接工作区；旧小图标入口兼容为明确文字。
- [x] 简单连接表单仅需地址/IP、端口、Token；支持多组凭据自动发现，移除一组不影响仍由其他 Token 授权的房间；默认不长期保存 Token，高级账户/房间设置保留为折叠入口。
- [x] 每个房间有省略号菜单：新增窗口、刷新所有窗口、关闭所有窗口；批量关闭有进程终止确认及固定目标集合/代际校验，其他房间不受影响。
- [x] 六种 Agent 各有 Windows/Android 独立 JSON 模板：Codex、Claude Code、Grok、Kimi、pi.dev、Cursor CLI；模式按电脑和会话保存，切换不清屏、不发送输入或 resize。见 [独立模板与调试](agent-render-modes.md)。
- [x] 修复“0.2.20 外壳仍显示旧 r4 UI”：默认缓存按内置快照哈希分代，继承序号保护但不继承旧活动指针；已有缓存/进程不修改。
- [x] 新模板资源使用 Native API 2（Windows 17、Android 16 文件），首次需安装新外壳。旧 API 1 明确拒绝新包；USB 完整快照同步升级并对旧覆盖整体回退。
- [x] 全量 390 passed、1 skipped、55 subtests passed；仅剩一个旧用例仍按旧按钮位置查找，更新菜单选择器后相关 4 项定向通过。Node 42 passed。证据 `.omx/artifacts/ui-menu-profiles/`。
- [x] 0.2.22 Windows/APK 已构建；实际 EXE 的 45 个源文件 hash、账号/发现/隔离 CMD 输入验证通过。17/16 文件 API2 界面包已发布并从 HTTPS 公网重新下载验签。
- [x] 已另开 0.2.22 Windows 可见窗口（PID 53576、frontend 6779），实际服务使用新 bundled 命名空间，菜单位置、三个菜单项、简单连接框、六模式和高亮入口均在新界面中；旧 0.2.20 窗口及 daemon/Codex/Kimi 继续运行。证据 `.omx/artifacts/ui-menu-profiles/live-new-frontend.json`。
- [ ] Android 真机本轮未安装：adb 当前无设备；APK 为 `dist/android/0.2.22/SuperMonoAgent.apk`。首次需覆盖安装 API2 外壳后才能热更新模板，不把 JVM/Chromium 验证当作真机已升级。

## 2026-10-23：远程 Codex/Claude 显示颜色

- [x] 对 JINLONG3090 做只读颜色统计：3 个历史快照及采样期间 2 个活跃流只有 reset/bold/dim/underline，无前景/背景色码；没有发送输入、调整尺寸或重启终端。证据 `.omx/artifacts/remote-terminal-colors/live-readonly.json`。
- [x] Windows 与 Android 共用显示层文本高亮；Windows 默认开启，可在底部“文本高亮”切换并保存选择；保留原生 ANSI 配色和终端字节。真实 Relay 下 DOM/WebGL 双端的实时/快照、开关与释放回归通过。
- [x] 后端有界历史保留裁剪点的 SGR 状态，避免重连历史丢色，能力标记 `snapshot_sgr:true`；远端旧 daemon 仍需正常升级才具备此能力，不自动重启现有终端。
- [x] 0.2.19 双端签名界面包已正式发布到现有 HTTPS 更新频道，公网重新下载、CNG 验签及 ZIP 校验通过（Windows 11 文件、Android 10 文件）；设备需执行“检查更新”并安装，未替用户强制重载。Windows 包 `dist/windows/0.2.19/` 已重建验证。
- [x] 最终全量回归 340 passed、1 skipped、55 subtests passed；Node 34 passed。证据 `.omx/artifacts/remote-terminal-colors/{full-suite.xml,publication-verified.json}`。没有重启 JINLONG3090 后台或向现有终端输入命令。

## 2026-10-23：Windows 机器与窗口两级列表

- [x] 每台机器只列一次，机器名下显示对应房间号；本机若收到多个中继房间则一起显示，没有提供房间号时明确标注。
- [x] 窗口直接缩进列在机器下面，去掉项目层、终端分组层及新建分组按钮；保留所有旧窗口、状态灯、改名、配色和电脑设置。
- [x] 移除搜索框，直接列出电脑与所有开启窗口；机器旁箭头可独立收起/展开，在页面刷新及速率刷新后保持状态；收起不关闭终端。
- [x] 当前 Windows 包 `dist/windows/0.2.18/`，同步使用 Android 版本号 0.2.18；机器隔离、窗口层级和状态灯回归通过。

以下早期交付目录和版本号为历史记录。

## 2026-10-22：TD-001～TD-006 执行状态（历史快照）

用户已确认当轮包含新增 TD-006；以下为 2026-10-22 时的状态，后面的 2026-10-21 检查、登记和旧包验收也作为历史记录保留。今日结论以文首当前摘要为准。详细代码落点和证据见 [验证记录](todo-completion.md)。

| 编号 | 当前结果 | 验证/剩余步骤 |
| --- | --- | --- |
| TD-001 | [x] 已实现收发速率与连接质量格 | 字节/时间采样、空闲零速率、重连/验证失败、按电脑独立统计及多宽度回归通过 |
| TD-002 | [x] 换行、确认网格与原生缩放通过 | 真实 CLI 中英文草稿、IME、窄分屏；WebView2 普通/最大化/窄窗口 × 100%/125%/150% 共 9 组通过，跟底与主动历史浏览均验证 |
| TD-003 | [x] 已移除新版经典入口 | 源码及隔离打包 EXE 页面验证；经典路由继续兼容 |
| TD-004 | [x] 已完成并扩大粘贴验证 | Windows 流式边界/UTF-8/CRLF、真实隔离 Kimi 与 PowerShell/Codex 草稿证据 |
| TD-005 | [x] 已完成受控全量重绘 | 现代 ACK、旧协议 PONG 屏障、丢确认/重连/IME/租约竞争回归 |
| TD-006A/B/C/H | [x] 桌面多主机与隔离回归 | 同 relay、相同 session ID、含冒号 host ID、并发输出/定向输入、断线/错 Token/延迟回调 |
| TD-006D | [ ] Android 源码和 APK 完成，真机安装待完成 | 双主机/生命周期与改地址清旧草稿回归通过；之前覆盖安装停在荣耀本人指纹/系统密码验证，最后确认已装版本为 0.2.4；当前 adb 无设备，新包 0.2.14(16) 未覆盖安装 |
| TD-006E/F | [x] 已知房间鉴权发现与推送 | 无公开房间 roster，错误凭证/未知/无法验证离线一致；不轮询 health |
| TD-006G | [x] 统一身份/分发设计完成 | 见 [安全与身份设计](global-workspace-security.md)，每主机独立凭证，不引入全局明文 Token；统一账户未上线 |
| TD-006I | [x] 资源评估完成，实测边界已登记 | 1/4/16 主机消息/内存、未订阅输出为零、Android 后台关闭；未声称测得长时间电池功耗 |
| TD-006J | [x] 协议与架构说明已更新 | 新鉴权、TLS/Host/Origin、目录推送、所有权登记及客户端连接模型 |
| TD-007 | [x] Windows 左栏窗口状态灯与分组缩进 | 红色断开、黄色无近期输出、闪烁绿色实时输出；黑底可见、窗口行增加缩进；相关 Python 回归 16 项、Node 32 项通过，新包 `dist/windows-sidebar-status/`；详见 [状态灯说明](sidebar-activity.md) |
| TD-008 | [ ] 默认中继、统一账号与自动选择电脑，部署待完成 | 默认地址可选/手填、统一账号目录与自动填充已实现；TLS模板、真实Caddy及最终 Windows 包的账号/发现/隔离 CMD 验证通过；部署包 `dist/relay-hot-update-deployment.zip` 已生成，公网部署仍需要服务器管理入口；见 [配置说明](relay-account-setup.md) |
| TD-009 | [ ] 桌面/手机签名界面热更新，部署待完成 | 自动提醒、顶部手动检查、双平台打包发布、完整备份和回退已实现；真实 WebView2 挂死恢复通过，Windows 外壳和 Android 0.2.14(16) 已构建，签名更新包已生成；公网发布仍需 SSH/TLS 配置，手机未连接、未覆盖安装；见 [热更新说明](ui-hot-updates.md) |
| TD-010 | [x] Windows 同手机图标已打包 | 手机原图复用为七尺寸 ICO；当前正式编号统一为手机端 0.2.14，目录 `dist/windows/0.2.14/`，以后 Windows 同步使用手机版本号，不再按功能另起名字；EXE/窗口/任务栏图标与快捷方式已验证 |

安全审查发现的直连旧 Token 撤销与 Android 修改目标后旧草稿残留已修复并增加回归。原生缩放验收与 Windows 重建包验证已通过；最终测试数字见 [本轮验证记录](todo-completion.md)。尚未关闭的是手机系统允许后的覆盖安装及真机验证，不把 APK 构建通过等同于手机已升级。

交付目标：`dist/windows-global-workspace/`、`dist/android-global-workspace/SuperMonoAgent.apk`。现有 daemon/Kimi/Codex 与远程生产中继均未重启；生产 TLS 和房间登记需按文档迁移，不等同于本轮已远程部署。

## 2026-10-21：连接状态、长输入换行与顶部入口

状态：**仅检查和登记，等待实施。** 本轮不修改功能代码、不重打包、不重启客户端或 daemon。以下代码位置是实施落点，不代表已修改或已修复。

### 二次核实结果（2026-10-21）

三项均仍待实施，当前源码与原清单的主要描述一致。尺寸接管与快照回放已有修复，不应重复登记成“完全没有自适应”；本次 TD-002 专门追踪用户仍报告的长输入换行与边框越界，尚不能标记为完成。

| 补充核实项 | 证据 | 归入 |
| --- | --- | --- |
| 换电脑／重连仍保留上一连接的 RTT | `workspace.js` 的 `metrics.lastPongMs` 初始化后，仅在 OP 11 响应中更新；`connect()` 和 `switchHost()` 未清空它 | TD-001 |
| 验证失败却显示“重连中” | `ws.onclose` 先把 `#connection-label` 设为“离线 · 重连中”，遇到 4001 随后直接返回，没有安排重连 | TD-001 |
| 非房间码的远端直连也可能标成本机 | `sync()` 仅按 `config.code` 是否存在选择“中继已连接／本机已连接”，未区分直连目标是否在本机 | TD-001 |
| 窄分屏存在前后台最小行列不一致的边界 | `fitPane()` 使用 FitAddon 的本地行列，后台 `resize_session()` 会约束到至少 20 列、5 行；隔离 FakePty 检查实际确认：请求 8×3，后台接受并返回 20×5 | TD-002 |
| 已提交的 resize 尚未确认就被缓存 | `fitPane()` 在 `send(5, ...)` 成功后立即更新 `pane.lastSize`；发送成功本身不等于后台接受该尺寸或取得租约 | TD-002 |
| 删除按钮还需同步错误文案与打包界面 | “右上角经典版”的提示仍在 `sync()`；`workspace.py` 的页面嵌入打包代码，单独改 JS 不能完成顶部入口变更 | TD-003／共同交付门槛 |

证据边界：本次增加的运行核实只使用进程内隔离 FakePty，没有启动真实 shell，也没有向用户现有终端发送内容。8×3 → 20×5 证明尺寸约束差异可发生，但不能单凭这一点认定它就是用户当前 Codex 长输入问题的唯一根因。

| 编号 | 优先级 | 待办 | 状态 |
| --- | --- | --- | --- |
| TD-001 | P1 | 顶部显示每秒通信字节数和连接质量格子 | [ ] 待实施 |
| TD-002 | P0 | 长输入在可用终端宽度内换行，输入框边界不越出屏幕 | [ ] 待复现、定位及修复 |
| TD-003 | P1 | 移除右上角“经典版”入口及失效提示 | [ ] 待实施 |

### TD-001：顶部通信速率与连接质量

检查事实：

- [`workspace.py`](../src/supermonoagent/workspace.py) 的 `.titlebar`、`#connect-button`、`#connection-dot`、`#connection-label` 当前只显示连接文字和圆点。
- [`static/workspace.js`](../src/supermonoagent/static/workspace.js) 的 `metrics` 已有累计 `transmittedBytes`、`receivedBytes`、`lastPongMs` 和 `reconnects`，但没有每秒速率采样和顶部速率显示。
- `send()` 和 `input()` 累加发送的二进制帧；`handshake()`、中继的 JSON 入房消息走直接 `ws.send()`，未进入该发送计数。接收统计目前只累计二进制帧，未累计文本消息。
- `sync()` 每 15 秒发送一次应用层 ping，`ws.onmessage` 的 OP 11 更新最近 RTT；`updateFooter()` 目前只把 RTT 放到页脚。

登记的修改：

- [ ] 在连接文字旁增加至少 `↑ 发送速率`，按 `B/s`、`KiB/s`、`MiB/s` 显示；建议同时增加 `↓ 接收速率`。
- [ ] 采用约 1 秒的字节增量采样，除以实际经过的时间；明确统计的是当前连接的应用消息吞吐量，不是物理链路带宽上限或运营商网速。
- [ ] 统一二进制、文本、握手、心跳等消息的计数口径，避免遗漏或重复统计；只记字节数，不记录消息正文或 Token。
- [ ] 增加类似手机信号的 0–4 格连接质量图标。依据连接状态、RTT、心跳新鲜度和重连情况分级，阈值在实施时统一定义；不得把传输量大小当成连接质量，也不声称能读取真实无线信号强度。
- [ ] 空闲时正常显示 `0 B/s`；连接中、离线、重连中和质量数据未知时分别显示正确状态。
- [ ] 工作区切换、重连时重设采样基线，防止上一台电脑的流量混入；计时器随连接生命周期和页面退出清理，避免重复定时器。
- [ ] 同时清空或作废上一连接的 `lastPongMs`，记录响应时间戳；新连接尚未测得 RTT 时显示“未知”，不沿用旧电脑的质量格数。区分全应用累计诊断计数与当前连接的显示采样。
- [ ] 修正 4001 验证失败的顶部状态：既然不自动重试，就不能继续显示“重连中”；4004 电脑未上线且仍在重试时应采用不同状态。增加相应状态转换回归。
- [ ] 根据当前电脑／连接目标区分本机、远端直连和中继连接，避免仅按有无房间码把远端直连显示成本机。
- [ ] 顶部右侧保留连接设置的点击行为。桌面和窄屏下状态文字、速率及格子不互相覆盖，不挤出页面。

代码落点：

| 文件 | 待改位置／职责 |
| --- | --- |
| [`workspace.py`](../src/supermonoagent/workspace.py) | 顶部状态区 DOM、速率与质量图标的可访问标签 |
| [`static/workspace.js`](../src/supermonoagent/static/workspace.js) | `metrics`、`send()`、`handshake()`、`input()`、`connect()`、`ws.onmessage/onclose`、`sync()`、`switchHost()`、退出清理；新增统一采样及显示更新 |
| [`static/workspace.css`](../src/supermonoagent/static/workspace.css) | `.titlebar`、`#connect-button`、连接文字及新状态组件的布局与窄屏规则 |
| [`tests/workspace-client.test.cjs`](../tests/workspace-client.test.cjs) | 速率换算、空闲、时间间隔、质量分级和采样重置的单元回归 |
| [`tests/test_workspace_hosts.py`](../tests/test_workspace_hosts.py) | 切换电脑后统计不串台；连接设置仍可打开 |

验收：给定已知字节数和采样时间可准确计算 B/s；握手与中继消息统计口径一致；空闲不显示为断线；切换、断线和重连无负速率或历史尖峰；不同窗口宽度下顶部不溢出。

### TD-002：长输入换行与输入框边界

用户现象：输入较长文字后，文本及其边框向屏幕右侧延伸，没有在屏幕边缘自然换行。

检查事实与证据边界：

- 新版实际输入由 xterm 经 `term.onData → terminalInput() → input()` 发送到后台，进入真实 PTY，并不是普通网页文本框。
- [`static/workspace.js`](../src/supermonoagent/static/workspace.js) 的 `fitPane()`、`ResizeObserver` 和尺寸接管逻辑负责终端行列；`drainPane()` 负责按后台尺寸回放。当前后端尺寸会限制在 20–500 列、5–200 行，见 [`hub.py`](../src/supermonoagent/hub.py) 的 `resize_session()`；[`pty.py`](../src/supermonoagent/pty.py) 的 `WinPty.resize()` 再调用 `setwinsize(rows, cols)`。
- [`static/workspace.css`](../src/supermonoagent/static/workspace.css) 的 `#stage`、`.split-child`、`.pane`、`.terminal-screen` 使用受限尺寸及溢出裁切；标签等界面文本另有 `white-space:nowrap` 规则。这些规则存在，不能据此直接判定终端长输入的根因。
- 现有 [`test_responsive_layout.py`](../tests/test_responsive_layout.py) 和 [`test_frontend.py`](../tests/test_frontend.py) 主要验证页面几何尺寸、PTY 尺寸同步及接管，不能替代长输入换行的验收。
- **Inference：** 需要区分网页容器真的越界、PTY/xterm 列数不一致，以及 Codex/PowerShell 自身的输入编辑／终端换行模式。此次未向用户现有终端输入测试文本，具体根因尚未复现确认。

登记的修改：

- [ ] 先在隔离的测试会话复现长中文、连续无空格英文／数字、多行粘贴和中文 IME 输入，分别检查 Codex、PowerShell 的输入区。
- [ ] 同步记录容器可用宽度、xterm `cols/rows`、后台确认的 `controls`、光标位置和换行状态；区分应用关闭自动换行的模式与错误尺寸。
- [ ] 覆盖小于 20 列／5 行的窄分屏，确认前台拟合尺寸与后台约束后的有效尺寸一致；结合容器布局确定最小可用区域或其他明确的适配办法，不能让两端长期使用不同网格。
- [ ] 区分“请求尺寸”与“服务端确认尺寸”，核对 `pane.lastSize` 的缓存时机；处理后台限幅、未取得租约或其他客户端接管时的确认结果。保持共享观看与用户主动接管的边界，避免多个客户端反复抢尺寸。
- [ ] 若是布局越界，修正具体容器的收缩约束及允许换行的界面文字；不依靠 `overflow:hidden` 隐藏问题。
- [ ] 若是终端／应用绘制问题，在 `fitPane()`、回放时序、尺寸确认或 PTY 适配边界修复；必要时记录并验证 Codex/PSReadLine 对终端宽度的处理。
- [ ] 保持固定单元格终端模型，不直接对 xterm 字符层施加 `word-break` 来代替终端换行，也不盲目强制改变应用的 VT 模式。
- [ ] 视觉换行不得向输入自动插入 `\r`、`\n` 或 Enter，避免改变命令内容或执行用户尚未提交的输入。

代码落点：

| 文件 | 待检查／可能修改位置 |
| --- | --- |
| [`static/workspace.css`](../src/supermonoagent/static/workspace.css) | `main`、`#stage`、`.split`、`.split-child`、`.pane`、`.terminal-screen`；输入相关 DOM 的宽度约束 |
| [`static/workspace.js`](../src/supermonoagent/static/workspace.js) | `createPane()`、`term.onData`、`drainPane()`、`fitPane()`、`takeControl()`、`openView()`、窗口 resize 与尺寸确认 |
| [`static/terminal-client.js`](../src/supermonoagent/static/terminal-client.js) | `terminalInput()` 与快照处理边界；核对软换行不改变发送字节 |
| [`hub.py`](../src/supermonoagent/hub.py)、[`pty.py`](../src/supermonoagent/pty.py) | 尺寸租约、有效行列限制、`WinPty.resize()`；仅在前后端证据指向此处时修改 |
| [`tests/test_responsive_layout.py`](../tests/test_responsive_layout.py)、[`tests/test_frontend.py`](../tests/test_frontend.py)、[`tests/workspace-client.test.cjs`](../tests/workspace-client.test.cjs) | 新增长输入的实际换行、光标、边界及原始输入字节回归 |

验收：在 390、850、1440、1920 px 宽度及分屏中，输入超过一屏宽的中文／连续英文数字，输入区边界不越界，文字按当前有效宽度换行；补测 19／20／21 列、4／5／6 行的约束边界及浏览器缩放／Windows DPI 场景；缩放后光标、边框与 Codex 底栏正确；退格、方向键、中文组合输入和粘贴正常；仅输入而未按 Enter 时不执行命令。需分别验证桌面 WebView2 和浏览器。

### TD-003：移除右上角“经典版”

检查事实：

- [`workspace.py`](../src/supermonoagent/workspace.py) 顶部包含 `<a href="/legacy" class="legacy">经典版 ↗</a>`。
- [`static/workspace.css`](../src/supermonoagent/static/workspace.css) 有 `.legacy` 的样式和媒体查询。
- [`static/workspace.js`](../src/supermonoagent/static/workspace.js) 的 `sync()` 在协议不兼容时仍提示“使用右上角经典版”，删除按钮后该提示会失效。

登记的修改：

- [ ] 删除新版顶部的“经典版”链接，右上区域只展示当前连接状态、通信速率及连接质量。
- [ ] 清理专属于 `.legacy` 的样式；保留与其并列选择器下其他仍需使用的窄屏样式。
- [ ] 更新协议不兼容提示，不再指引用户点击已移除的按钮；保留清楚的后台升级／切换工作区提示。
- [ ] 此项范围是移除新版入口，`/legacy` 路由和既有经典客户端服务保持可用，避免影响现有链接和会话。
- [ ] 实施时同步修订 [`docs/workspace-hosts.md`](workspace-hosts.md) 等“右上角进入经典版”的说明。

代码落点：`workspace.py`、`static/workspace.css`、`static/workspace.js::sync()`；回归落点为 [`tests/test_workspace.py`](../tests/test_workspace.py) 及 [`tests/test_client_routes.py`](../tests/test_client_routes.py)。

验收：桌面与窄屏右上角均无“经典版”入口，连接设置仍能使用；错误提示无失效入口；顶部状态与速率不越界；原经典路由测试继续通过。

### 后续实施顺序

先为 TD-002 建立长输入复现与回归，再处理终端换行；TD-001 与 TD-003 共用顶部 DOM，可在同一轮顶部调整中完成。用户要求当前仅登记，三个事项均保持未勾选，不启动实施或发布。

### 共同验收与发布门槛（依附上述三项）

- [ ] 在隔离会话完成回归，保留用户现有 daemon、Kimi、Codex 进程；不把给真实用户会话自动发送长文本当成无副作用测试。
- [ ] 修改后检查源码与最终交付的 HTML／JS／CSS 一致。`workspace.py` 页面嵌入打包代码，需要重新打包后验收实际 EXE；只覆盖静态 JS 不能代替完整发布验证。
- [ ] 用 [`frontend.py`](../src/supermonoagent/frontend.py) 的界面 revision 机制确认客户端连接到对应版本的 frontend，避免旧 HTML 搭配新 JS。核实 [`desktop.py`](../src/supermonoagent/desktop.py)、[`scripts/build-windows.ps1`](../scripts/build-windows.ps1) 的桌面／浏览器入口使用一致的新版工作区。
- [ ] 实测最终包的顶部状态、通信速率、长输入、页签配色、房间码工作区切换及关闭视图保留后台会话；同步使用说明。文档不把已有几何尺寸测试通过等同于长输入问题已修复。

## 2026-10-21：Kimi 黏贴与 Codex 输入栏重绘

状态：**已完成代码、自动化回归和独立 Windows 包验证。** 未重启 daemon，也未向现有 Kimi/Codex 会话注入测试输入；当前运行实例仍使用旧包，需用户自行选择何时切换到 `dist/windows-paste-fix`。

### 已确认事实

- 本机版本：Kimi CLI 2.3.0、Codex CLI 0.163.0。
- 前端 `terminalInput()` 不会剥离 bracketed paste；`tests/workspace-client.test.cjs` 已确认 `ESC[200~...ESC[201~` 会原样进入后台。
- Kimi 支持 bracketed paste，但 Windows ConPTY 会剥离标记；Codex 对无标记的快速黏贴流兼容更好，因此表现为“Codex 可以、Kimi 不行”。
- Codex 输入栏使用 ratatui/crossterm 的 diff 渲染和 inline viewport。重连或重开视图后只重放有限原始输出，无法可靠恢复光标、滚动区域和输入栏状态；之后只发变化单元格，旧画面可能持续存在。
- 尺寸变化会迫使 Codex 全量重绘；当前 workspace 在打开、点击、窗口尺寸变化时可能自动接管尺寸，需避免多视图反复抢租约。

### TD-004：Kimi bracketed paste 兼容

| 编号 | 优先级 | 待办 | 状态 |
| --- | --- | --- | --- |
| TD-004A | P0 | 在隔离会话记录单行、中文、多行黏贴到达 daemon 的实际字节 | [x] 已由单元回归和真实 CLI 探针覆盖 |
| TD-004B | P0 | 在 [`pty.py`](../src/supermonoagent/pty.py) 的 `WinPty.write()` 前为 ConPTY 解包 bracketed paste | [x] 已完成 |
| TD-004C | P0 | 增加 Kimi 形态输入回归：短 ASCII、中文、多行、跨包标记和换行不提前提交 | [x] 已完成 |

要求：只在 Windows/ConPTY 路径处理；不盲目过滤其他 CSI；黏贴只写正文，不向输入自动追加 Enter；保留 Codex 当前可用行为。

### TD-005：Codex 输入栏全量重绘与尺寸竞争

| 编号 | 优先级 | 待办 | 状态 |
| --- | --- | --- | --- |
| TD-005A | P0 | 在 [`static/workspace.js`](../src/supermonoagent/static/workspace.js) 的接管、重连或快照回放完成后触发一次受控全量重绘 | [x] 已完成；另有“刷新”按钮 |
| TD-005B | P1 | 收敛 `openView()`、`activate()` 和浏览器 resize 中的自动接管，避免相同尺寸反复请求 | [x] 已完成 |
| TD-005C | P1 | 统一 [`hub.py`](../src/supermonoagent/hub.py) `resize_session()` 与 `resize()` 的行列上限 | [x] 已由共享 `_terminal_size()` 完成 |
| TD-005D | P0 | 增加 Codex 形态 diff 输出、截断快照、重新订阅和尺寸竞争的回归 | [x] 已覆盖重开视图、尺寸弹跳和最终尺寸恢复 |

要求：不能把“扩大历史缓冲”当成完整 VT 状态恢复；若采用尺寸弹跳强制重绘，必须限频、只在接管或用户明确刷新时触发，并避免影响正在输入的内容。

### 本轮共同验收

- [x] 运行 Python 与 JavaScript 全量测试：96 项 Python、6 项子测试、12 项 JavaScript 通过。
- [x] 在隔离环境验证 Kimi/Codex 真实 CLI 黏贴探针和 Codex 尺寸弹跳重绘；未向用户当前会话发送测试内容。
- [x] 已重建 `dist/windows-paste-fix`；26 个源码哈希全部匹配，EXE 校验值匹配且 CLI 可启动。
- [x] 已更新本文件；现有 daemon、Kimi、Codex 进程均未被擅自终止。

## 2026-10-21：全局桌面工作区互联

状态：**仅完成分析并登记，尚未实施。** 详细分析见 [`global-workspace-analysis.md`](global-workspace-analysis.md)。目标是把当前“桌面/手机保存多台电脑但逐台切换”升级为“同时连接多台电脑，并统一显示和操作各电脑的终端”。

### 分析结论

- 底层 daemon、relay、房间码、机器/项目 identity、多会话订阅和 per-host Token 已具备。
- 当前缺口主要在客户端：桌面 workspace 与 Android 都是单 WebSocket 切换模型；同时连接时需要按 `hostId:sessionId` 区分会话，避免不同电脑撞号。
- relay 已有 `/health` 项目/主机目录，但目录当前无鉴权地暴露房间码、机器名和项目名；接入发现前必须收紧。
- 第一阶段可以不改 daemon、relay 转发协议或终端帧格式，只做桌面与 Android 的并行聚合。

### TD-006：全局桌面工作区互联

| 编号 | 优先级 | 待办 | 状态 |
| --- | --- | --- | --- |
| TD-006A | P0 | 将 `workspace.js` 的单连接状态抽成 per-host `HostConnection`，每台电脑独立保存 ws、catalog、controls、重连、速率和错误状态 | [ ] 待实施 |
| TD-006B | P0 | 将会话内部键改为 `hostId:sessionId`，输出、输入、订阅、尺寸租约、刷新和关闭视图均按 host 分流 | [ ] 待实施 |
| TD-006C | P0 | 侧栏与页签按“项目 → 电脑 → 分组 → 会话”聚合显示，每台电脑显示独立在线状态、RTT、速率和协议版本 | [ ] 待实施 |
| TD-006D | P0 | Android 页面改为多 host 目录并行感知；仍只渲染一个活动终端，未打开会话不订阅大输出 | [ ] 待实施 |
| TD-006E | P1 | 为 relay 主机目录增加鉴权或“已知房间在线状态”接口，再把发现/选择电脑接入 workspace 增加工作区对话框 | [ ] 待设计 |
| TD-006F | P1 | 增加主机上下线推送或低成本目录订阅，避免客户端轮询 `/health` | [ ] 待设计 |
| TD-006G | P1 | 设计跨主机统一身份/Token 分发；不得以共享明文 Token 换取便利 | [ ] 待设计 |
| TD-006H | P0 | 双隔离 daemon + 共享 relay 回归：相同 session id、并行输出、定向输入、单 host 断连/验证失败不影响另一台 | [ ] 待实施 |
| TD-006I | P1 | 评估多 host 心跳、输出订阅、手机耗电和内存；默认只给打开的终端订阅输出 | [ ] 待实施 |
| TD-006J | P1 | 更新 `protocol.md`、`architecture.md` 的安全现状和连接模型，使其与当前 token 校验及聚合设计一致 | [ ] 待实施 |

### 实施顺序与验收

1. 先做 TD-006A/B/C/H，形成桌面端双主机并行聚合 MVP；不改 relay。
2. 再做 TD-006D 的 Android 聚合目录与活动终端切换。
3. 发现、目录推送和统一身份必须先完成安全设计，再进入实施。
4. 验收至少覆盖：两台电脑同 session id 不串画面；双向输入路由正确；一台离线或 Token 错误不影响另一台；每台电脑状态独立；关闭视图不结束后台；手机和桌面看到的电脑/项目目录一致。
5. 生产启用前必须完成 TLS、Origin 校验、Token 移出 URL 查询串及 relay 目录鉴权。
