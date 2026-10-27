# 桌面与手机界面热更新

桌面和 Android 共用签名界面更新渠道。当前为 Native API 2，新增了六种 Agent 的独立 JSON 模板；首次需要安装支持 API 2 的 Windows 外壳与 APK。之后同 API 的页面、样式、脚本及模式配置更新不必重新打包外壳。原生能力、后台协议或签名信任锚变化仍需正常升级，不能通过页面包替换。

当前安装包使用数字版本目录，Windows `dist/windows/0.2.25/`，Android `dist/android/0.2.25/SuperMonoAgent.apk`。HTTPS 频道已上线；本轮发布和设备验证结果以 [TODO](TODO.md) 为准。早期 `windows-ui-updates`、`android-ui-updates` 与日期名称仅是历史构建，不是新版命名规则。

0.2.22 的 17/16 文件 API2 频道已发布，公网回读验签及归档校验见 `.omx/artifacts/ui-menu-profiles/publication-verified.json`。实际 Windows 新窗口已打开并使用新的内置快照，旧窗口和终端保留；Android 本轮没有连接设备，尚未覆盖安装。

## 用户操作

客户端在启动以及前台可见时每六小时检查一次。支持更新提示的中继还会轮询公开 stable manifest，并向在线桌面/Android 页面发送小型 `ui_update` 提醒；客户端随后调用原生更新服务，仍独立通过 HTTPS 下载并验签。提醒只打开检查/确认界面，不自动安装或重载。旧版页面不认识此提醒，只能等启动、六小时轮询或手动检查；Android 后台系统通知栏不属于热更新提示通道。Windows 左上角“菜单”位于侧栏折叠按钮右边、supermono 左边，提供“检查更新”“下载并更新”“连接工作区”；手机也可从原生连接设置进入。

点击“下载并更新”后，先下载并校验完整包，再保存当前界面备份和导航/草稿状态，最后激活并重载。输入法正在组合时会延后激活；后台终端进程不结束。菜单中的“恢复上一版本”也先保存当前界面状态，再恢复本机旧版本。

新界面必须在 20 秒启动窗口内完成本地初始化与首次渲染确认；不要求 daemon 或公网登录在线。未确认、启动脚本致命错误、未完成启动的进程重启会回退。Windows 外壳还独立监测挂死的 WebView2，并通过原生控制恢复；更新页面的脚本不是唯一恢复途径。

Android API 29+ 可以终止本应用挂死的 renderer 后重建 WebView；API 26～28 仅有原生导航/重建的最佳努力能力，不声称已在这些旧系统实测强制中断。健康更新复用 WebView，保留页面私有 sessionStorage；renderer 死亡后重建可能需要重新登录。草稿迁移不复制账号凭据。

如果旧的 USB 开发界面没有新的安全状态导出接口，客户端会停止激活，避免悄悄丢失闭包中的草稿。原生“使用安装包自带界面”提供明确恢复入口，并要求先保存旧界面各窗口草稿再确认；旧 USB 文件保留，不自动删除。新的引导界面提供完整迁移接口。

## 开发者的一条命令

仅本地打包、签名并验证：

```powershell
.venv/Scripts/python.exe scripts/package-ui-update.py --revision 0.2.39
```

服务器 SSH 与 HTTPS 配好以后，一条命令完成打包和正式发布：

```powershell
.venv/Scripts/python.exe scripts/publish-ui-update.py --publish `
  --revision 0.2.39 `
  --ssh-target publisher@117.50.48.22 --ssh-port 22 --ssh-key C:/secure/id_ed25519
```

本项目后续发布显式指定统一三段数字版本号，例如 `0.2.39`；工具保留旧编号的兼容解析，但本项目不再使用自动时间戳编号发布。也支持 `--build-only`，或 `--stage-only --ssh-target ...` 先上传校验、不切换频道。要激活已经生成的同一版本，增加 `--release .omx/ui-releases/stable/releases/版本号`。同 revision 只有完全相同的三个公共文件才能重用，不能覆盖成另一套内容。

### 复用 Android 基线仅用于本地构建或暂存

共享频道使用一个全局 revision。把公网旧 Android ZIP 原样放入新 revision，仍会让手机安装该旧快照，覆盖设备上较新的内置或本地 UI；0.2.39 复用 0.2.34 Android 后丢失 9／16 宫格就是这个回退。描述符和 ZIP 散列不变不能证明“不会影响 Android”。

因此，`--reuse-android-from` 自动启用的 `preserve_android`，以及直接传入的 `preserve_android=True`／`--preserve-android`，均禁止向共享生产根目录 `/var/lib/supermono-updates/stable` 正式 promotion。CLI 和发布 API 在任何 SSH 命令前拒绝。正式修复必须用当前 Desktop 与 Android 双端源码一起构建发布；真正的单平台发布需要未来独立的平台频道。

复用功能仅保留用于本地验签构建、`--stage-only` 暂存和明确隔离的自定义根目录。先将可信 `manifest.json`、`desktop.zip`、`android.zip` 保存为完整本地基线目录，本地构建示例：

```powershell
.venv/Scripts/python.exe scripts/publish-ui-update.py --build-only `
  --revision 0.2.40 --reuse-android-from .omx/ui-baseline
