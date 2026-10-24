# Windows 安装与多电脑接入方案评估

## 实施更新

本文后续保留原评估快照。当前已交付 **0.2.36 完整 ZIP 一键安装器**、签名文件目录、默认配置注入、版本目录/指针、单一桌面快捷方式、当前用户卸载登记和受限专用下载服务；安装版支持从同一中继目录自动检查、下载校验并提醒安装完整程序，实际使用见 [一键安装说明](windows-oneclick-install.md)。NPM/winget 注册、原生程序启动失败自动回退、干净虚拟机和多台实机验收仍未实施，不能把本次 ZIP 安装当作这些项目已经完成。

评估日期：2026-10-24。状态：方案文档，未实现安装器、未安装到新电脑、未发布 winget/npm 包。本次只读核对本地 **0.2.33** 编译产物和当前源代码，并查阅官方资料；不把产物存在视为正式验收通过。

本文体积和签名表是 0.2.33 的固定评估快照，不代表最新发行号；最新完整包以 `dist/README.md` 为准。本轮另行实现的守护进程日志功能随新版程序提供，采用一个主窗口集中展示各电脑的 daemon 状态与日志，不为每个后台自动弹窗，并保留高级 `logs` 命令；它不等于已经实现本方案中的安装器或包管理渠道。source_path：`src/supermonoagent/daemon_logs.py`、`docs/windows-clients.md`。

## 推荐结论

可以实现接近 `npm install` 的安装体验，而且普通用户无需先装 Python 或 Node.js。推荐先交付**完整便携 ZIP + 免管理员的用户级安装脚本 + 固定启动入口与快捷方式**，复用现在的 PyInstaller 产物；完善完整程序升级、卸载、回退后，再接入 winget。图形安装 EXE 可以在同一安装规则上补充，首次交付不强制增加安装器编译工具。

用户最终只需安装、打开、填写中继 URL 与 Token。连接工作区沿用现有授权房间目录，可保存多个服务器配置；只有想让其它电脑看到本机终端时，才需要配置本机后台的中继注册。安装客户端和把本机作为主机共享，必须在引导里分清。

继续使用 `0.2.33` 这样的数字版本，与 Android 保持一致。安装渠道、UI 更新渠道、文件名都不另造功能版本名；后续实现使用届时的实际数字版本，不重发不同内容的同版本包。

## 当前包与启动前提

以下数据来自 `dist/windows/0.2.33/` 的全目录只读枚举，未创建新的构建或压缩包。

| 核对项 | 实际结果 | 含义 |
| --- | --- | --- |
| 完整目录 | 101 个文件，34,979,636 字节，约 33.359 MiB | 是解压后体积；ZIP 下载体积尚未测量 |
| 主程序 | `SuperMonoAgent/SuperMonoAgent.exe`，6,147,178 字节 | 不能单独拷贝这个 EXE |
| Python | `_internal/python314.dll`，6,753,112 字节，另有标准库和扩展 | 已内置 Python 3.14；最终用户无需独立 Python |
| 终端后端 | winpty DLL、`winpty-agent.exe`、`OpenConsole.exe`、x64 Python 扩展 | 当前核对的是 Windows x64 包；不宣称原生 ARM64 已适配 |
| 桌面桥接 | pythonnet、clr_loader、WebView2 托管 DLL 与 Loader | 包含桥接组件，不等于包含完整 .NET/WebView2 Runtime |
| UI 与入口 | 静态页面、三组 CMD/VBS、图标、快捷方式辅助脚本、构建清单 | 现有便携启动能力可直接复用 |
| EXE 签名 | `Get-AuthenticodeSignature` 返回 `NotSigned` | 当前构建不是已经签名的正式安装器 |
| 构建识别 | EXE SHA-256 `f97e045a9e3f6994fc0c8be3439fedfa5c9479b2024c4009f7d82b00f9f1ced9` | 仅识别本次检查对象，不作为未来包的固定校验值 |

