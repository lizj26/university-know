// 用最小 DOM/浏览器桩在 Node 里跑页面内联 JS，验证按需加载路径
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync("index.html", "utf8");
const js = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];

const dataMatch = html.match(/<script id="notice-data" type="application\/json">([\s\S]*?)<\/script>/);
const PAYLOAD = JSON.parse(dataMatch[1]);

let fetched = [];
const fetchLog = [];

const IDS = [...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]);

function makeEl(tag) {
  const el = {
    tagName: tag, children: [], dataset: {}, style: { setProperty(){} }, attrs: {},
    hidden: false, open: false, innerHTML: "", textContent: "", value: "", className: "",
    classList: { add(){}, remove(){}, contains(){return false;} },
    appendChild(c){ this.children.push(c); return c; },
    removeChild(c){ const i=this.children.indexOf(c); if(i>=0) this.children.splice(i,1); },
    setAttribute(k,v){ this.attrs[k]=v; },
    getAttribute(k){ return this.attrs[k]; },
    removeAttribute(k){ delete this.attrs[k]; },
    addEventListener(){}, removeEventListener(){},
    querySelector(){ return makeEl("div"); },
    querySelectorAll(){ return []; },
    closest(){ return null; },
    focus(){}, select(){}, scrollTo(){},
    showModal(){ this.open = true; }, close(){ this.open = false; },
    getBoundingClientRect(){ return { bottom: 0 }; },
    insertAdjacentHTML(){},
  };
  return el;
}

const elMap = {};
for (const id of IDS) elMap[id] = makeEl("div");

global.document = {
  documentElement: makeEl("html"),
  body: makeEl("body"),
  title: "",
  activeElement: null,
  getElementById(id){ return elMap[id] || (elMap[id] = makeEl("div")); },
  querySelector(sel){
    if (sel === ".app-shell") return makeEl("div");
    if (sel === "#campus-switch a[data-campus]") return null;
    if (sel === '[data-open="upcoming"]') return makeEl("button");
    return makeEl("div");
  },
  querySelectorAll(){ return []; },
  createElement(t){ return makeEl(t); },
  addEventListener(){},
  execCommand(){ return true; },
};

global.window = {
  matchMedia(){ return { matches: false }; },
  addEventListener(){},
  scrollTo(){},
  isSecureContext: true,
};
global.localStorage = { getItem(){ return null; }, setItem(){} };
global.location = { hash: "", href: "http://x/" };
global.navigator = { clipboard: null };
// 不要覆盖 setTimeout/clearTimeout：页面自己会用它们做防抖，而测试也要靠它们等待。
// 只把「定时器句柄」当作不可用的环境来处理即可。

// fetch 桩：真的去读磁盘上的分片，并记录请求路径
global.fetch = function (p) {
  fetchLog.push(p);
  const file = path.join(process.cwd(), p);
  return new Promise((resolve) => {
    fs.readFile(file, "utf8", (err, txt) => {
      if (err) return resolve({ ok: false, status: 404, json: () => Promise.reject(err) });
      resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(txt)) });
    });
  });
};

// 暴露内部状态用于断言。
// 页面脚本整体是一个 IIFE，内部变量从外面看不到，所以把 __EXPORT__(...) 注入到
// IIFE 的收尾之前，让导出语句和内部变量处在同一作用域。
const PAYLOAD_SRC = 'JSON.parse(document.getElementById("notice-data").textContent)';
const close = js.lastIndexOf("})();");
const injected = js.slice(0, close)
  .split(PAYLOAD_SRC).join("__PL__")
  + "\n  __EXPORT__(function(){ return allItems; }, campusItems, state, findItem, ensureCampus, switchCampus, showNotice, whenCampusLoaded);\n" +
  js.slice(close);

const WIRE = { document: global.document, window: global.window, fetch: global.fetch,
  localStorage: global.localStorage, location: global.location, navigator: global.navigator,
  setTimeout: global.setTimeout, clearTimeout: global.clearTimeout };

const probe = new Function(
  "__EXPORT__", "__PL__", "document", "window", "fetch", "localStorage", "location", "navigator", "setTimeout", "clearTimeout",
  injected
);

const api = {};
try {
  probe(function (all, items, st, find, ensure, sw, show, when) {
    api.getAll = all;                 // getter 函数
    api.getItems = items;             // campusItems 本身
    api.getState = function () { return st; };
    api.findItem = find;
    api.ensureCampus = ensure;
    api.switchCampus = sw;
    api.showNotice = show;
    api.whenCampusLoaded = when;
  }, PAYLOAD, WIRE.document, WIRE.window, WIRE.fetch, WIRE.localStorage,
    WIRE.location, WIRE.navigator, WIRE.setTimeout, WIRE.clearTimeout);
} catch (e) {
  console.error("probe 执行失败:", e && e.message);
  throw e;
}
module.exports = { api, fetchLog, PAYLOAD };

