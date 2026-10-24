from pathlib import Path

from supermonoagent.assistant_preferences import AssistantPreferences
from test_workspace_profile_rooms import fixture, run


def test_model_preferences_save_without_network_or_terminal_input(tmp_path):
    run(lambda playwright: exercise(playwright, tmp_path))


async def exercise(playwright, tmp_path):
    preferences = AssistantPreferences(tmp_path / 'preferences.json')
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages, _daemons, _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1100, 'height': 800})
        requests, errors = [], []
        page.on('request', lambda request: requests.append(request.url))
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.wait_for_function('smaWorkspace?.connected&&smaWorkspace.activePane?.ready')
        await page.expose_function('fixturePreferencesRead', preferences.get)
        await page.expose_function('fixturePreferencesSave', preferences.save)
        await page.evaluate('''()=>{window.pywebview={api:{recommend_skills:async()=>({available:true,indexed:0,skills:[]}),get_assistant_preferences:fixturePreferencesRead,save_assistant_preferences:fixturePreferencesSave}};dispatchEvent(new Event('pywebviewready'));}''')
        await page.locator('#mylib-toggle').click()
        await page.locator('#mylib-configure').click()
        assert await page.locator('#mylib-provider').input_value() == 'unconfigured'
        assert not await page.locator('#mylib-auto-context').is_checked()
        assert not preferences.path.exists()
        await page.locator('#mylib-provider').select_option('api')
        assert await page.locator('#mylib-endpoint').is_visible()
        await page.locator('#mylib-model').fill('fixture-model')
        await page.locator('#mylib-endpoint').fill('https://model.example.test/v1')
        await page.locator('#mylib-settings-save').click()
        await page.wait_for_function('document.querySelector("#mylib-settings-status").textContent.includes("已保存")')
        saved = preferences.get()['preferences']
        assert saved['provider'] == 'api' and saved['model'] == 'fixture-model'
        assert saved['endpoint'] == 'https://model.example.test/v1'
        assert not saved['auto_context']
        await page.screenshot(path=str(tmp_path / 'api-preferences.png'))
        assert not any('model.example.test' in request for request in requests)
        # Unsafe endpoint is rejected and leaves the last saved profile intact.
        await page.locator('#mylib-endpoint').fill('http://remote.example.test/v1')
        await page.locator('#mylib-settings-save').click()
        await page.wait_for_function('document.querySelector("#mylib-settings-status").textContent.includes("未保存")')
        assert preferences.get()['preferences'] == saved
        await page.locator('#mylib-settings-cancel').click()
        await page.locator('#mylib-configure').click()
        assert await page.locator('#mylib-endpoint').input_value() == saved['endpoint']
        await page.locator('#mylib-endpoint').fill('not a url')
        await page.locator('#mylib-provider').select_option('codex')
        assert not await page.locator('#mylib-endpoint').is_visible()
        assert await page.locator('#mylib-endpoint').is_disabled()
        await page.locator('#mylib-model').fill('')
        await page.locator('#mylib-settings-save').click()
        await page.wait_for_function('document.querySelector("#mylib-settings-status").textContent.includes("已保存")')
        assert preferences.get()['preferences']['endpoint'] == ''
        assert not errors
        assert not any(session.pty.written for owned in sessions for session in owned)
