"""Read-only TLS/WSS deployment gate. No account passwords or personal tokens required."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import ssl
from urllib.parse import urlsplit
from urllib.request import urlopen

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus


def read_health(url, context):
    with urlopen(url, context=context, timeout=8) as response:
        return json.load(response)


async def check(url, ca_file=None):
    target = urlsplit(url)
    if target.scheme != 'https' or not target.hostname or target.username or target.password or target.path not in ('', '/') or target.query or target.fragment:
        raise ValueError('Use an HTTPS base URL without credentials, query or path')
    context = ssl.create_default_context(cafile=ca_file)
    origin = f'https://{target.netloc}'
    health = await asyncio.to_thread(read_health, origin + '/health', context)
    if not isinstance(health, dict) or health.get('role') != 'relay' or health.get('ok') is not True:
        raise ValueError('The HTTPS endpoint is not a healthy relay')
    if any(key in health for key in ('codes', 'rooms', 'hosts', 'default_token', 'token', 'projects')):
        raise ValueError('The relay still exposes directory/credential information through health')
    report = {'url': origin, 'checked_at': datetime.now(timezone.utc).isoformat(), 'trusted_tls': True, 'health_no_roster_or_credentials': True}
    async with connect('wss://' + target.netloc + '/ws', ssl=context, origin=origin, open_timeout=8) as socket:
        certificate = socket.transport.get_extra_info('ssl_object').getpeercert()
        remaining = ssl.cert_time_to_seconds(certificate['notAfter']) - datetime.now(timezone.utc).timestamp()
        if remaining < 24 * 3600:
            raise ValueError('Certificate expires in less than 24 hours')
        report['certificate_hours_remaining'] = round(remaining / 3600, 2)
        # This is deliberately invalid, non-personal probe data, not an account credential.
        await socket.send(json.dumps({'op': 'watch', 'session': 'invalid-deployment-probe'}))
        try:
            async with asyncio.timeout(5):
                await socket.recv()
        except ConnectionClosed as error:
            if error.rcvd is None or error.rcvd.code != 4001:
                raise ValueError('The relay did not reject invalid account authorization with 4001') from error
        else:
            raise ValueError('An invalid account session received a response instead of rejection')
    report['account_session_required'] = True
    try:
        async with connect('wss://' + target.netloc + '/ws', ssl=context, origin='https://untrusted-origin.invalid', open_timeout=8):
            raise ValueError('Foreign browser Origin was accepted')
    except InvalidStatus as error:
        if error.response.status_code != 403:
            raise ValueError('Foreign Origin did not receive HTTP 403') from error
    report['foreign_origin_rejected'] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='For example https://117.50.48.22')
    parser.add_argument('--ca-file', help='Optional explicit private/test trust root; verification always remains enabled')
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(check(args.url, args.ca_file)), indent=2))
    except Exception as error:
        print(json.dumps({'passed': False, 'error': str(error)}))
        raise SystemExit(1) from error


if __name__ == '__main__':
    main()
