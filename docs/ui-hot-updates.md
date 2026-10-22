# 桌面与手机界面热更新

本轮实现桌面和 Android 共用的签名界面更新渠道。首次需要安装包含更新能力的 Windows 外壳与 APK；之后兼容 Native API 1 的 HTML、CSS、JavaScript 与配套 vendor 更新不必重新打包外壳。Java/Python 原生能力、后台协议或签名信任锚变化仍需正常升级相应组件，不能通过页面包替换。

Windows 外壳 `dist/windows-ui-updates/` 与 Android 0.2.14(16) `dist/android-ui-updates/SuperMonoAgent.apk` 已构建；本地签名包为 `.omx/ui-releases/stable/releases/ui-20261022-bootstrap/`。公网目录、证书及首次发布尚未执行，缺少服务器管理连接。不要把本地签名测试包当成已上线更新。

## 用户操作

客户端在启动以及前台可见时每六小时检查一次。发现新版先提示，不会在输入中直接重载。顶部的“检查更新”可手动查看版本与状态；手机也可从原生连接设置进入。

点击“下载并更新”后，先下载并校验完整包，再保存当前界面备份和导航/草稿状态，最后激活并重载。输入法正在组合时会延后激活；后台终端进程不结束。菜单中的“恢复上一版本”也先保存当前界面状态，再恢复本机旧版本。

新界面必须在 20 秒启动窗口内完成本地初始化与首次渲染确认；不要求 daemon 或公网登录在线。未确认、启动脚本致命错误、未完成启动的进程重启会回退。Windows 外壳还独立监测挂死的 WebView2，并通过原生控制恢复；更新页面的脚本不是唯一恢复途径。

Android API 29+ 可以终止本应用挂死的 renderer 后重建 WebView；API 26～28 仅有原生导航/重建的最佳努力能力，不声称已在这些旧系统实测强制中断。健康更新复用 WebView，保留页面私有 sessionStorage；renderer 死亡后重建可能需要重新登录。草稿迁移不复制账号凭据。

如果旧的 USB 开发界面没有新的安全状态导出接口，客户端会停止激活，避免悄悄丢失闭包中的草稿。原生“使用安装包自带界面”提供明确恢复入口，并要求先保存旧界面各窗口草稿再确认；旧 USB 文件保留，不自动删除。新的引导界面提供完整迁移接口。

## 开发者的一条命令

仅本地打包、签名并验证：

```powershell
.venv/Scripts/python.exe scripts/package-ui-update.py --revision ui-20261022-example
```

服务器 SSH 与 HTTPS 配好以后，一条命令完成打包和正式发布：

```powershell
.venv/Scripts/python.exe scripts/publish-ui-update.py --publish `
  --ssh-target publisher@117.50.48.22 --ssh-port 22 --ssh-key C:/secure/id_ed25519
```

不提供固定 revision 时自动生成新的不可变编号。也支持 `--build-only`，或 `--stage-only --ssh-target ...` 先上传校验、不切换频道。要激活已经生成的同一版本，增加 `--release .omx/ui-releases/stable/releases/版本号`。同 revision 只有完全相同的三个公共文件才能重用，不能覆盖成另一套内容。

输出位于 `.omx/ui-releases/stable/releases/版本号/`：`desktop.zip`、`android.zip`、签名 `manifest.json`。根目录 `manifest.json` 指向最后完成的本地频道版本。发布会先验证远端文件 SHA-256，最后原子替换频道 manifest；旧版本与旧 manifest 保留。构建及发布使用 OS 锁，进程死亡不会留下永久占用的标记锁。

当前使用的开发发布私钥为 `.omx/update-signing/development-private.pem`，权限限当前用户和 SYSTEM；公钥 ID 为 `c728238ab3ebabab`。请在开发者控制的安全位置另作备份。打包命令不会默默生成或替换密钥；丢失私钥后，已有外壳不会接受新密钥，需要正常更新信任锚。私钥、SSH 密钥、账号库、Token 都不会随界面包上传。

## 更新渠道与完整性

默认频道为 `https://117.50.48.22/updates/stable/manifest.json`。更新流独立于终端账号连接，不发送账号 Authorization、Cookie 或系统代理认证信息；使用默认 TLS/主机名校验，同源重定向和明确超时/大小限制。

