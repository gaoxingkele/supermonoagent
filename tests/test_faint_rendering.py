"""Measure owned ANSI faint pixels in actual xterm DOM and WebGL output."""
from collections import Counter
import json
import struct
import zlib

import pytest

from test_workspace_profile_rooms import fixture, run


def pixels(png):
    """Decode Chromium's 8-bit RGB/RGBA PNG without adding an imaging dependency."""
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    offset, chunks = 8, []
    while offset < len(png):
        size = struct.unpack('>I', png[offset:offset + 4])[0]
        kind, data = png[offset + 4:offset + 8], png[offset + 8:offset + 8 + size]
        if kind == b'IHDR': width, height, depth, color, *_ = struct.unpack('>IIBBBBB', data)
        if kind == b'IDAT': chunks.append(data)
        offset += size + 12
    assert depth == 8 and color in (2, 6)
    channels = 3 if color == 2 else 4
    stride, raw, previous, output = width * channels, zlib.decompress(b''.join(chunks)), bytearray(width * channels), []
    for y in range(height):
        start = y * (stride + 1)
        mode, row = raw[start], bytearray(raw[start + 1:start + stride + 1])
        for x in range(stride):
            left = row[x - channels] if x >= channels else 0
            up, corner = previous[x], previous[x - channels] if x >= channels else 0
            if mode == 1: predictor = left
            elif mode == 2: predictor = up
            elif mode == 3: predictor = (left + up) // 2
            elif mode == 4:
                p = left + up - corner
                predictor = min((left, up, corner), key=lambda v: abs(p - v))
            else:
                assert mode == 0
                predictor = 0
            row[x] = (row[x] + predictor) & 255
        output.extend(tuple(row[x:x + 3]) for x in range(0, stride, channels))
        previous = row
    return output


@pytest.mark.parametrize('renderer', ['dom', 'webgl'])
def test_faint_default_foreground_actual_rendered_gray(renderer, tmp_path):
    run(lambda playwright: check_faint(playwright, renderer, tmp_path))


async def check_faint(playwright, renderer, tmp_path):
    async with fixture(playwright) as (browser, url, _endpoint, _trap, _messages,
                                       _daemons, _ops, _windows, sessions):
        page = await browser.new_page(viewport={'width': 1200, 'height': 700})
        await page.goto(url)
        await page.wait_for_function('smaWorkspace.activePane?.ready')
        measurements = []
        for ratio in (1, 4.5):
            state = await page.evaluate(r'''async ({renderer,ratio})=>{
                const box=document.createElement('div');box.id='faint-owned';box.style.cssText='position:fixed;inset:0;z-index:3000;background:#11151b;padding:20px';document.body.append(box);
                const el=document.createElement('div');el.style.cssText='width:1000px;height:100px';box.append(el);
                const profile=await TerminalClient.loadAgentProfile('/agent-modes/claude-code.json','claude-code');
                const t=new Terminal({cols:70,rows:2,fontSize:20,fontFamily:'Consolas, monospace',allowProposedApi:true,cursorBlink:false,theme:profile.theme,minimumContrastRatio:ratio});t.open(el);
                if(renderer==='webgl')t.loadAddon(new WebglAddon.WebglAddon());
                window.faintOwned=t;
                await new Promise(done=>t.write('\x1b[?25l> \x1b[2mTry "edit <filepath> to..."\x1b[0m\r\n\x1b[38;2;153;153;153mshift+tab to cycle\x1b[0m',done));
                window.faintHighlighter=TerminalClient.createHighlighter(t,{profile});
                await new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)));
                const rect=t.element.querySelector('.xterm-screen').getBoundingClientRect(),cell=t._core._renderService.dimensions.css.cell;
                return {clip:{x:rect.x+2*cell.width,y:rect.y,width:30*cell.width,height:cell.height},
                    dim:!!t.buffer.active.getLine(0).getCell(2).isDim(),
                    spans:[...t.element.querySelectorAll('.xterm-rows>div:first-child span')].map(s=>({color:getComputedStyle(s).color,opacity:getComputedStyle(s).opacity,text:s.textContent}))};
            }''', {'renderer': renderer, 'ratio': ratio})
            image = await page.screenshot(clip=state['clip'], path=str(tmp_path / f'{renderer}-{ratio}.png'))
            counts = Counter(pixels(image))
            foreground = max(counts, key=lambda rgb: sum(rgb))
            measurements.append({'ratio': ratio, 'brightest_rgb': foreground,
                                 'brightest_count': counts[foreground], 'dim': state['dim'], 'spans': state['spans']})
            await page.evaluate('()=>{faintHighlighter.dispose();faintOwned.dispose();document.querySelector("#faint-owned").remove();}')
        (tmp_path / 'rendered-gray.json').write_text(json.dumps(measurements, indent=2), encoding='utf-8')
        assert all(item['dim'] for item in measurements)
        # White/default foreground is (214,217,223); native faint must render
        # substantially darker rather than be promoted back to body brightness.
        assert all(max(item['brightest_rgb']) <= 130 for item in measurements), measurements
        assert measurements[0]['brightest_rgb'] == measurements[1]['brightest_rgb']
        assert not any(session.pty.written for owned in sessions for session in owned)
