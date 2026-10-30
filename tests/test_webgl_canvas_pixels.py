"""Verify WebGL backing pixels retain the terminal grid at fractional DPR."""
import asyncio
import base64
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')


@pytest.mark.parametrize('dpr', [1.0, 1.25, 1.5, 2.0, 3.0])
def test_webgl_canvas_matches_terminal_grid(dpr):
    playwright = pytest.importorskip('playwright.async_api')
    if not EDGE.exists():
        pytest.skip('Hidden Edge needed for WebGL pixel checks')
    asyncio.run(check_canvas(dpr, playwright))


async def check_canvas(dpr, playwright):
    async with playwright.async_playwright() as runtime:
        browser = await runtime.chromium.launch(executable_path=str(EDGE), headless=True)
        try:
            context = await browser.new_context(
                viewport={'width': 1400, 'height': 500}, device_scale_factor=dpr)
            page = await context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.set_content('''<html><head><style>
              body{margin:0;background:#121212}#terminal{padding:8px}
              </style></head><body><div id="terminal"></div></body></html>''')
            await page.add_style_tag(path=str(ROOT / 'src/supermonoagent/static/xterm.css'))
            for name in ('xterm.js', 'xterm-unicode11.js', 'xterm-webgl.js', 'terminal-client.js'):
                await page.add_script_tag(path=str(ROOT / 'src/supermonoagent/static' / name))
            await page.evaluate(r'''async () => {
              const term=new Terminal({cols:80,rows:14,allowProposedApi:true,disableStdin:true,
                cursorBlink:false,fontFamily:'"Cascadia Mono", "Cascadia Code", Consolas, "Microsoft YaHei UI", "Microsoft YaHei", "DengXian", "Noto Sans CJK SC", monospace',
                fontSize:14,scrollback:1000,minimumContrastRatio:4.5,
                theme:{background:'#121212',foreground:'#dedede'},rendererType:'dom'});
              term.loadAddon(new Unicode11Addon.Unicode11Addon());term.unicode.activeVersion='11';
              term.open(document.getElementById('terminal'));
              const gl=new WebglAddon.WebglAddon({preserveDrawingBuffer:true});term.loadAddon(gl);
              const highlighter=TerminalClient.createHighlighter(term,{enabled:true});
              window.testTerm=term;window.testGL=gl;
              const text='Todo\r\n\x1b[32m✓\x1b[0m 抓取当前窗口与其他窗口的刷新异常证据\r\n'
                +'\x1b[36m●\x1b[0m \x1b[1m复现并定位终端缓冲或绘制的根因\x1b[0m\r\n'
                +'○ 修复刷新路径并补充回归测试\r\n○ 构建安装并逐窗口真机验收\r\n'
                +'中文 🙂 e\u0301 │ app.py 42\r\n';
              for(let i=0;i<12;i++){
                await new Promise(done=>term.write('\x1b[2J\x1b[H'+(i%2?'⠹':'⠙')+' '+text,done));
                await new Promise(requestAnimationFrame);
              }
              await new Promise(done=>term.write('\x1b[2J\x1b[H'+text,done));
              term.refresh(0,term.rows-1);
              await document.fonts.ready;
              await new Promise(done=>setTimeout(done,80));
            }''')
            metrics = await page.evaluate(r'''() => {
              const term=testTerm,r=testGL._renderer,cell=r.dimensions.device.cell;
              const copy=document.createElement('canvas');copy.width=r._canvas.width;copy.height=r._canvas.height;
              const ctx=copy.getContext('2d');ctx.drawImage(r._canvas,0,0);
              const ink=[];
              for(let y=0;y<6;y++){
                const pixels=ctx.getImageData(0,y*cell.height,Math.min(copy.width,50*cell.width),cell.height).data;
                let count=0;for(let i=0;i<pixels.length;i+=4)if(pixels[i]+pixels[i+1]+pixels[i+2]>180&&pixels[i+3]>200)count++;
                ink.push(count);
              }
              return {expected:r.dimensions.device.canvas,viewport:Array.from(r._gl.getParameter(r._gl.VIEWPORT)),
                canvas:{width:r._canvas.width,height:r._canvas.height},ink,
                lines:Array.from({length:6},(_,y)=>term.buffer.active.getLine(y).translateToString(true)),
                raw:r._canvas.toDataURL('image/png')};
            }''')
            expected = metrics['expected']
            assert metrics['viewport'] == [0, 0, expected['width'], expected['height']]
            assert abs(metrics['canvas']['width'] - expected['width']) <= 2
            assert abs(metrics['canvas']['height'] - expected['height']) <= 2
            assert metrics['lines'] == [
                'Todo', '✓ 抓取当前窗口与其他窗口的刷新异常证据',
                '● 复现并定位终端缓冲或绘制的根因', '○ 修复刷新路径并补充回归测试',
                '○ 构建安装并逐窗口真机验收', '中文 🙂 é │ app.py 42']
            assert all(count > 20 for count in metrics['ink']), metrics['ink']
            assert not errors, errors
            artifacts = ROOT / '.omx/artifacts/webgl-canvas-pixels'
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / f'webgl-dpr-{dpr}.png').write_bytes(base64.b64decode(metrics['raw'].split(',', 1)[1]))
            await page.screenshot(path=str(artifacts / f'page-dpr-{dpr}.png'))
            (artifacts / f'metrics-{dpr}.json').write_text(json.dumps(
                {key: value for key, value in metrics.items() if key != 'raw'}, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            await browser.close()