签名信封包含原始 JSON payload 的 Base64 和 RSA PKCS#1 v1.5 SHA-256 签名。Windows 使用系统 CNG，Android 使用系统 Java 安全提供者；没有新增 Python 运行时依赖。可信公钥只在首次外壳包中提供，不属于热更新白名单。

每个平台是一个完整快照：桌面 11 个文件，手机 10 个文件。校验签名、归档 hash、每个文件的大小/hash、平台与 Native API 后才安装。单文件最多 4MiB、展开快照最多 5MiB、下载归档最多 8MiB；路径越界、重复条目、大小写冲突、符号链接、缺失或额外文件均拒绝。

已观察的最高发布序号不因回滚降低；相同序号不能更换签名内容。失败版本被隔离，避免自动反复安装。首次更新会实体保存内置/当前有效旧界面；之后保留旧的已验证快照。旧页面所有资源固定到自己的 revision，不混用新版本文件。历史已验证版本即使频道签名时间自然过期，仍可在本机恢复；网络安装的新包必须在签名有效期内。

默认新 manifest 有效 30 天；长期没有开发时，要继续让新安装客户端获取频道包，可以用同一打包发布命令生成新的 revision/序号。过期或离线不会阻止本机当前可用界面运行。

## 中继的目录

`deploy/relay/Caddyfile` 在 HTTPS 站点下增加 `/updates/*`，对应 `/var/lib/supermono-updates`。频道 manifest 不长期缓存，版本目录采用 immutable 缓存；暂存目录、锁文件和目录列表不对外暴露。明文 bootstrap 站点仅用于 ACME 验证，不分发更新。

发布账号需要该更新目录的写权限，Caddy 仅需读取公共文件；签名私钥留在开发电脑。账号中继、TLS 和 SSH 的部署前提见 [统一中继账号配置](relay-account-setup.md)。当前 443 证书不覆盖公网 IP，新客户端会拒绝该连接，不能通过取消证书校验来伪造更新可用。

## 验证与限制

证据位于 `.omx/artifacts/ui-updates/`，包括共享签名向量、publisher 实际签名/传输失败/锁进程死亡恢复、Windows 双进程存储、真实 WebView2 无页面桥参与的挂死恢复，以及 Android JVM 的签名/HTTPS/归档/回退与页面状态测试。

浏览器模式没有控制用户外部浏览器进程的原生窗口权限；普通脚本失败可自动回退重载，极端 renderer 挂死时本机存储仍会恢复，可能需要用户刷新该浏览器标签页。真正 Windows 桌面外壳具备独立的原生恢复控制。

Linux systemd 与证书 hook 只做了静态审查，Caddy 配置已在本机隔离 HTTPS/WSS 服务上实际验证。当前没有生产 SSH 上传、公开证书签发或正式频道发布的证据；最终首次外壳构建、真机安装状态以交付记录为准。

Windows 实际包已通过账号存储、绑定授权、电脑清单自动填充和隔离 CMD 输入验证（34 个源文件 hash 一致），内置图标、公钥和 native bridge 与源码逐字节一致。双平台最终 ZIP 已通过 Windows CNG 验签及完整归档校验。APK 签名验证通过；手机当前未连接，尚未覆盖安装。

本轮全量 Python 回归为 235 passed、1 skipped、42 subtests passed；Node 为 33 passed。全量运行中的一处隔离测试线程清理告警已通过测试清理顺序修正，修正后全部 6 种发送方式及连续 3 次 send 在“线程告警视为错误”模式下通过。Codex 发送测试的早期失败已复现为命令接受后的退出收尾超过原 5 秒，而非遗漏 Enter；测试分别验证关闭确认与进程退出，仍要求只发送一次 Enter。相关证据在 `.omx/artifacts/android-composer-submit/`。
