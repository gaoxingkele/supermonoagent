# 默认中继、统一账号与自动选择电脑

本轮用户已确认采用统一中继账号。默认地址选择、账号登录、授权电脑发现和自动填充已经实现；公网部署、证书签发和真实账号初始化还需要服务器管理连接。本页不把隔离测试通过等同于现有公网服务已经升级。

## 使用流程

Windows 构建产物为 `dist/windows-relay-accounts/`：双击 `02-Orca-Workspace.vbs` 打开桌面，或 `01-Browser.vbs` 打开浏览器。它保留此前状态灯和多主机功能。

1. Windows 右上角连接状态可打开连接窗口。“中继地址”和增加电脑窗口的地址框都可下拉选择默认地址，也可手填。集中默认值位于 `src/supermonoagent/defaults.py`，环境变量 `SUPERMONO_DEFAULT_RELAY` 可覆盖。已有本机连接不因显示建议地址而自动切换。
2. 在“统一中继账号”区域填写已配置的 HTTPS/WSS 地址、邮箱及密码，选择“登录并发现电脑”。也可从左上角增加电脑窗口进入登录。
3. 登录后，左上角“增加工作区”的清单自动列出该账号获授权且在线的电脑。选中一项，电脑名、中继地址及端口、房间号和掩码授权凭据自动填写。选择“保存并连接”即可打开终端。
4. 账号授权和手填电脑 Token 是独立连接方式。账号模式不需要逐台复制长期 Token；修改目标会清掉旧授权，注销或会话到期会停止对应账号连接。手填方式继续可用。

账号登录态只放在当前浏览器会话的 `sessionStorage`，按规范化中继地址绑定；密码不保存。电脑记录只保存连接方式，不持久保存账号 session。账号 session 不发给 daemon；中继通过受信主机链路请求委托授权，daemon 验证本机绑定后才接受终端连接。

## 为什么需要首次登记与绑定

统一账号和每台电脑原有的终端所有者是两层权限。中继管理员授予“可以访问此房间”，主机管理员再明确授权“这个账号可以操作本机哪个所有者的终端”。不会因为邮箱相同就接管其他人的旧窗口。新账号没有自动获得全部电脑的权限。

旧的长期 Token、房间所有权 secret 和新的登录 session 彼此独立：owner secret 只用于主机登记，登录 session 只用于中继认证，两者都不能作为直接 daemon 的长期 Token。

## 中继管理员配置

以下命令是准备好的部署步骤，尚未在 `117.50.48.22` 执行。示例以 Linux、`/opt/supermonoagent/.venv`、服务账号 `supermono-relay` 为前提；实际用户名、目录与已有服务应先核实。

```bash
# 首次登记对应房间；输出的 owner secret 安全交给该主机管理员，不发给终端用户。
sudo -u supermono-relay /opt/supermonoagent/.venv/bin/supermono-relay \
  --enrollment-file /var/lib/supermono-relay/owners.json --enroll ROOM

# 创建统一账号；密码由 getpass 在本机终端输入，不写在命令行中。
sudo -u supermono-relay /opt/supermonoagent/.venv/bin/supermono-relay \
  --identity-file /var/lib/supermono-relay/accounts.sqlite3 --create-account owner@example.com

# 返回的稳定账号 ID 用于后面的主机本地绑定。
sudo -u supermono-relay /opt/supermonoagent/.venv/bin/supermono-relay \
  --identity-file /var/lib/supermono-relay/accounts.sqlite3 \
  --enrollment-file /var/lib/supermono-relay/owners.json \
  --grant-room ROOM --account owner@example.com
```

撤销使用 `--revoke-room ROOM --account EMAIL_OR_ID`；重置密码使用 `--reset-account-password EMAIL_OR_ID`，同样通过交互输入或 `--password-stdin`，没有密码命令行参数。账号、授权、登录会话摘要保存在 SQLite 中；主机所有权验证文件仍单独保存。运行中的中继会观察外部 CLI 的权限变化并撤销相关连接。

## 每台 Windows 主机的明确绑定

```powershell
# 用部署后的 WSS 地址、该主机房间号和稳定账号 ID。此配置命令保存后退出，不启动/终止 PTY。
.\SuperMonoAgent.exe host --state-dir "本机已有状态目录" `
  --relay wss://117.50.48.22/ws --code ROOM --bind-relay-account ACCOUNT_ID
```

默认绑定到本机主 Token 对应的终端所有者。要绑定已有的特定本地账号，增加 `--relay-local-account EMAIL`。移除使用 `--unbind-relay-account ACCOUNT_ID`，仍需明确 relay 和 room。绑定保存到该状态目录的 `relay-bindings.json`，新版 daemon 在运行时观察变更，不需要为修改绑定而重启终端。

主机上联仍需要对应房间登记的独立 owner secret。旧二进制 daemon 没有新的委托授权能力，不能只更新 HTML 就假装完成后台迁移。现有旧实例和终端应保留原连接，选择合适时机切换具备新能力的主机端；本轮没有擅自结束用户现有进程。

## 为现有公网 IP 增加 HTTPS/WSS

当前实测 `http://117.50.48.22:8817/health` 可达，HTTPS 同端口不支持 TLS。另查默认 443 已有 HTTPS 服务，但证书不覆盖这个 IP，默认验证报 IP address mismatch；不能当作可用中继入口，也不能在未核实既有反代的情况下直接抢占 443 或覆盖配置。账号密码和账号 session 在公网 `ws://` 上传输会被客户端及新版服务端拒绝。

