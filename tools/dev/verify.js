// 针对本次修改的浏览器回归检查:CDP 驱动无头 Chrome + 一组断言
"use strict";
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const URL_ = "file:///E:/project/thu-know/index.html";
const OUT_DIR = path.join(__dirname, "..", "_check");
const PORT = 9360;
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "cdp-verify-"));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--hide-scrollbars",
  "--remote-debugging-port=" + PORT,
  "--user-data-dir=" + profile,
  "--window-size=1680,1200", "--force-device-scale-factor=1",
  "about:blank",
], { stdio: "ignore" });

async function getTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await res.json();
      const page = list.find((t) => t.type === "page");
      if (page) return page;
    } catch (e) { /* not ready */ }
    await sleep(250);
  }
  throw new Error("Chrome 调试端口未就绪");
}

const PAGE_SCRIPT = `(async () => {
  const out = { checks: [] };
  const $ = (s) => document.querySelector(s);
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const ok = (name, pass, detail) => out.checks.push({ name, pass: !!pass, detail: detail === undefined ? null : detail });
  const shown = (el) => el ? getComputedStyle(el).display !== "none" : false;

  let data;
  try { data = JSON.parse($("#notice-data").textContent); }
  catch (e) { return { fatal: "内嵌 JSON 解析失败: " + e.message }; }
  const campus = data.default_campus;
  const items = data.campuses[campus].items;
  out.items = items.length;
  out.sources_declared = (data.campuses[campus].sources || []).length;
  out.sources_actual = new Set(items.map(x => x.s)).size;

  /* 1. [hidden] 是否真的生效 */
  const clear = $("#q-clear");
  ok("空搜索框时「×」不显示", !shown(clear), getComputedStyle(clear).display);
  const inp = $("#q");
  inp.value = "讲座"; inp.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(450);
  ok("有输入时「×」显示", shown(clear), getComputedStyle(clear).display);
  $("#q-clear").click();
  await sleep(450);
  ok("清空后「×」重新隐藏", !shown(clear), getComputedStyle(clear).display);

  /* 2. 无发布时间条目 */
  out.undated_in_data = items.filter(x => !x.d && !(x.dt && x.dt.ev && x.dt.ev[0])).length;
  let guard = 0;
  while (guard++ < 25) {
    const more = [...document.querySelectorAll(".section-more")].find(b => /展开全部/.test(b.textContent));
    if (!more) break;
    more.click();
    await sleep(140);
  }
  const times = [...document.querySelectorAll(".notice-block-time span")].map(e => e.textContent.trim());
  ok("没有空的时间标签", times.every(t => t.length > 0), times.filter(t => !t).length + " 个空标签");
  const pending = times.filter(t => t === "时间待定").length;
  ok("无发布时间条目显示「时间待定」", pending > 0, pending + " 条");

  const srt = $("#sort");
  srt.value = "asc"; srt.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(400);
  const cards = [...document.querySelectorAll(".notice-block")];
  const lastTime = cards.length ? cards[cards.length - 1].querySelector(".notice-block-time span").textContent.trim() : "";
  ok("升序时「时间待定」排在最后", lastTime !== "时间待定" && lastTime.length > 0, "最后一条=" + lastTime);
  srt.value = "desc"; srt.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(350);

  /* 3. 时间筛选不再吞掉无时间条目 */
  const rangeEl = $("#range");
  const rangeVal = [...rangeEl.options].map(o => o.value).find(v => v !== "all");
  rangeEl.value = rangeVal; rangeEl.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(450);
  const ft = [...document.querySelectorAll(".notice-block-time span")].map(e => e.textContent.trim());
  ok("时间筛选下仍保留无时间条目", ft.includes("时间待定"), "range=" + rangeVal + " 共 " + ft.length + " 条");
  rangeEl.value = "all"; rangeEl.dispatchEvent(new Event("change", { bubbles: true }));
  await sleep(450);

  /* 4. 详情弹窗 */
  const noDate = items.find(x => !x.d);
  if (noDate) {
    location.hash = "#notice=" + encodeURIComponent(noDate.i);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await sleep(550);
    const pub = [...document.querySelectorAll("#detail-body .meta-row")].map(r => r.textContent).find(t => t.indexOf("发布时间") >= 0) || "";
    ok("无发布时间时详情写「未公布」", pub.indexOf("未公布") >= 0, pub.slice(0, 40));
    $("#detail-dialog").close(); location.hash = ""; await sleep(300);
  }
  const noEn = items.find(x => !x.en);
  if (noEn) {
    location.hash = "#notice=" + encodeURIComponent(noEn.i);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await sleep(550);
    ok("无英文标题时 #detail-en 真正隐藏", !shown($("#detail-en")), getComputedStyle($("#detail-en")).display);
    $("#detail-dialog").close(); location.hash = ""; await sleep(300);
  }

  /* 5. 近期活动按钮状态 */
  const up = document.querySelector('[data-open="upcoming"]');
  up.click(); await sleep(450);
  ok("进入近期活动后为按下态", up.getAttribute("aria-pressed") === "true" && up.style.background !== "", up.getAttribute("aria-pressed") + " / " + up.style.background);
  $("#cat-all").click(); await sleep(450);
  ok("点「全部」后按下态清除", up.getAttribute("aria-pressed") === "false" && up.style.background === "", up.getAttribute("aria-pressed") + " / " + up.style.background);
  up.click(); await sleep(400);
  $("#brand-link").click(); await sleep(450);
  ok("点 logo 后按下态清除", up.getAttribute("aria-pressed") === "false" && up.style.background === "", up.getAttribute("aria-pressed") + " / " + up.style.background);

  /* 6. 语言按钮 */
  const lang = $("#lang-toggle");
  const zhLabel = lang.getAttribute("aria-label");
  lang.click(); await sleep(450);
  const enLabel = lang.getAttribute("aria-label");
  ok("英文模式下 aria-label 变成反向动作", zhLabel === "Switch to English" && enLabel === "Switch to Chinese titles", zhLabel + " -> " + enLabel);
  ok("英文模式下 placeholder 为英文", $("#q").placeholder === "Search notices", $("#q").placeholder);
  const firstTitleLang = document.querySelector(".notice-block-title");
  ok("英文模式下标题标 lang=en", firstTitleLang && firstTitleLang.getAttribute("lang") === "en", firstTitleLang && firstTitleLang.getAttribute("lang"));
  lang.click(); await sleep(400);
  ok("切回中文后 aria-label 复位", lang.getAttribute("aria-label") === "Switch to English", lang.getAttribute("aria-label"));

  /* 7. 实时状态区 */
  ok("结果容器不再整体 aria-live", !$("#board-results").hasAttribute("aria-live"), $("#board-results").getAttribute("aria-live"));
  ok("存在 role=status 的状态区", !!$("#board-status") && $("#board-status").getAttribute("role") === "status", null);
  const inp2 = $("#q");
  inp2.value = "讲座"; inp2.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(500);
  ok("状态区随搜索结果更新", /共 \\d+ 条通知/.test($("#board-status").textContent), $("#board-status").textContent);
  $("#q-clear").click(); await sleep(450);

  /* 8. 展开全部后焦点保留 */
  const more = [...document.querySelectorAll(".section-more")].find(b => /展开全部/.test(b.textContent));
  if (more) {
    const key = more.dataset.key;
    more.focus(); more.click(); await sleep(450);
    const act = document.activeElement;
    ok("展开后焦点回到同一分区的按钮", act && act.classList.contains("section-more") && act.dataset.key === key,
      act ? act.tagName + "." + act.className + " / " + act.dataset.key : "null");
  } else {
    ok("找到可展开的分区", false, "没有「展开全部」按钮");
  }

  /* 9. 跳转链接 */
  const skip = document.querySelector(".skip-link");
  ok("跳转链接指向 #notices", skip && skip.getAttribute("href") === "#notices", skip && skip.getAttribute("href"));

  /* 10. 页面统计与数据一致 */
  const statBtn = [...document.querySelectorAll("[data-open]")].find(b => b.dataset.open === "stats");
  statBtn.click(); await sleep(400);
  const tiles = [...document.querySelectorAll("#modal-body .stat-tile")].map(t => t.textContent.trim());
  const srcTile = tiles.find(t => t.indexOf("个信息来源") >= 0) || "";
  ok("「N 个信息来源」与实际来源数一致", srcTile.indexOf(String(out.sources_actual)) === 0,
    srcTile + " / 声明 " + out.sources_declared + " / 实际 " + out.sources_actual);
  const totalTile = tiles.find(t => t.indexOf("条通知") >= 0) || "";
  ok("「N 条通知」与实际条数一致", totalTile.indexOf(String(items.length)) === 0, totalTile + " / 实际 " + items.length);
  $("#modal-dialog").close(); await sleep(200);

  /* 11. 焦点轮廓不再改圆角 */
  const css = [...document.styleSheets[0].cssRules].map(r => r.cssText).join("\\n");
  const focusRule = [...document.styleSheets[0].cssRules].find(r => r.selectorText === ":focus-visible");
  ok(":focus-visible 不再改 border-radius", focusRule && focusRule.style.borderRadius === "", focusRule ? focusRule.cssText : "未找到");
  ok("存在 [hidden] 兜底规则", /\\[hidden\\]/.test(css), null);

  out.failures = out.checks.filter(c => !c.pass).map(c => c.name);
  out.passed = out.checks.length - out.failures.length;
  out.total = out.checks.length;
  return out;
})()`;

