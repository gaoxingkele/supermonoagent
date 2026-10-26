# Windows 一键安装与默认配置

当前完整安装包版本：**0.2.39，Windows x64**。压缩后约 17 MB，包含完整 Python/终端运行时和百余个程序文件，不需要先装 Node.js 或 Python。

## 使用专用包

1. 打开会话提供的专用 HTTPS 下载链接，保存 ZIP。
2. 全部解压，双击根目录的 `Install.cmd`。
3. 安装完成后使用唯一的 `SuperMonoAgent` 桌面快捷方式。

专用包自动注入默认中继 `wss://117.50.48.22/ws` 与指定的默认 Token。新电脑没有保存房间号时，后台启动会读取**目标电脑自己的名称**作为房间号，不使用打包电脑的名称。同一 Token 获授权的其它房间及窗口会按现有客户端发现流程列出。

安装目标为 `%LOCALAPPDATA%\Programs\SuperMonoAgent`，配置和私有 Token 保存在 `%USERPROFILE%\.supermonoagent`。无需管理员权限；Token 不进入快捷方式、进程参数或安装输出。已有 Token、外部 Token 指针、房间号和运行中的后台均保留，安装器不会终止终端或为现有主窗口再开一个窗口。

专用下载凭据从链接 fragment 在浏览器中读取，通过同源 Bearer 请求取包，不写入 HTTP 访问日志或 Git。默认有效期七天，可用于用户授权的多台电脑；链接过期后需重新签发。ZIP 中有私有默认配置，专用包只存于私有下载目录，不能把它作为公开发布资产。

## 通用无凭据包