Let’s Encrypt 已支持公网 IP 证书，使用 `shortlived` 配置，有效期 160 小时；Certbot 至少需要 5.4 的 IP webroot 支持。HTTP-01 验证必须能从公网访问 TCP 80；客户端加密入口使用 443。[IP 证书官方说明](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)、[Certbot IP 操作说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)、[ACME 验证端口](https://letsencrypt.org/docs/challenge-types/)。

准备好的文件位于 `deploy/relay/`：

- `Caddyfile.bootstrap`：只提供 HTTP-01 challenge 文件。
- `Caddyfile`：HTTPS/WSS 443 → loopback 8818，并保留 challenge 路径；不会覆盖旧 8817 端口。
- `supermono-relay.service`：新版账号中继单独监听 loopback 8818，指定稳定账号库、public URL、Host/Origin 与可信 TLS 反代。
- `supermono-cert-deploy`：验证证书 IP、余期及公私钥匹配；版本目录保存完整证书对，原子切换 `current`，配置校验/重载失败会回滚。
- `supermono-certbot-renew.service/.timer`：没有现成续期任务时，每天两次检查。已有 Certbot 自动续期任务时应整合本证书 hook，避免重复运行，不要停止维护其他站点的任务。

可用 `python scripts/package-relay-deployment.py` 生成 `dist/relay-account-deployment.zip`，包含 relay 源码、配置模板、验收脚本和文件哈希清单，不打包本机账户库、Token、owner secret、私钥或测试证书。该文件是部署输入，解压后仍需按实际 Linux 环境创建虚拟环境并安装项目。

需要先核实服务器系统、80/443/8818 端口占用、已有反代和服务配置。创建服务账号与 `/var/lib/supermono-relay` 私有可写目录；Caddy 用户只获得证书版本目录读取权。部署模板按 Linux systemd/GNU 工具准备，不能直接当作 Windows 安装命令。

先合并 challenge 配置到现有 Caddy 站点，不要直接覆盖其他站点配置。证书签发示例：

```bash
# 先 dry-run，验证公网80挑战可达；不保存 staging 证书到正式路径。
certbot certonly --dry-run --webroot -w /var/lib/supermono-acme \
  --cert-name supermono-relay-ip --ip-address 117.50.48.22 \
  --preferred-profile shortlived

# 正式签发去掉 --dry-run；ACME 账号及联系邮箱按服务器已有配置或管理员提示设置。
# 将 supermono-cert-deploy 安装到 /usr/local/sbin 后，首次以正式 lineage 执行一次，
# 再合并正式 Caddyfile；后续由 Certbot deploy hook 在成功续期后调用。
RENEWED_LINEAGE=/etc/letsencrypt/live/supermono-relay-ip /usr/local/sbin/supermono-cert-deploy
```

首次执行 hook 前 `/etc/caddy/Caddyfile` 应是可验证的 bootstrap/已有配置；首次证书落位后再切入正式站点。手动证书文件变化需要强制重载 Caddy；重载可能让 WebSocket 重连，配置延迟关闭以减小影响，不能承诺全程不断连接。[Caddy reload](https://caddyserver.com/docs/command-line#caddy-reload)、[WebSocket 重载行为](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming)、[Certbot 续期](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)。

TLS 成功后，把 Windows 启动环境中的 `SUPERMONO_DEFAULT_RELAY` 配为 `wss://117.50.48.22/ws`；同步更新各主机上联地址。待所有旧连接迁移并验证后，再另行关闭公网明文 8817。当前默认的旧 WS 地址仍作为已知建议保留，不会悄悄把账号密码发过去。

## 验收与当前部署边界

```powershell
.venv/Scripts/python.exe scripts/check-relay-tls.py https://117.50.48.22
```

这个只读检查不需要用户密码：验证公信 TLS、证书余期、health 不泄露目录/凭据、WSS 要求账号认证、恶意 Origin 被拒绝。没有跳过证书校验的参数；`--ca-file` 只用于显式测试/私有根信任。

已完成隔离验证：统一登录、按账号过滤目录、选择电脑自动填充、委托授权至现有 FakePTY、撤销/注销/过期/错误绑定、跨地址凭据隔离；另有真实 Caddy TLSfixture → 新版中继的登录和目录测试。证据位于 `.omx/artifacts/relay-accounts/`。

本机没有 Linux/systemd，因此 hook 和 systemd 未声称真实服务器执行通过；Caddy 模板已使用官方校验过的二进制实际 adapt/validate 并做隔离反代验证。公网服务器尚未部署或签发证书；仍需要可用 SSH/管理入口及初始化账号、房间登记和主机绑定。