```

工具按现有公钥验证整个基线、有效期和双平台归档，并冻结输入；复用模式不读取本地 Android 源文件。隔离根目录及暂存仍保留发布锁内的 Android 描述符比较，以防并发发布改变基线。这项比较只防服务器上的并发变更，不能保护手机上较新的 UI。`--release` 不会自动识别历史复用包；不得将这类包改用普通发布参数绕过护栏。

默认完整双端构建与发布保持不变，固定保留桌面 17／手机 16 个资源及 API2。桌面 0.2.38 外壳具备模型桥接；新增两页账本读取能力仍需目标后台 0.2.39，旧后台的三页可以继续使用。

输出位于 `.omx/ui-releases/stable/releases/版本号/`：`desktop.zip`、`android.zip`、签名 `manifest.json`。根目录 `manifest.json` 指向最后完成的本地频道版本。发布会先验证远端文件 SHA-256，最后原子替换频道 manifest；旧版本与旧 manifest 保留。构建及发布使用 OS 锁，进程死亡不会留下永久占用的标记锁。

当前使用的开发发布私钥为 `.omx/update-signing/development-private.pem`，权限限当前用户和 SYSTEM；公钥 ID 为 `c728238ab3ebabab`。请在开发者控制的安全位置另作备份。打包命令不会默默生成或替换密钥；丢失私钥后，已有外壳不会接受新密钥，需要正常更新信任锚。私钥、SSH 密钥、账号库、Token 都不会随界面包上传。

## 更新渠道与完整性

默认频道为 `https://117.50.48.22/updates/stable/manifest.json`。更新流独立于终端账号连接，不发送账号 Authorization、Cookie 或系统代理认证信息；使用默认 TLS/主机名校验，同源重定向和明确超时/大小限制。

签名信封包含原始 JSON payload 的 Base64 和 RSA PKCS#1 v1.5 SHA-256 签名。Windows 使用系统 CNG，Android 使用系统 Java 安全提供者；没有新增 Python 运行时依赖。可信公钥只在首次外壳包中提供，不属于热更新白名单。

每个平台是一个完整快照：桌面 17 个文件，手机 16 个文件，各含本端六份独立 Agent JSON。校验签名、归档 hash、每个文件的大小/hash、平台与 Native API 后才安装。单文件最多 4MiB、展开快照最多 5MiB、下载归档最多 8MiB；路径越界、重复条目、大小写冲突、符号链接、缺失或额外文件均拒绝。

已观察的最高发布序号不因回滚降低；相同序号不能更换签名内容。失败版本被隔离，避免自动反复安装。首次更新会实体保存内置/当前有效旧界面；之后保留旧的已验证快照。旧页面所有资源固定到自己的 revision，不混用新版本文件。历史已验证版本即使频道签名时间自然过期，仍可在本机恢复；网络安装的新包必须在签名有效期内。

默认新 manifest 有效 30 天；长期没有开发时，要继续让新安装客户端获取频道包，可以用同一打包发布命令生成新的 revision/序号。过期或离线不会阻止本机当前可用界面运行。

## 中继的目录

`deploy/relay/Caddyfile` 在 HTTPS 站点下增加 `/updates/*`，对应 `/var/lib/supermono-updates`。频道 manifest 不长期缓存，版本目录采用 immutable 缓存；暂存目录、锁文件和目录列表不对外暴露。明文 bootstrap 站点仅用于 ACME 验证，不分发更新。

发布账号需要该更新目录的写权限，Web 服务仅读公共文件；签名私钥留在开发电脑。HTTPS 下载与默认信任链验签已经验证，见 `.omx/artifacts/remote-terminal-colors/publication-verified.json`。客户端始终校验证书，不提供跳过校验的更新路径。

## 验证与限制

证据位于 `.omx/artifacts/ui-updates/`，包括共享签名向量、publisher 实际签名/传输失败/锁进程死亡恢复、Windows 双进程存储、真实 WebView2 无页面桥参与的挂死恢复，以及 Android JVM 的签名/HTTPS/归档/回退与页面状态测试。

浏览器模式没有控制用户外部浏览器进程的原生窗口权限；普通脚本失败可自动回退重载，极端 renderer 挂死时本机存储仍会恢复，可能需要用户刷新该浏览器标签页。真正 Windows 桌面外壳具备独立的原生恢复控制。

早期部署记录中的“缺少 SSH/TLS/正式频道”已过时：HTTPS/WSS 与签名 stable 频道已部署，公开下载验签证据见 `.omx/artifacts/remote-terminal-colors/publication-verified.json`。生产证书续期 timer 也已实测启用。当前验收边界以 [TODO](TODO.md) 和 [运行时配置与回滚验收](../omx_wiki/runtime-config-and-recovery.md) 为准。

早期 Windows 包曾通过账号存储、绑定授权、电脑清单自动填充和隔离 CMD 输入验证（34 个源文件 hash 一致）；双平台 ZIP 通过 Windows CNG 验签及完整归档校验。后续手机已完成 API2 原生壳安装、正常更新及多主机验证；旧 QA 的异常回滚暴露了二次回退缺陷。修复已有 JVM 证据，修复版 QA 安装被系统终止，真机 throw/hang 验收尚未通过，不把正常更新成功等同于异常恢复成功。

本轮全量 Python 回归为 235 passed、1 skipped、42 subtests passed；Node 为 33 passed。全量运行中的一处隔离测试线程清理告警已通过测试清理顺序修正，修正后全部 6 种发送方式及连续 3 次 send 在“线程告警视为错误”模式下通过。Codex 发送测试的早期失败已复现为命令接受后的退出收尾超过原 5 秒，而非遗漏 Enter；测试分别验证关闭确认与进程退出，仍要求只发送一次 Enter。相关证据在 `.omx/artifacts/android-composer-submit/`。
