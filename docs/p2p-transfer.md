# 点对点目录传输（sma-p2p）设计

状态：独立进程，未接入桌面。现有 daemon、relay、桌面页面、Android 都不改。测通后再在桌面加一个入口打开它。

## 目标

1. 在本机选一个目录（或单个文件），整目录发到另一台电脑。
2. 列出网络里的其他电脑，选中后浏览对方的目录，选目标位置。
3. 断点续传；多条连接并行传输。
4. 信令走公网服务器，数据尽量点对点直连；打不通再经服务器转发，转发的也是端到端密文。

## 组成

```text
电脑 A: python -m supermonoagent.p2p          电脑 B: 同左
  ├─ 本机页面 http://127.0.0.1:6795/           ├─ 本机页面
  ├─ 对等端口 TCP 6796（入站直连）              ├─ 对等端口 TCP 6796
  └─ 控制连接 ──┐                         ┌──── 控制连接
                ▼                         ▼
        信令服务 rendezvous（117.50.48.22:8827 TCP）
        - 在线名单（按网络号分组）
        - 转发信令（候选地址）
        - 观测公网地址（打洞用）
        - 回退转发：把两条 TCP 拼起来，只搬密文
```

| 文件 | 职责 |
|---|---|
| `src/supermonoagent/p2p/rendezvous.py` | 信令服务。只用标准库，兼容服务器上的 Python 3.10 |
| `src/supermonoagent/p2p/agent.py` | 每台电脑一个。连信令、收发候选、建对等连接、对端 RPC |
| `src/supermonoagent/p2p/secure.py` | TLS-PSK 上下文、帧格式 |
| `src/supermonoagent/p2p/netinfo.py` | 本机候选地址（局域网 IPv4、公网 IPv6） |
| `src/supermonoagent/p2p/files.py` | 路径安全、目录清单、根目录限制 |
| `src/supermonoagent/p2p/transfer.py` | 分块计划、发送端并行、接收端写盘与续传日志 |
| `src/supermonoagent/p2p/web.py` + `static/p2p.html` | 本机页面与 JSON/SSE 接口 |
| `src/supermonoagent/p2p/__main__.py` | 命令行入口 |

不依赖第三方包。`websockets` 也不用；信令是 TCP 上的 JSON 行。

## 网络与身份