// --- 断言 ---
const settle = () => new Promise(r => setTimeout(r, 60));

const results = [];
function check(name, cond, extra) {
  results.push((cond ? "PASS  " : "FAIL  ") + name + (extra ? "  [" + extra + "]" : ""));
}

const idxCount = Object.values(PAYLOAD.campuses).reduce((a,c)=>a+c.items.length,0);
// 条数会随抓取与 90 天保留期变化，断言「索引与分片条数一致」而不是写死数字，
// 否则每次数据更新后测试都会假失败。
const shardCount = (k) => JSON.parse(fs.readFileSync(path.join("data/shard", k + ".json"), "utf8")).items
  .filter(it => {
    const ts = Date.parse(it.d) || 0;
    return !ts || ts >= Date.now() - 90 * 86400000;   // 与页面 RETENTION_CUTOFF 一致
  }).length;
const xjtuIdx = PAYLOAD.campuses.xjtu.items.length;
check("首屏索引非空且含三校", idxCount > 0 && ["zju","sjtu","xjtu"].every(k => PAYLOAD.campuses[k].items.length > 0), String(idxCount));
check("首屏默认校区 = xjtu", PAYLOAD.default_campus === "xjtu", PAYLOAD.default_campus);
check("索引条数 = 分片条数(xjtu)", xjtuIdx === shardCount("xjtu"), xjtuIdx + " / " + shardCount("xjtu"));

const first = PAYLOAD.campuses.xjtu.items[0];
check("索引条目不含正文 dt.tx", !(first.dt && first.dt.tx));
check("索引条目保留标题/来源/分类/时间",
  !!(first.t && first.s && first.c && first.d));

(async () => {
 try {
  // 启动后：默认校区(xjtu)的全量应已异步取回
  await settle();

  check("后台请求了默认校区分片", fetchLog.includes("data/shard/xjtu.json"), fetchLog.join(", "));

  const items = api.getItems();
  check("合并后条目数 = xjtu 分片条数", items.length === shardCount("xjtu"), items.length + " / " + shardCount("xjtu"));
  const withTx = items.filter(it => it.dt && it.dt.tx).length;
  check("合并后带回正文(tx)可搜索", withTx > 0, withTx + " 条有正文");

  const all = api.getAll();
  check("其他校区尚未加载(仍是轻量索引)",
    all.filter(i=>i._campus==="zju").length === shardCount("zju") &&
    all.filter(i=>i._campus==="zju").every(i => !(i.dt && i.dt.tx)));

  // 切校区 → 按需拉取
  api.switchCampus("zju");
  await settle();
  check("切到 zju 时请求了它的分片", fetchLog.includes("data/shard/zju.json"), fetchLog.join(", "));
  check("zju 合并后 = zju 分片条数", api.getItems().length === shardCount("zju"), api.getItems().length + " / " + shardCount("zju"));

  // 打开详情 → 按需拉 -d 分片。
  // 注意：切校区时加载的「全量分片」本身已含正文，所以真正验证按需取正文，
  // 要在该校区全量到位之前打开详情（模拟用户一进页面就点某条）。
  api.switchCampus("sjtu");
  const one = api.getItems()[0];                 // 此刻 sjtu 还只有轻量索引
  check("打开前该条没有正文", !(one.dt && one.dt.tx));
  api.showNotice(one);                           // 先触发详情请求
  await settle();
  check("打开后正文到位（不依赖全量分片）", !!(one.dt && one.dt.tx),
    (one.dt && one.dt.tx || "").slice(0, 30));
  check("打开详情时请求了详情分片", fetchLog.includes("data/shard/sjtu-d.json"), fetchLog.join(", "));
  
  // 重复打开不应重复请求
  const reqCount = fetchLog.filter(p=>p==="data/shard/sjtu-d.json").length;
  api.showNotice(api.getItems()[1]);
  await settle();
  check("详情分片只请求一次", fetchLog.filter(p=>p==="data/shard/sjtu-d.json").length === reqCount,
    fetchLog.join(", "));

  console.log(results.join("\n"));
  const failed = results.filter(r=>r.startsWith("FAIL"));
  console.log("\n" + (results.length - failed.length) + "/" + results.length + " 通过");
  process.exit(failed.length ? 1 : 0);
 } catch (e) {
  console.error("断言过程出错:", (e && e.stack) || e);
  process.exit(2);
 }
})();
