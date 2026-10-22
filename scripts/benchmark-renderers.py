"""Headless Edge DOM/WebGL comparison on an isolated, temporary local daemon."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from playwright.async_api import async_playwright
from websockets.asyncio.server import serve
from supermonoagent.daemon import Daemon
from test_client_integration import ControlledPty

EDGE = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")

MEASURE = r"""async ({text, fragmented}) => {
  const term = smaWorkspace.terminal;
  smaWorkspace.panes.get(smaWorkspace.activeSession).observer.disconnect();
  await new Promise(resolve => term.write('', resolve));
  term.reset(); term.resize(100, 30);
  term.options.cursorBlink = false;
  term.options.fontFamily = 'Consolas, monospace'; term.options.fontSize = 14;
  await document.fonts.ready;
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const bytes = new TextEncoder().encode(text);
  const start = performance.now();
  await new Promise(resolve => {
    if (fragmented) {
      for (let i=0; i<bytes.length; i++) term.write(bytes.subarray(i,i+1), i===bytes.length-1?resolve:undefined);
    } else term.write(bytes, resolve);
  });
  const parsed = performance.now();
  const firstFrame = await new Promise(resolve => requestAnimationFrame(resolve));
  const secondFrame = await new Promise(resolve => requestAnimationFrame(resolve));
  const buffer = term.buffer.active;
  const lines = Array.from({length:buffer.length}, (_,i)=>buffer.getLine(i).translateToString(true));
  const widths = [];
  const styledCells = [];
  for (let row=0; row<buffer.length; row++) {
    const line=buffer.getLine(row);
    for(let col=0; col<term.cols; col++) {
      const cell=line.getCell(col), value=cell?.getChars();
      if(value && /[中文🙂]/u.test(value)) widths.push({value,width:cell.getWidth()});
      if(value)styledCells.push([row,col,value,cell.getWidth(),cell.getFgColorMode(),cell.getFgColor(),
        cell.getBgColorMode(),cell.getBgColor(),cell.isBold(),cell.isItalic(),cell.isUnderline(),cell.isInverse()]);
    }
  }
  return {requestedBytes:bytes.length,fragmented,writes:fragmented?bytes.length:1,
    parseCallbackMs:parsed-start,firstRafAfterParseMs:firstFrame-parsed,
    twoRafObservationMs:secondFrame-start,lines,widths,styledCells,cursor:{x:buffer.cursorX,y:buffer.cursorY},
    terminal:{cols:term.cols,rows:term.rows,fontFamily:term.options.fontFamily,fontSize:term.options.fontSize,
      unicodeVersion:term.unicode.activeVersion},effectiveRenderer:smaWorkspace.effectiveRenderer,
    devicePixelRatio,consolasAvailable:document.fonts.check('14px Consolas')};
}"""

GPU = r"""() => {
  const pane=smaWorkspace.panes.get(smaWorkspace.activeSession);
  for(const canvas of pane.screen.querySelectorAll('canvas')) {
    const gl=canvas.getContext('webgl2');
    if(!gl)continue;
    window.__benchmarkGL=gl;
    const debug=gl.getExtension('WEBGL_debug_renderer_info');
    return {version:gl.getParameter(gl.VERSION),vendor:gl.getParameter(gl.VENDOR),
      renderer:gl.getParameter(gl.RENDERER),
      unmaskedVendor:debug?gl.getParameter(debug.UNMASKED_VENDOR_WEBGL):null,
      unmaskedRenderer:debug?gl.getParameter(debug.UNMASKED_RENDERER_WEBGL):null,
      contextLost:gl.isContextLost(),loseContextAvailable:!!gl.getExtension('WEBGL_lose_context')};
  }
  return null;
}"""


async def run(args):
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    small = "\x1b[2J\x1b[HASCII 中文 🙂 e\u0301\r\n\x1b[31mRED\x1b[0m \x1b[38;2;20;180;220mRGB\x1b[0m\r\nprogress 10%\rprogress 99%\x1b[K\r\nEND\r\n"
    bulk = "\x1b[2J\x1b[H" + "".join(f"{i:04d} 中文 🙂 e\u0301 \x1b[32mgreen\x1b[0m │ powershell / cmd\r\n" for i in range(1000))
    results, errors = [], []
    with tempfile.TemporaryDirectory(prefix="supermono-renderer-") as directory, patch(
        "supermonoagent.daemon.STATE", Path(directory)
    ), patch.dict(os.environ, {"SUPERMONO_STATE_DIR": directory}):
        daemon = Daemon("renderer-test-only")
        daemon.hub.persist = False
        daemon.hub.pty_factory = lambda *_: ControlledPty()
        daemon.hub.attach_loop(asyncio.get_running_loop())
        window = daemon.hub.open_window(owner=daemon.token)
        session = daemon.hub.open_cli(window.id, "cmd")
        try:
            async with serve(daemon.handle_socket, "127.0.0.1", 0, process_request=daemon.serve_http) as server:
                port = server.sockets[0].getsockname()[1]
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(executable_path=str(args.edge), headless=True)
                    try:
                        browser_version = browser.version
                        for requested in ("dom", "webgl"):
                            context = await browser.new_context(viewport={"width":1440,"height":900}, device_scale_factor=1)
                            page = await context.new_page()
                            page.on("pageerror", lambda error: errors.append(str(error)))
                            await page.goto(f"http://127.0.0.1:{port}/workspace?renderer={requested}")
                            await page.wait_for_function("window.smaWorkspace?.connected", timeout=10000)
                            await page.evaluate("sid => smaWorkspace.openSession(sid)", session.id)
                            await page.wait_for_function("!!smaWorkspace.terminal", timeout=10000)
                            await page.wait_for_timeout(100)
                            gpu = await page.evaluate(GPU) if requested == "webgl" else None
                            if gpu:
                                description = (gpu.get("unmaskedRenderer") or gpu["renderer"]).lower()
                                gpu["backendClassification"] = "software-reported" if any(
                                    name in description for name in ("swiftshader", "llvmpipe", "software", "microsoft basic")
                                ) else "hardware-reported" if gpu.get("unmaskedRenderer") else "unknown"
                            samples = []
                            for name, text, fragmented in (("whole-correctness", small, False),
                                                          ("byte-fragmented-correctness", small, True),
                                                          ("bulk", bulk, False)):
                                result = await page.evaluate(MEASURE, {"text":text,"fragmented":fragmented})
                                lines = result.pop("lines")
                                result["bufferSha256"] = hashlib.sha256("\n".join(lines).encode()).hexdigest()
                                result["cellStyleSha256"] = hashlib.sha256(json.dumps(result.pop("styledCells"),ensure_ascii=False).encode()).hexdigest()
                                result["bufferLines"] = len(lines)
                                result["sample"] = name
                                result["widthSummary"] = sorted({(item["value"],item["width"]) for item in result.pop("widths")})
                                samples.append(result)
                            screenshot = args.output / f"workspace-{requested}.png"
                            await page.screenshot(path=str(screenshot))
                            effective = await page.evaluate("smaWorkspace.effectiveRenderer")
                            entry = {"requested":requested,"effective":effective,"gpu":gpu,
                                     "samples":samples,"screenshot":str(screenshot.relative_to(ROOT) if screenshot.is_relative_to(ROOT) else screenshot)}
                            if requested == "webgl" and effective == "webgl" and gpu and gpu["loseContextAvailable"]:
                                before = await page.evaluate("smaWorkspace.metrics.rendererFallbacks")
                                await page.evaluate("window.__benchmarkGL.getExtension('WEBGL_lose_context').loseContext()")
                                await page.wait_for_function("smaWorkspace.effectiveRenderer === 'dom'", timeout=7000)
                                entry["contextLoss"] = await page.evaluate("({effective:smaWorkspace.effectiveRenderer,fallbacks:smaWorkspace.metrics.rendererFallbacks})")
                                entry["contextLoss"]["fallbacksBefore"] = before
                                entry["contextLoss"]["bufferSha256After"] = hashlib.sha256((await page.evaluate(
                                    "Array.from({length:smaWorkspace.terminal.buffer.active.length},(_,i)=>smaWorkspace.terminal.buffer.active.getLine(i).translateToString(true)).join('\\n')"
                                )).encode()).hexdigest()
                                entry["contextLoss"]["bufferPreserved"] = entry["contextLoss"]["bufferSha256After"] == samples[-1]["bufferSha256"]
                                assert entry["contextLoss"]["bufferPreserved"], "Context-loss fallback changed terminal text"
                                await page.screenshot(path=str(args.output / "workspace-context-loss-dom.png"))
                            else:
                                entry["contextLoss"] = {"tested":False,"reason":"effective WebGL or lose-context extension unavailable"}
                            results.append(entry)
                            await context.close()
                        await browser.close()
                    finally:
                        await browser.close()
        finally:
            for wid in list(daemon.hub.windows):
                daemon.hub.close_window(wid)
            await asyncio.sleep(0.03)
    parity = all(a["bufferSha256"] == b["bufferSha256"] and a["widthSummary"] == b["widthSummary"]
                 and a["cellStyleSha256"] == b["cellStyleSha256"]
                 and a["cursor"] == b["cursor"] and a["terminal"] == b["terminal"]
                 for a,b in zip(results[0]["samples"], results[1]["samples"]))
    fragmentation = all(entry["samples"][0]["bufferSha256"] == entry["samples"][1]["bufferSha256"]
                        and entry["samples"][0]["cellStyleSha256"] == entry["samples"][1]["cellStyleSha256"] for entry in results)
    report = {"recordedAtUtc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
              "environment":{"platform":platform.platform(),"python":sys.version,"edgePath":str(args.edge),
                             "edgeVersion":browser_version,"headless":True,"viewport":{"width":1440,"height":900},
                             "deviceScaleFactor":1,"isolatedHost":True,"physicalAndroid":False},
              "results":results,"bufferAndWidthParity":parity,"byteFragmentationParity":fragmentation,
              "pageErrors":errors,"limitations":["rAF is a paint opportunity observation, not proof of GPU presentation",
                "parse callback includes scheduling; not a pure parser throughput profile",
                "fixed font family can still use system glyph fallback for CJK and emoji",
                "no keyboard end-to-end, native terminal UI or physical Android measurement"]}
    (args.output / "current.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"edge":browser_version,"bufferParity":parity,"fragmentationParity":fragmentation,
                      "renderers":[{"requested":r["requested"],"effective":r["effective"],"gpu":r["gpu"]} for r in results],
                      "pageErrors":errors},ensure_ascii=False,indent=2))
    if not parity or not fragmentation or errors:
        raise SystemExit("Renderer correctness verification failed; inspect artifacts")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge",type=Path,default=EDGE)
    parser.add_argument("--output",type=Path,default=ROOT / ".omx/artifacts/renderers")
    args=parser.parse_args()
    if not args.edge.is_file():
        parser.error("Edge executable not found; pass --edge <path>")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
