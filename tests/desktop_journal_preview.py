"""Hold a synthetic desktop journal fixture for native browser acceptance."""
import asyncio
from playwright import async_api
from test_workspace_journal import fixture, install_journal


async def main():
    async with fixture(async_api) as owned:
        _browser, url, _endpoint, _trap, _messages, daemons, _ops, _windows, sessions = owned
        install_journal(daemons[0], sessions[0][0])
        print('SYNTHETIC_DESKTOP_JOURNAL_URL=' + url, flush=True)
        await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
