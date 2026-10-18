// 用 Chrome DevTools Protocol 驱动无头浏览器:诊断横向溢出 + 截图
// 用法: node cdp.js <url> <outPng> [width] [height] [--dark] [--eval <js>]
"use strict";
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const args = process.argv.slice(2);
const url = args[0];
const out = args[1];
let width = 1680, height = 1200, dark = false, evalJs = null;
for (let i = 2; i < args.length; i++) {
  if (args[i] === "--dark") dark = true;
  else if (args[i] === "--eval") { evalJs = args[++i]; }
  else if (!isNaN(+args[i])) { if (width === 1680 && !isNaN(+args[i])) { width = +args[i]; } else height = +args[i]; }
}
// 更稳妥的解析:前两个数字分别是宽高
const nums = args.slice(2).filter(a => !isNaN(+a)).map(Number);
if (nums.length >= 2) { width = nums[0]; height = nums[1]; }

const PORT = 9333;
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "cdp-prof-"));

const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--hide-scrollbars",
  "--remote-debugging-port=" + PORT,
  "--user-data-dir=" + profile,
  "--window-size=" + width + "," + height,
  "--force-device-scale-factor=1",
  ...(dark ? ["--force-dark-mode", "--blink-settings=preferredColorScheme=0"] : []),
  "about:blank",
], { stdio: "ignore" });

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function getTarget() {
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await res.json();
      const page = list.find(t => t.type === "page");
      if (page) return page;
    } catch (e) { /* 未就绪 */ }
    await sleep(250);
  }
  throw new Error("Chrome 调试端口未就绪");
}

(async () => {
  const target = await getTarget();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const send = (method, params) => new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const p = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? p.reject(new Error(JSON.stringify(msg.error))) : p.resolve(msg.result);
    }
  };
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride",
    { width, height, deviceScaleFactor: 1, mobile: width < 700 });
  await send("Page.navigate", { url });
  await sleep(3500);

  if (evalJs) {
    const r = await send("Runtime.evaluate", { expression: evalJs, returnByValue: true, awaitPromise: true });
    console.log(JSON.stringify(r.result.value, null, 2));
  } else {
    const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(out, Buffer.from(shot.data, "base64"));
    console.log("saved", out, fs.statSync(out).size, "bytes");
  }
  ws.close();
  chrome.kill();
  process.exit(0);
})().catch(e => { console.error("ERR", e.message); chrome.kill(); process.exit(1); });