[下载通用完整安装包](https://117.50.48.22/updates/installers/0.2.39/SuperMonoAgent-0.2.39-Setup.zip)。它不包含私有默认 Token；安装时优先保留本机既有凭据，没有凭据才以隐藏输入提示填写。要实现上述免输入配置，应使用会话里的专用链接。

安装前验证内置固定 RSA 公钥的签名目录、四个安装/启动脚本、每个运行文件及私有配置摘要，再写入版本目录。首次下载安装的信任起点是本机认可的 HTTPS 下载通道；本交付不宣称 Windows Authenticode 代码签名。安装器不修改全局执行策略，不提权，不关闭证书校验或系统防护。

已有主窗口会被保留，安装器尝试激活它，并显示安装完成说明；正常关闭后再从新快捷方式打开可使用已安装程序。已有旧后台不会自动重启；文件接收和项目账本等新后台功能在正常换用新版后台后生效。缺少可用 WebView2/.NET 桌面运行时会使用现有浏览器入口，不静默安装系统依赖。

0.2.37 将新建、排列和最大化移到顶部，标签行提供左右滚动；新增 mylib 本地建议、目标项目账本只读查询和文件粘贴入口。侧栏与文件界面包含在现有签名 UI 资源中，后续页面修改可通过 UI 频道更新。新装客户端不会把低于内置数字版本的 UI 当作更新。

0.2.38 接通 API／本机 Codex 两种助手文本入口、独立窗口对话、显式附带上下文及取消；详情见 [助手配置](assistant-models.md)。密钥仅本次进程保存。自有假模型的真实原生桥接与 CLI 测试通过，未调用用户真实账号或云模型。新建、排列、最大化直接位于顶部页面结构，页签行仅保留页签及左右滚动按钮。

0.2.39 增加项目账本建议、独立发送账本开关和重连后自动恢复；完整包的后台可读取额外两页。0.2.38 桌面外壳可只更新界面，现有三页后台仍可降级使用。实验登记和受保护规则准入尚未接入，文件接收仍需要目标电脑新版后台。

文件功能与验收边界见 [文件粘贴](clipboard-files.md)：真实 WebView 桥接及打包后台已验证，系统剪贴板读取仍未完成实测；当前受限测试环境拒绝打开剪贴板，未覆盖用户内容。目标后台仍需支持文件接收和项目账本能力。

## 安装与维护布局

### 自动检查与下载完整程序

安装版每次启动会启动**无控制台**检查：原生主窗口存在时每 15 分钟检查同一中继的完整程序频道 `https://117.50.48.22/updates/windows/stable/manifest.json`；浏览器后备模式每次启动检查一次并可提醒。同一用户/安装目录只执行一个活跃检查，主窗口关闭后结束，不创建计划任务或开机启动项，也不弹出日志窗口。

发现更高数字版本时，先验证 RSA 签名、时间与防回退序号，再自动从 `/updates/installers/<版本>/` 下载无 Token 的公开完整包，验证大小/整个 ZIP hash/安装目录与所有文件签名，才提醒用户确认安装。选择暂不更新后，同版本 24 小时内不重复提醒。

确认安装后保留 Token、房间、当前主窗口和后台终端，更新版本指针；当前窗口继续运行，下次正常打开客户端使用新版本。活动后台的升级需在会话可以正常结束后进行，更新器不会为换版本杀掉 PTY。下载失败或校验失败不执行包内脚本。界面热更新与完整程序更新是独立频道。

手动检查或诊断可使用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\SuperMonoAgent\Update.ps1" -CheckOnce
# 仅检查、下载验证，不弹窗或安装：
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\SuperMonoAgent\Update.ps1" -CheckOnce -NoPrompt
```

```text
%LOCALAPPDATA%\Programs\SuperMonoAgent\
  versions\0.2.36\...完整运行文件...
  Launch.ps1
  Uninstall.ps1
  Install.ps1                 # 已安装的可信验证器
  Update.ps1                  # 完整程序更新
  current.json
  previous.json                # 更新已有版本时保留
  config.json                 # 只有路径和中继地址，不含 Token 值
%USERPROFILE%\.supermonoagent\
  token.txt                   # 新注入时仅当前用户与 SYSTEM 可读写
  token-source.json
  relay-code                  # 已有值保留；新后台创建本机名
```

卸载入口登记在当前用户的应用列表。卸载保留用户状态、外部凭据与运行中的版本，不结束任何进程；小型卸载支持脚本可保留供后续清理。`previous.json` 不是已经完成的原生启动失败自动回退功能，UI 热更新回退仍由现有独立机制负责。

管理员隔离验证可运行 `Install.ps1 -InstallRoot ... -StateDir ... -NoLaunch -NoShortcuts -NoRegistry`；普通安装直接双击 `Install.cmd` 即可。

## 开发者打包

通用包使用当前完整运行目录，输出可公开提交：

```powershell
.venv/Scripts/python.exe scripts/package-windows-installer.py `
  --runtime dist/windows/0.2.39 `
  --output dist/windows/SuperMonoAgent-0.2.39-Setup.zip
```

私包额外传 `--token-file`，输出路径被限制在不受 Git 管理的 `.omx` 下。默认 Token 只在本地私密目录和服务端受限 ZIP 中存在；通用包、源代码和公开下载目录不含该文件。发布者使用现有私有签名密钥，工具不会生成或替换密钥。

开发者可用以下命令完成公开完整包与更新发现清单的打包和发布（私有初始配置包另行受限交付，不用于自动更新）：

```powershell
python scripts/publish-windows-release.py --runtime dist/windows/0.2.39 --publish --ssh-target publisher@117.50.48.22 --ssh-key C:/private/publisher-key
```

上传先验证完整包不可变内容，最后在 OS 锁内原子切换 `/updates/windows/stable/manifest.json`。发布者签名私钥留在开发电脑，不上传服务器。源码实现：`scripts/windows-installer/`、`scripts/package-windows-installer.py`、`scripts/package-windows-update-feed.py`、`scripts/publish-windows-release.py`、`deploy/downloads/`。安装与自动更新使用临时目录、签名 fixture 和模拟提醒验证，不以测试替换真实用户程序或结束现有终端。
