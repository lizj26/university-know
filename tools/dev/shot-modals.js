// 把「信息来源」「数据概览」两个弹窗截图出来(看状态徽标)
"use strict";
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const FILE = "file:///E:/project/thu-know/index.html";
const OUT = __dirname;
const PORT = 9388;
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "cdp-modal-"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--hide-scrollbars",
  "--remote-debugging-port=" + PORT, "--user-data-dir=" + profile,
  "--window-size=1440,1100", "--force-device-scale-factor=1", "about:blank",
], { stdio: "ignore" });

async function getTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const page = (await res.json()).find((t) => t.type === "page");
      if (page) return page;
    } catch (e) { /* not ready */ }
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
  ws.onmessage = (ev) => {
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
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: FILE + "#campus=zju" });
  await sleep(5000);

  const shot = async (name) => {
    const s = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    const p = path.join(OUT, name);
    fs.writeFileSync(p, Buffer.from(s.data, "base64"));
    console.log("saved " + name + "  " + fs.statSync(p).size + " bytes");
  };

  await send("Runtime.evaluate", { expression: "document.querySelector('[data-open=\"sources\"]').click()" });
  await sleep(900);
  await shot("modal-sources-zju.png");
  await send("Runtime.evaluate", { expression: "document.getElementById('modal-dialog').close()" });
  await sleep(400);
  await send("Runtime.evaluate", { expression: "document.querySelector('[data-open=\"stats\"]').click()" });
  await sleep(900);
  await shot("modal-stats-zju.png");

  const info = await send("Runtime.evaluate", {
    expression: "JSON.stringify({badges:document.querySelectorAll('#modal-body .src-status').length," +
      "text:document.getElementById('modal-body').textContent.replace(/\\s+/g,' ').slice(0,260)})",
    returnByValue: true
  });
  console.log("数据概览: " + info.result.value);

  ws.close();
  chrome.kill();
  process.exit(0);
})().catch((e) => { console.error("ERR", e.message); chrome.kill(); process.exit(1); });