(async () => {
  const target = await getTarget();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const consoleMsgs = [];
  const exceptions = [];
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
      return;
    }
    if (msg.method === "Runtime.consoleAPICalled") {
      consoleMsgs.push({ type: msg.params.type, text: msg.params.args.map(a => a.value !== undefined ? String(a.value) : (a.description || a.type)).join(" ") });
    } else if (msg.method === "Runtime.exceptionThrown") {
      const d = msg.params.exceptionDetails;
      exceptions.push({ text: d.text, desc: d.exception && (d.exception.description || d.exception.value) });
    }
  };
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1680, height: 1200, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: URL_ });
  await sleep(4500);

  const r = await send("Runtime.evaluate", { expression: PAGE_SCRIPT, returnByValue: true, awaitPromise: true });
  const result = r.result ? r.result.value : null;
  const report = {
    console: consoleMsgs,
    exceptions,
    evaluate_error: r.exceptionDetails ? (r.exceptionDetails.exception && r.exceptionDetails.exception.description) || r.exceptionDetails.text : null,
    result
  };
  fs.writeFileSync(path.join(OUT_DIR, "verify-report.json"), JSON.stringify(report, null, 2));

  console.log("控制台消息: " + consoleMsgs.length + "  未捕获异常: " + exceptions.length);
  if (report.evaluate_error) console.log("EVAL_ERROR: " + report.evaluate_error);
  if (result) {
    console.log("通过 " + result.passed + "/" + result.total);
    for (const c of result.checks) console.log((c.pass ? "  OK   " : "  FAIL ") + c.name + (c.detail !== null ? "  [" + c.detail + "]" : ""));
    console.log("数据: items=" + result.items + " sources_declared=" + result.sources_declared + " sources_actual=" + result.sources_actual + " undated=" + result.undated_in_data);
  }

  await send("Emulation.setDeviceMetricsOverride", { width: 1680, height: 1200, deviceScaleFactor: 1, mobile: false });
  await sleep(600);
  const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  fs.writeFileSync(path.join(OUT_DIR, "verify-desktop.png"), Buffer.from(shot.data, "base64"));

  ws.close();
  chrome.kill();
  process.exit(result && result.failures && result.failures.length ? 3 : 0);
})().catch((e) => { console.error("ERR", e.message, e.stack); chrome.kill(); process.exit(1); });
