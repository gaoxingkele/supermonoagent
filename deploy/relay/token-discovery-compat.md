# 旧中继的同 Token 发现补丁

`token_discovery_compat.py` 供仍在运行旧版、未启用 owner enrollment 的中继过渡使用。它保留原终端/账号转发和旧主机认证模型，不声称提供现代中继的房间所有权保护。现代中继应使用项目内 `relay.py`，不要通过此适配器绕过 enrollment。

部署前保存原中继源码、启动命令和服务配置。把适配器放入服务可读目录，用包装入口导入旧模块后安装，再调用其原 `main()`：

```python
from supermonoagent import relay
from token_discovery_compat import install

install(relay)
relay.main()
```

服务必须只监听 loopback，并由 HTTPS 反向代理转发。默认适配器只接受来自 `127.0.0.1` / `::1` 且包含 `X-Forwarded-Proto:https` 的发现请求，或真实 TLS 连接；代理必须覆盖该请求头。`reverse_proxy_tls=False` 仅用于明确的 loopback 隔离测试。目录 Token 不得放在 URL 中。

适配器增加有界、同 Token 的在线终端目录，通知房间通过 host 首帧 `service:"notifications"` 排除。旧主机首次连接的机器名/项目名会被保存；身份和 Token 更新立即重新发布最新目录。它也阻止重复 host 在被拒绝前覆盖现有 Token，并在 Token 撤销时清除对应 phone 的队列、关闭该连接。`/health` 不再返回房间码。

适配器还回移植可选 `flow_control:1` 协商：32 帧硬上限不变，24/8 帧高低水位通知新版 daemon 暂停和恢复，暂停溢出后请求最新快照。未协商的旧 host 保持原队列行为。该回移植不启用 owner enrollment，也不改变旧房间认证。

启动包装入口需要短暂重启中继，终端主机的 PTY 进程不需要重启；客户端依原策略重连。先重启通知兼容桥以加载 `service` 字段，再重启中继。回滚时恢复原启动入口和原源码即可；补丁不写入 Token、目录或新的账号数据库。

中继还轮询公开 HTTPS stable manifest（默认每 30 秒），在版本变化后及新 phone 的 `ready` 后发送 `{"op":"ui_update","revision":"…","sequence":…}`。它只提示在线终端页面，不发往任务通知房间，也不占用终端帧队列。客户端必须独立下载、验签并由用户确认更新；中继提示不是信任根。`SUPERMONO_UPDATE_FEED` 可覆盖 manifest 地址，设为空可关闭。旧界面忽略该提示，仍按启动、可见时六小时轮询或手动检查发现更新。

验证使用已保存的旧版源码夹具 [legacy-relay.py](../../tests/fixtures/legacy-relay.py) 与 [test_relay_token_discovery_compat.py](../../tests/test_relay_token_discovery_compat.py)，覆盖同 Token 新房间、隐私、元数据变化、通知排除、Token 撤销、双向原始字节、重复主机及可信代理边界。公网仍需单独验证代理配置与现有连接恢复。
