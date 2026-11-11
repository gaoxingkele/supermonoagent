"""Directory revisions are ordered within one relay connection, not across restarts."""
from test_workspace_profile_rooms import add_profile, fixture, open_profiles, run, wait_room


def test_directory_reconnect_accepts_restarted_relay_and_rejects_stale_callbacks():
    run(_check_reconnect)


async def _check_reconnect(playwright):
    async with fixture(playwright) as (browser, url, endpoint, _trap, _messages, daemons, *_):
        page = await browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.connected')
        await add_profile(page, endpoint, daemons[0].token)
        await wait_room(page, 0)

        # Model an established connection whose relay counter was higher before
        # the relay restarted. Its next socket receives the live fixture's lower counter.
        await page.evaluate("""() => {
          const directory = [...smaWorkspace.directories.values()][0];
          directory.revision += 10000;
          window.previousDirectory = {
            callback: directory.ws.onmessage,
            generation: directory.generation,
            revision: directory.revision
          };
          directory.ws.close(1000, 'fixture relay restart');
        }""")
        await page.wait_for_function("""() => {
          const directory = [...smaWorkspace.directories.values()][0];
          return directory.generation > previousDirectory.generation
            && directory.ws?.readyState === WebSocket.OPEN;
        }""")

        # A fresh probe can discover the same room even when an old persistent
        # directory erroneously rejects the new relay's lower revision.
        await open_profiles(page)
        await page.locator('#relay-profile-form button[type=submit]').click()
        await page.wait_for_function("document.querySelectorAll('#relay-probe-rooms input').length === 1")
        await page.wait_for_function("""() => {
          const directory = [...smaWorkspace.directories.values()][0];
          return directory.phase === 'connected' && directory.hosts.length === 1
            && directory.revision < previousDirectory.revision;
        }""", timeout=3000)
        await wait_room(page, 0)

        # Keep rejecting out-of-order data on this same socket and callbacks
        # captured from the previous socket, even after resetting the counter.
        assert await page.evaluate("""() => {
          const directory = [...smaWorkspace.directories.values()][0];
          const before = JSON.stringify([directory.revision, directory.hosts]);
          const message = revision => ({data: JSON.stringify({
            op: 'directory', revision,
            capabilities: {token_discovery: true}, hosts: []
          })});
          directory.ws.onmessage(message(directory.revision - 1));
          if (JSON.stringify([directory.revision, directory.hosts]) !== before) return false;
          previousDirectory.callback(message(previousDirectory.revision + 1));
          return JSON.stringify([directory.revision, directory.hosts]) === before;
        }""")
        assert not errors