source_path：`dist/windows/0.2.33/build-manifest.json`、`dist/windows/0.2.33/version.txt`、`scripts/build-windows.ps1`。

`scripts/build-windows.ps1` 使用 PyInstaller `--onedir --console`，将 Python、模块、静态资源和 winpty 辅助程序放在同一目录。官方说明此类产物包含解释器，并受构建平台与架构约束；无需用户另装 Python，但仍可能依赖操作系统组件。[PyInstaller 运行方式](https://pyinstaller.org/en/stable/operating-mode.html)

桌面窗口需要 WebView2 Runtime 与可用的 .NET Framework。Python.NET 在 Windows 默认使用 `netfx`，官方列出的最低版本为 4.6.1，并推荐 4.7.2 或以上；本项目应先以 .NET Framework 4.8/4.8.1 的受支持 Windows 环境做验收，**这是一项拟定验收基线，不是本次已完成的最低版本兼容认证**。安装现代 .NET 8/9 并不能直接替代这里的 .NET Framework。[Python.NET 运行时说明](https://pythonnet.github.io/pythonnet/python.html)、[pywebview 安装要求](https://pywebview.flowrl.com/guide/installation.html)

WebView2 Evergreen 可由官方 Bootstrapper 安装；非提升权限下支持按用户安装，但企业策略或已有机器级更新器会影响行为。因此“免管理员”指本应用安装，不保证所有缺失系统依赖在任何受管电脑上均可免管理员补齐。安装前检测，缺失时提供官方安装入口或浏览器模式，不静默提权或修改策略。[WebView2 分发说明](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)

浏览器入口在导入 `webview` 之前就打开系统浏览器，可作为桌面引擎缺失时的现有备用入口；新电脑上的实际回退体验仍需验证。Microsoft Intelligent Terminal 是第三种可选入口，另需安装其外部应用和 `wtai.exe`，不应成为标准桌面安装的强制依赖。source_path：`src/supermonoagent/desktop.py`、`src/supermonoagent/microsoft.py`、`docs/windows-clients.md`。

## 三种分发路线

| 路线 | 用户体验与成本 | 适用边界 | 建议 |
| --- | --- | --- | --- |
| A：便携 ZIP + 用户级安装脚本，之后可加图形安装 EXE | 下载一次，安装到用户目录；建立快捷方式与稳定 CLI；不要求 Python/Node 或管理员 | 需要自行实现版本切换、卸载登记、完整包签名验证与运行进程保护 | **先做**；现有产物改动最少 |
| B：winget 管理相同 ZIP 或签名安装器 | 登记后可 `winget install/upgrade/uninstall`，多台开发机方便重复安装 | 需要可访问且不可变的发布 URL、正确清单、包标识与渠道审核；不能把网站目录直接当软件源 | 第二阶段；复用 A 的数据与进程规则 |
| C：npm 包装预编译 Windows 二进制 | 已装 Node/npm 的开发机可获得熟悉的命令入口 | 无 Node 用户反而多一步；还需处理平台选择、可选依赖缺失、代理、升级时被占用的文件和后台生命周期 | 可选兼容入口，不作为首发依赖 |

**winget 便携 ZIP 可行，但有格式要求。** 官方清单支持归档中的安装类型：`InstallerType: zip`、`NestedInstallerType: portable`，并声明 `NestedInstallerFiles/RelativeFilePath`；`PortableCommandAlias` 可提供命令别名。本项目的多文件 onedir 包必须验证整个 `_internal` 目录在安装后保留，不能只声明单独下载 EXE。别名也不会自动提供本项目需要的后台安全升级逻辑。[官方 ZIP/portable 清单规范](https://github.com/microsoft/winget-pkgs/blob/master/doc/manifest/schema/1.12.0/installer.md)

**MSI/MSIX 也可作为 B 的载体，但不推荐为首次交付重做封装。** MSI 支持按用户与按机器安装，不是天然必须管理员；需要正确编写上下文和升级规则。MSIX 必须签名且证书受设备信任，其应用身份、目录/注册表重定向和卸载语义需要另验。本项目涉及后台子进程、用户目录状态、外部 CLI 和 PTY，尚未做 MSIX 原型；不能据此断言不兼容，也不能宣称换壳后直接可用。[MSI 安装上下文](https://learn.microsoft.com/en-us/windows/win32/msi/installation-context)、[MSIX 签名](https://learn.microsoft.com/en-us/windows/msix/package/signing-package-overview)、[打包桌面应用运行行为](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes)

**npm 技术上可分发这个包，但不会消除原生运行时。** 可用 `bin` 提供 JS 启动器，按 `os/cpu` 选择二进制依赖；`optionalDependencies` 允许安装失败或被跳过，因此启动时必须明确检测缺包。它只增加分发入口，不把 Python/WinPTY 后端变成 JS。应避免把所有下载与配置放在隐式安装脚本里；即使将来提供 npm，也应共用 A 的安装目录和管理器，不复制一套状态。npm 工具自身仍需 Node/npm 环境。[npm package.json](https://docs.npmjs.com/cli/v11/configuring-npm/package-json/)、[安装 Node.js 与 npm](https://docs.npmjs.com/downloading-and-installing-node-js-and-npm/)

## 已存在与待实现

| 能力 | 当前状态 | source_path |
| --- | --- | --- |
| 数字版本 Windows 完整目录、内置 Python、图标、文件哈希 | 已有；版本从 Android manifest 读取 | `scripts/build-windows.ps1` |
| 启动桌面、浏览器、主机与中继的统一 EXE 分派 | 已有；无参数打开 workspace | `src/supermonoagent/launcher.py` |
| 从解压目录建立桌面快捷方式 | 已有辅助脚本；路径绑定当前目录，移动后需重建 | `scripts/create-desktop-shortcuts.vbs` |
| URL/端口/Token、多配置、授权房间发现 | 已有连接流程；权限与服务端登记仍须有效 | `docs/windows-clients.md`、`src/supermonoagent/static/workspace.js` |
| 复用已运行本机后台而不重启终端 | 已有启动行为；不能将新参数当作已运行后台的重配置 | `src/supermonoagent/desktop.py` |
| 签名 UI 热更新与页面缓存 | 已有代码和发布工具；只更新 UI，不替换 EXE/后端 | `src/supermonoagent/ui_updates.py`、`src/supermonoagent/frontend.py`、`scripts/publish-ui-update.py` |
| 用户级安装器、完整包下载/签名发布、稳定启动器、卸载登记 | 待实现；本次未找到对应发布流水线 | `scripts/build-windows.ps1`、`scripts/` |
| 完整程序 CLI 安装/更新/回退/卸载子命令 | 待实现；不能把下面的设计命令当作当前可执行命令 | `src/supermonoagent/launcher.py` |
| winget/npm 包及公开注册标识 | 本次未验证存在，不提供可直接安装的包名 | `pyproject.toml`、`scripts/` |

Python 源码入口已经有 `supermono`（daemon）、`supermono-relay`、`supermono-client`（统一启动器）。但 `pyproject.toml` 的项目版本仍是 **0.1.0**，与本次包 **0.2.33** 不同；如要正式发 Python 包或生成其它包管理元数据，应先统一版本来源。不能直接宣传 `pip install supermonoagent` 已可获取此版本。已有源码环境可从仓库执行 `python -m pip install -e ".[desktop]"`，这不是面向普通用户的免依赖安装路径。source_path：`pyproject.toml`。

## 实际操作与拟定操作

### 现在可用的操作

将完整 `dist/windows/0.2.33/` 拷贝到目标电脑并保持目录结构；双击 `02-Orca-Workspace.vbs`，或从该目录执行：

```powershell
.\SuperMonoAgent\SuperMonoAgent.exe workspace
# 缺少桌面浏览器引擎时，可显式使用现有浏览器入口：
.\SuperMonoAgent\SuperMonoAgent.exe browser --workspace
# 为当前解压位置创建桌面快捷方式：
cscript.exe //nologo .\create-desktop-shortcuts.vbs
```

在工作区的连接对话框中填写中继地址、端口和 Token，测试连接后选择授权房间；添加其它服务器配置即可并列列出其它房间。不要求统一邮箱账号，不代表任意 Token 都能列出服务端所有房间。source_path：`docs/windows-clients.md`、`src/supermonoagent/static/workspace.js`。

若希望**新启动的本机后台**也登记到中继，现有参数支持以下方式。Token 文件需由用户在本机私有位置预先准备，示例不含凭据；已有后台继续保留原配置：

```powershell
.\SuperMonoAgent\SuperMonoAgent.exe workspace `
  --relay 'wss://117.50.48.22/ws' `
  --token-file "$env:USERPROFILE\.supermonoagent\token.txt"
```

没有保存房间号的新后台默认使用电脑名登记；已有房间号保留。多电脑重名冲突、目录权限拒绝应给明确诊断，不能靠安装器静默改房间或替换现有后台 Token 来修复。source_path：`src/supermonoagent/desktop.py`、`src/supermonoagent/daemon.py`。

### 拟实现的最小安装体验

下载页面提供明确数字版本的完整 ZIP 和 `install.ps1`，普通用户可通过图形安装入口获得同样结果；开发机从 PowerShell 执行**下载到本地且已验证的脚本文件**：

```powershell
# 以下为未来接口设计，目前仓库没有这个安装脚本：
.\install.ps1 -Version 0.2.33
supermono-client workspace
```

首次只显示“查看已有电脑”与“共享本机”两种目的，复用现有地址/端口/Token 界面。默认中继从现有默认配置读取，也允许手填和多个配置；Token 不放进命令历史、快捷方式、公开安装包或诊断日志。查看远端房间不应自动为远端创建终端。

建议稳定入口复用已有名称 `supermono-client`，避免改变现有 `supermono` 的 daemon 语义。拟新增的管理命令如下，**都尚未实现**：

```powershell
supermono-client status
supermono-client configure
supermono-client upgrade --version 0.2.34
supermono-client rollback
supermono-client uninstall
```

`status` 应分开显示已选安装版本、实际客户端版本、正在运行的后台版本、UI revision 与连接诊断，隐去凭据。UI 菜单也应分开说明“更新界面”与“更新完整程序”，不能下载了页面就声称后台已升级。

winget 清单获批准后才展示实际注册 ID。未来命令形式为 `winget install --id <实际注册ID> --exact --scope user`；占位符不可直接执行。安装器仍须遵守标准用户范围，不能认为 `--scope user` 会自动修复一个只支持机器级安装的包。[winget 安装命令](https://learn.microsoft.com/en-us/windows/package-manager/winget/install)、[清单创建](https://learn.microsoft.com/en-us/windows/package-manager/package/manifest)

## 安装布局、凭据与运行中的终端

建议使用以下**未来布局**，目录名保持产品名和数字版本：

```text
%LOCALAPPDATA%\Programs\SuperMonoAgent\
  versions\0.2.33\...完整包...
  versions\0.2.34\...完整包...
  bin\supermono-client.cmd
  current.json                 # 原子切换的版本指针
  previous.json                # 最近已确认可用的完整版本
%USERPROFILE%\.supermonoagent\ # 沿用现有状态根目录，不随卸载/版本更换删除
```

1. 安装只写当前用户目录、当前用户卸载登记和开始菜单；桌面快捷方式、用户 PATH 作为可选项。受策略禁止 VBS 的电脑用稳定原生启动器或 CMD 备用，不要求解除系统限制。
2. 并行安装多个版本，快捷方式指向稳定入口。先完整下载到临时目录，验证后落入新版本目录，再原子更新指针，禁止原地覆盖运行中的 EXE/DLL。
3. 保留当前 `.supermonoagent` 下 Token、`token-source.json` 路径指针、房间号、机器身份及其它状态。不得读取 Token 后写进构建包；迁移日志只记路径与结果。外部 Token 文件不归卸载器所有。
4. 若 Token 路径仍指向旧的便携版本目录，清理旧包前必须提示迁移到稳定私有位置并验证引用；不能直接删掉该目录。不得以覆盖新默认值的方式改变已运行后台的授权。
5. 后台拥有 PTY。客户端关闭、安装升级或卸载不能发送 close/input/resize，不能无条件杀 daemon、PowerShell 或编码代理进程。旧后台继续提供已有终端，新客户端验证协议能力后复用；不兼容时保留旧客户端并明确说明。
6. 后台使用旧目录中的二进制时，该版本目录标记“运行中保留”。完整后台升级安排在会话结束后的显式维护操作，不能承诺在任意活跃 PTY 下无损替换后台。新版本的后端修复只有实际换用新后台才生效。

source_path：`src/supermonoagent/daemon.py`、`src/supermonoagent/token_config.py`、`src/supermonoagent/desktop.py`、`src/supermonoagent/frontend.py`。

**待验证：**工作区草稿、连接偏好含有浏览器 origin 范围的本地存储；现有前端还有按构建指纹划分的 UI 缓存。稳定安装路径本身不能保证全部偏好跨版本保留。实施前需固定 WebView 用户数据位置与适当的 origin，或设计受控迁移并验证端点绑定，不能把不同服务器的 Token 混用。不得通过放宽 origin 检查来“保留数据”。source_path：`src/supermonoagent/frontend.py`、`src/supermonoagent/static/workspace.js`。

## 完整包更新、失败回退与安全下载

完整程序包使用独立于 UI 热更新的发布清单：数字版本、架构、下载 URL、大小、SHA-256、最小系统/协议条件及签名。复用现有信任机制的设计原则，但不把 UI 的文件白名单和验收结果套到原生包；代码签名和原生包清单验证还需实现。

- 发布到不可变的版本 URL，可用 GitHub Releases 或现有 HTTPS 中继的独立下载目录；`latest` 只用于发现，不可作为唯一可追溯产物。发布流程从同一构建生成清单和 ZIP，检测凭据/运行数据，验完上传后再更新发现指针。当前只确认本地目录与 UI 发布工具，未验证完整运行时 Release 资产已发布。
- PowerShell 使用 `Invoke-WebRequest -OutFile` 下载，限制协议与允许的主机、超时、大小和重定向；不关闭证书校验，不转发 Token 给下载域。Windows PowerShell 5.1 与 PowerShell 7 的参数差异需测试，不要求先装新版 PowerShell。[下载命令](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/invoke-webrequest?view=powershell-7.5)
- 对照**已验证签名的清单**检查整个 ZIP 的 SHA-256，校验脚本/EXE 的 Authenticode 及预期发布者，或另行使用预置信任公钥。相同服务器上未签名的哈希只能检查一致性，不能单独建立发布者身份；首次安装脚本本身也需可信分发与校验。拒绝 `iwr | iex`、全局修改执行策略或遇到失败就切换 HTTP。[Get-FileHash](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/get-filehash?view=powershell-7.5)、[Get-AuthenticodeSignature](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.security/get-authenticodesignature?view=powershell-7.5)
- 安全解压必须校验路径仍在安装临时目录内，拒绝绝对路径、目录穿越和可逃逸的链接；验证失败删除本次临时文件即可，保留当前指针与旧包。
- 首次启动新客户端在隔离健康检查中确认入口、UI 与回退通道，检查本身不得自动新建终端或执行模型请求。通过后才确认新版本；启动失败恢复上一可用指针并给出错误。保留失败版本记录，避免反复自动安装同一个坏包。
- 回退完整程序不等于回退用户状态或后台进程；配置迁移必须向后兼容或有事务备份。UI 的签名序列保护不能因原生回退而清零；不自动倒退 WebView2 系统运行时。
- 卸载默认只移除本应用拥有的启动入口、登记和未被占用的运行文件，保留配置、凭据、UI 备份与后台终端。被后台占用的旧运行目录留待进程自然结束后再清理，不注册会在下次重启误删用户目录的宽泛删除任务。删除数据应是单独、明确的选择，外部 Token 原文件始终不自动删除。

签名不等于保证不出现 SmartScreen 提示，管理员策略也不应由安装器绕过。下载安装失败时仍能启动旧版本，这是首发验收条件。

## 实施顺序与验收

1. **统一产物契约。** 单一数字版本来源，修正 Python 元数据差异；固定 x64 首发范围、完整 ZIP 根结构、全部文件清单和发行说明，明确系统依赖与 UI/后台更新边界。先测实际 ZIP 体积，不沿用本报告的解压体积作为下载量。
2. **用户级安装与稳定入口。** 新增安装/卸载脚本、版本目录与原子指针、开始菜单/可选桌面快捷方式、脱敏 `status`；保留现有命令语义、状态路径与已运行后台。此阶段先用本地隔离夹具验证，不改用户实际 daemon。
3. **首次连接引导。** 复用简单 URL/端口/Token 与授权目录，区分查看远端/共享本机，明确保存范围；空目录、Token 撤销、重名房间与既有后台均有诊断，不默认开放入站监听。
4. **原生升级与回退。** 为 ZIP/清单建立可信签名和 HTTPS 下载，补充版本生命周期及失败回退；明确后端维护入口。复用当前 UI 菜单入口但分清更新类型。只有通过以下验收才公开“自动更新完整程序”。
5. **统一发布命令。** 在现有 Windows 构建之后增加可重入的打包、扫描、签名、上传、回读校验；UI 与完整程序分别发布，发现指针最后提交。构建工具或代码签名服务的新增选择单列，不在本次评估中安装。
6. **接入 winget。** 相同安装器/ZIP 通过验证后生成清单并提交；先测试 `winget validate`、隔离安装/升级/卸载，再宣布真实 ID。npm 只在已有 Node 用户确有需求时增设适配层，复用同一签名包和安装逻辑。

| 验收场景 | 必须提供的证据 |
| --- | --- |
| 干净 Windows 11 x64 标准用户，无 Python/Node，Windows 10 支持范围另行明确 | 无管理员提示安装应用、菜单和 CLI 能启动；缺失 WebView2/.NET 时能清晰诊断或使用浏览器入口 |
| 中文/空格用户名，重复安装，受管设备禁止 VBS/下载，离线或代理失败 | 路径与参数正确；重复安装幂等；不改全局策略、不半安装、不损坏旧版本 |
| 两台实际 Windows 开发机 + 同一授权中继，多个 URL/Token 配置 | 各自主机/房间身份正确，全部已授权窗口可见；跨 Token 不越权，Token 不出现在包/日志/快捷方式 |
| 升级时存在正在运行的本机及远端 PTY | 记录后台 PID/会话身份前后一致，无 input/close/resize 帧；旧后台仍运行且版本显示准确 |
| 完整包坏签名、缺文件、路径穿越、断电模拟、新客户端抛错或卡死 | 下载拒绝或原子恢复旧指针；旧客户端能开；失败版本隔离，配置/草稿/Token 保留 |
| UI 更新与完整程序升级交叉、旧后台兼容和不兼容 | 区分版本来源，UI 序列保护有效；新旧协议协商明确，不能以“页面已更新”掩盖后端仍旧 |
| 卸载/重装与旧 Token 位于版本目录 | 默认保留状态与活跃终端，只有本应用拥有的文件被清理；Token 外部文件不删除；重装能恢复授权配置 |
| winget/可选 npm 渠道 | 可追溯固定版本下载，安装/升级/卸载均与手动安装一致；npm 依赖被跳过时明确报缺包 |

以上为待执行验收清单。本次只核对现有文件、签名状态与官方机制；没有安装器实装、干净虚拟机、多 Windows 主机安装升级、原生完整包回退或 MSIX 兼容测试结果。