- **网络密钥 K**：32 字节随机数，第一台电脑生成。其他电脑在页面里粘贴「加入码」就进同一网络。K 只在各台电脑之间人工传递，不发给服务器。
- **网络号**：`net = sha256("sma-p2p/net/" + K)` 的前 32 位十六进制。服务器只按网络号分组，看不到 K。
- **电脑号**：每台电脑首次启动生成 16 位十六进制，存在 `p2p/config.json`。显示名默认取本机名。
- 状态目录 `%USERPROFILE%\.supermonoagent\p2p\`：`config.json`（网络密钥、电脑号、名称、根目录、端口）、`local-token`（本机页面令牌）、`transfers.json`（发送记录，供续传）。

## 加密与认证

对等连接一律 TLS 1.2 PSK，标准库 `ssl.set_psk_*_callback`：

- 密码套件 `ECDHE-PSK-CHACHA20-POLY1305`（前向保密）优先，`PSK-AES256-GCM-SHA384` 备选。
- PSK = `HMAC-SHA256(K, "sma-p2p/psk")`，identity = 本端电脑号。握手成功即证明双方都持有 K。
- 握手后第一帧 `hello {peer, session, kind}`，对端检查电脑号与信令里声明的一致。
- 经服务器回退时，服务器只拼接两条 TCP，TLS 在两台电脑之间，服务器看不到明文。
- 信令里的候选地址带 `HMAC(K, …)`，服务器伪造不了候选。
- 本机页面只听 `127.0.0.1`，检查 `Host`，接口要求 `X-SMA-Token` 等于 `local-token`；令牌只放在页面 fragment 里传进去。

已知限制：到信令服务器的控制连接本身不加密，旁路能看到网络号、电脑名、IP。数据不受影响。

## 建立连接（谁连谁）

A 要和 B 建会话时：

1. A 生成 `session`，通过信令发 `connect {session, candidates, punch}` 给 B；B 回自己的候选。
2. **候选**按优先级：
   - `lan`：本机所有私有 IPv4 + 对等端口 6796
   - `ipv6`：公网 IPv6 + 6796
   - `public`：服务器观测到的公网 IPv4 + 6796（有端口映射或无 NAT 时可通）
   - `punch`：TCP 打洞。双方各从一个固定本地端口连信令服务器做 `probe`，拿到 NAT 映射后的公网地址；收到 `go` 后两边用同一本地端口（`SO_REUSEADDR`）互相反复 `connect` 4 秒，等同时打开
3. 双方**同时**拨对方所有候选（局域网先发，其他错开 150ms），也接受对方拨入。谁先完成 TLS + hello 就用谁，其余取消。
4. 3–5 秒都不通就走 `relay`：A 在控制连接上登记一次性 token，两边各连服务器 `pipe {token}`，服务器配对后拼接。
5. 数据连接沿用胜出的路线和方向，再开 N-1 条（默认 N=4，局域网或 IPv6 可调到 8）。

页面上每个传输都显示当前路线：局域网 / IPv6 / 公网直连 / 打洞 / 中转。

## 对等协议（TLS 内部）

帧：`uint32 LE 长度 | uint8 类型 | 负载`，控制帧最大 32 MiB，数据帧最大 8 MiB + 头。

| 类型 | 名称 | 负载 |
|---|---|---|
| 1 | JSON | `{id, op, ...}` 请求或 `{id, ok, ...}` 回复 |
| 2 | CHUNK | 头 `<II Q I 32s>`：transfer 序号、文件序号、偏移、长度、sha256；后跟数据 |
| 3 | ACK | `<I I Q B>`：transfer 序号、文件序号、偏移、状态（0 成功，1 校验失败，2 写盘失败） |

控制连接上的 RPC：

| op | 方向 | 说明 |
|---|---|---|
| `info` | 双向 | 名称、版本、根目录列表 |
| `list` | → 对端 | `{path}`，只允许根目录之内；返回子目录、文件、大小、修改时间 |
| `mkdir` | → 接收端 | 在目标位置新建文件夹 |
| `offer` | 发送端 → 接收端 | `{transfer, name, dest, chunk, files:[[rel,size,mtime_ns]...], dirs:[...]}`，清单 zlib 压缩 |
| `progress` | 接收端 → 发送端 | 周期汇报已落盘字节 |
| `finish` | 发送端 → 接收端 | 发完全部块后请求收尾 |
| `cancel` | 双向 | `{transfer, discard}` |

## 断点续传

- **传输号** = `sha256(发送端电脑号, 源绝对路径, 目标电脑号, 目标路径)` 的前 24 位。同一源发到同一目标，重发即续传。
- 接收端在 `<目标>\<目录名>\.sma-p2p\<传输号>\` 下保存：
  - `manifest.json`：清单、块大小
  - `chunks.log`：每落盘一块追加 12 字节记录（文件序号 + 块序号），写满一批 flush
  - 未完成的文件写在 `<文件>.sma-part`，先按大小 truncate
- `offer` 到达时接收端比对清单：同一文件大小与修改时间不变，就保留已完成块；变了就清掉重来。目标位置已有同名完成文件、大小和修改时间都对得上的，直接跳过（`skip_existing`，默认开）。回复 `need`：每个文件缺的块（区间编码）。
- 一个文件的块收齐：fsync、`.sma-part` 改名为正式名、设置修改时间，日志记文件完成。全部完成后删 `.sma-p2p\<传输号>`。
- 发送端把记录写进 `transfers.json`。连接断了、对方离线、本机重启后，页面显示「续传」，对方重新上线时自动重试（退避 2–60 秒）。
- 每块带 sha256，接收端校验失败回 ACK 1，发送端重发该块，同一块失败 3 次判传输失败。

## 并行与吞吐

- 块大小：直连 4 MiB；第一次连上就是中转时 1 MiB（慢线路上信令不被大块堵住）。块大小写进 `transfers.json`，续传沿用，换路线也不丢进度。
- 发送端一个块队列，N 个 worker，各占一条数据连接。在途窗口按字节计：直连每条 16 MiB，中转 2 MiB，块数上限 64，所以大量小文件不会每个都等一个往返。中转只开 1 条数据连接。
- 读文件和 sha256 在线程池里做（hashlib 释放 GIL）。
- 接收端数据连接用 `FrameReceiver`（`asyncio.BufferedProtocol`），TLS 直接解密进按帧大小分配的缓冲。用 `StreamReader` 收 4 MiB 帧时 `bytearray.extend` 占了一半时间，换掉后吞吐翻倍。
- 校验和写盘进线程池；同一文件的写入加锁，打开的文件句柄 LRU 缓存 64 个。
- 回压：发送端字节窗口 + 接收端每连接最多 4 帧排队，超过就暂停读。

### 实测（2026-11-08，本机 NVMe，两个独立进程）

| 场景 | 结果 |
|---|---|
| 单条 TLS-PSK 连接，本机回环 | 266–321 MB/s |
| 局域网路线，2 GB 大文件 + 300 小文件 | 平均 179 MB/s，峰值 357 MB/s |
| 本机信令强制中转，512 MB | 平均 101 MB/s |
| 线上信令（117.50.48.22，经 SSH 隧道）强制中转，3 MB + 300 小文件 | 16.4 s（改按字节窗口前 182 s） |

线上中转慢是线路决定的：这台电脑在美国，到服务器 RTT 254 ms，上行 88 KB/s、下行 476 KB/s。中转只保证能传。

## 路径安全

- 清单只放相对路径，用 `/` 分隔。拒绝空段、`.`、`..`、盘符、绝对路径、NUL 与控制字符、Windows 保留名（CON、NUL、COM1…）、结尾的点或空格。
- 接收端把目标路径和每个相对路径拼起来后 `resolve()`，必须仍在目标目录内。
- `list`、`mkdir`、`offer` 的路径都必须在本机配置的根目录内。Windows 默认根目录是所有固定盘，可在 `config.json` 的 `roots` 改窄。持有网络密钥 = 完全信任，这是自己的几台电脑之间用的。
- 符号链接和交接点不跟随，扫描时跳过并计入「已跳过」。

## 信令服务协议

TCP 8827，每行一个 JSON，单行最大 64 KiB。连接首行决定角色：

| 首行 | 作用 |
|---|---|
| `{"op":"hello","net","peer","name","ver"}` | 控制连接。服务器回 `{"op":"welcome","addr":[ip,port]}`，之后推送 `peers` |
| `{"op":"probe"}` | 回 `{"op":"probe","addr":[ip,port]}`，连接保持到对方关闭（打洞期间保留 NAT 映射） |
| `{"op":"pipe","token"}` | 回退转发。token 必须先由同一网络的在线电脑用 `relay` 登记，60 秒内有效，恰好配对两条 |

控制连接上：`signal {to, data}` 原样转给同网络的目标电脑（带 `from`）；`relay {token}` 登记转发 token；`ping`/`pong` 保活（30 秒）。
限额：每个网络最多 64 台电脑，每个 IP 最多 32 条连接，同时最多 64 条转发，转发闲置 120 秒断开。

## 部署

- 服务器（已部署 2026-11-08）：`/home/others/sma-p2p/rendezvous.py`，systemd 单元 `sma-p2p`（DynamicUser，只读系统），监听 `0.0.0.0:8827`，firewalld 已放行 `8827/tcp`。用系统自带 python3.10，不建 venv。`sma-relay`、nginx 未改。
- **待办：云安全组还没放行 8827/tcp**，公网连接在到达网卡前被丢弃（tcpdump 0 包）。放行前只能经 SSH 隧道使用：`ssh -N -L 18828:127.0.0.1:8827 root@117.50.48.22`，再加 `--rendezvous 127.0.0.1:18828`。
- 每台电脑：`.\.venv\Scripts\python.exe -m supermonoagent.p2p`（需 Python 3.13+），默认连 `117.50.48.22:8827`，打印带令牌的本机页面地址。
- 入站：本机防火墙现有 python 规则是阻止。要被直连的电脑运行一次 `python -m supermonoagent.p2p --firewall`（弹 UAC），按端口放行 TCP 6796。两端都挡入站时只能靠打洞或中转。
- 更新服务器：`scp src/supermonoagent/p2p/rendezvous.py root@117.50.48.22:/home/others/sma-p2p/` 后 `chmod 644` 并 `systemctl restart sma-p2p`。重启会断开在线电脑，它们 1–30 秒内自动重连。

## 测试

- `tests/test_p2p_files.py`：路径校验、清单、根目录限制。
- `tests/test_p2p_transfer.py`：本机起信令服务 + 两个电脑进程（各自状态目录），传混合目录（空目录、大文件、大量小文件、中文名），比对 sha256；中途断开后续传，只补缺块；强制走中转；修改源文件后重传；路径穿越被拒绝。
- 真实网络：本机两个实例经线上信令（SSH 隧道）已通过，直连与强制中转都完成。**还没测**：两台不同网络的电脑之间的 IPv6 直连、TCP 打洞成功率。这需要第二台电脑，且服务器没有 IPv6，无法从外网测本机 IPv6 入站。

当前结果：`tests/test_p2p_files.py` 26 项、`tests/test_p2p_transfer.py` 6 项全部通过；本机页面 12 项（令牌、Host、Origin、根目录、SSE）通过。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_p2p_files.py tests/test_p2p_transfer.py -q
```

## 分期

1. 本期：上面全部，路线为局域网 / IPv6 / 公网直连 / TCP 打洞 / 中转。
2. 下期：UDP 打洞 + QUIC（对称 NAT 下的成功率更高）；限速；接入桌面菜单「传输文件…」打开本页面，并用当前窗口所在电脑预选源目录。
