"""python -m supermonoagent.p2p  — run one transfer agent with its loopback page."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import webbrowser
from pathlib import Path

from .agent import Agent
from .web import WebUI


def default_state() -> Path:
    base = os.environ.get('SUPERMONO_STATE_DIR')
    return (Path(base) if base else Path.home() / '.supermonoagent') / 'p2p'


def add_firewall_rule(port: int) -> None:
    """Port-scoped inbound allow rule, so it holds whichever python.exe runs the agent."""
    import sys
    if sys.platform != 'win32':
        print('只在 Windows 上需要。')
        return
    if not 1 <= port <= 65535:
        raise SystemExit('端口无效')
    import ctypes
    name = f'SuperMonoAgent P2P TCP {port}'
    command = (f'/c netsh advfirewall firewall delete rule name="{name}" >nul 2>&1 & '
               f'netsh advfirewall firewall add rule name="{name}" dir=in action=allow protocol=TCP '
               f'localport={port} profile=private,public')
    code = ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'cmd.exe', command, None, 0)
    print('已请求管理员确认，添加规则：' + name if code > 32 else '没有取得管理员权限，规则未添加。')


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog='python -m supermonoagent.p2p', description='点对点目录传输')
    parser.add_argument('--state-dir', help='状态目录，默认 %%USERPROFILE%%\\.supermonoagent\\p2p')
    parser.add_argument('--rendezvous', help='信令服务 host:port，默认 117.50.48.22:8827')
    parser.add_argument('--web-port', type=int, default=6795, help='本机页面端口（只听 127.0.0.1）')
    parser.add_argument('--peer-port', type=int, help='对等直连端口，默认 6796')
    parser.add_argument('--name', help='本机显示名')
    parser.add_argument('--workers', type=int, help='并行数据连接数，默认 4')
    parser.add_argument('--join', metavar='CODE', help='用加入码加入另一台电脑的网络')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    parser.add_argument('--no-direct', action='store_true', help='测试用：不发布也不拨直连候选')
    parser.add_argument('--no-punch', action='store_true', help='测试用：不打洞')
    parser.add_argument('--force-relay', action='store_true', help='测试用：只走中转')
    parser.add_argument('--firewall', action='store_true',
                        help='添加 Windows 防火墙入站放行规则（TCP 对等端口），会弹出管理员确认，然后退出')
    args = parser.parse_args(argv)
    if args.firewall:
        add_firewall_rule(args.peer_port or 6796)
        return
    import ssl
    import sys
    if not hasattr(ssl.SSLContext, 'set_psk_client_callback'):
        parser.error(f'需要 Python 3.13 以上（TLS-PSK），当前是 {sys.version.split()[0]}')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')

    async def run():
        agent = Agent(args.state_dir or default_state(), rendezvous=args.rendezvous, peer_port=args.peer_port,
                      name=args.name, no_direct=args.no_direct, no_punch=args.no_punch,
                      force_relay=args.force_relay, workers=args.workers)
        if args.join:
            agent.join(args.join)
        await agent.start()
        web = WebUI(agent, args.web_port)
        await web.start()
        print(f'sma-p2p 已启动：{web.url}', flush=True)
        (agent.state / 'web-url').write_text(web.url, 'ascii')
        if not args.no_browser:
            webbrowser.open(web.url)
        try:
            await asyncio.Future()
        finally:
            await web.stop()
            await agent.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
