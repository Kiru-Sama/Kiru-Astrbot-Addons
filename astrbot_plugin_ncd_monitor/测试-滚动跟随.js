/* 「最近一次输出」区块的滚动跟随回归测试。
 *
 * 现象（用户报的）："一直往下拽，我都看不了上面的东西"。
 *
 * 根因：paintLive 里那句
 *       if (atBottom || _liveWasAtBottom) body.scrollTop = body.scrollHeight;
 *   —— `_liveWasAtBottom` 初始化为 true，**全文只有声明和读取两处，从来没被赋值过**。
 *   所以条件恒为 true：每秒重绘一次就把视口拽回底部，用户没法往上翻。
 *   注释里写着"贴在底部才自动跟随，不打断用户往上翻"，意图对，闩是死的。
 *
 * 修法：真正的闩 _liveFollow
 *   · 容器上挂 scroll 监听，按"离底部多远"重新咬合/松开；
 *   · 不跟随时要【把 scrollTop 还回去】—— 重建 innerHTML 会把位置打回 0，
 *     只"不拽"是不够的，否则会直接跳到最上面，一样没法看；
 *   · 新的一次生成 / 切换并发路 → 重新咬合。
 *
 * ★ 不是手抄副本：paintLive / _liveOnScroll / currentLive / esc 全部按
 *   测试-身份解析.js 的老办法从 pages/monitor/index.html 里原样抽出来跑。
 * ★ 文件名别超 11 字符（SnowLuma 那个 node.exe 对长中文名会崩 0xC0000005，
 *   详见 测试-思考块展开.js 头注释）。
 *
 * 跑：node 测试-滚动跟随.js
 */

const fs = require("fs");
const path = require("path");

const HTML = path.join(__dirname, "pages", "monitor", "index.html");
const html = fs.readFileSync(HTML, "utf8");

function grabFn(name) {
  const start = html.indexOf("function " + name + "(");
  if (start < 0) throw new Error("找不到函数: " + name);
  const end = html.indexOf("\n}", start);
  if (end < 0) throw new Error("找不到函数结尾: " + name);
  return html.slice(start, end + 2);
}

const escMatch = html.match(/const esc = [\s\S]*?\)\);/);
if (!escMatch) throw new Error("找不到 esc 的定义");

/* 源码层面的护栏：别再退回那个死变量 */
if (/_liveWasAtBottom/.test(html) && !/原本叫 _liveWasAtBottom/.test(html)) {
  throw new Error("页面里又出现了 _liveWasAtBottom —— 那个变量从来没被赋值过，别用");
}
if (!/let _liveFollow = true;/.test(html)) {
  throw new Error("页面里找不到 `let _liveFollow = true;` —— 跟随闩没了");
}
if (!/addEventListener\("scroll", _liveOnScroll/.test(html)) {
  throw new Error("没给 liveBody 挂 scroll 监听 —— 闩永远不会松开");
}

const SRC = `
${escMatch[0]}

let _liveFollow = true;
let _thinkUserOpen = null;
let _liveHidden = false, _livePrevDone = true, _lastLives = [], _liveSelKey = null,
    _queueOpen = false, _lastGate = null;

function renderGateChips() {}
function renderQueueBox() {}
function nudgeContexts() {}

${grabFn("currentLive")}
${grabFn("_liveOnScroll")}
${grabFn("paintLive")}

return {
  render(live) { _lastLives = [live]; paintLive(); },
  userScrollTo(v) { document.getElementById("liveBody").scrollTop = v; _liveOnScroll(); },
  follow() { return _liveFollow; },
  top() { return document.getElementById("liveBody").scrollTop; },
  html() { return document.getElementById("liveBody").innerHTML; },
};
`;

/* ---- DOM 桩：重点是把 scrollTop 做成真滚动器的样子 ---------------------- */
function makeEl(id) {
  const el = {
    id, tagName: "DIV", textContent: "", hidden: false, open: false,
    value: "", checked: false,
    style: { setProperty() {}, removeProperty() {} },
    dataset: {},
    classList: {
      _s: new Set(),
      toggle(c, on) {
        if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); }
        else if (on) { this._s.add(c); } else { this._s.delete(c); }
      },
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    // 内容比可视区高：scrollHeight 1000 / clientHeight 300
    scrollHeight: 1000, clientHeight: 300, clientWidth: 300,
    querySelector() { return null; }, querySelectorAll() { return []; },
    appendChild() {}, removeChild() {}, addEventListener() {}, removeEventListener() {},
  };
  // ★ 真浏览器里把 innerHTML 整个换掉会把 scrollTop 打回 0（右栏上下文列表
  //   为此专门做了 keepScroll）。这里照这个行为模拟，否则测不出"位置还原"那半。
  let _inner = "";
  let _top = 0;
  Object.defineProperty(el, "innerHTML", {
    get() { return _inner; },
    set(v) { _inner = String(v); _top = 0; },
  });
  Object.defineProperty(el, "scrollTop", {
    get() { return _top; },
    set(v) { _top = Number(v) || 0; },
  });
  return el;
}

function makeApi() {
  const els = new Map();
  const documentStub = {
    getElementById(id) {
      if (!els.has(id)) els.set(id, makeEl(id));
      return els.get(id);
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    createElement(tag) { const e = makeEl("_" + tag); e.tagName = String(tag).toUpperCase(); return e; },
    addEventListener() {}, removeEventListener() {},
  };
  const api = new Function("document", SRC)(documentStub);
  return { api, els };
}

const failures = [];
function check(label, cond, msg) {
  if (!cond) failures.push(label + ": " + msg);
}

function mkLive(over) {
  return Object.assign(
    {
      key: "k1", mode: "chat", done: false, ok: true, sent: 1, label: "私聊",
      group_name: "", sender_name: "某人", chars: 12, reasoning_chars: 0,
      elapsed: 1.5, text: "正文", reasoning: "",
    },
    over || {}
  );
}

const BOTTOM = 1000;   // scrollHeight：贴底就是滚到 1000
const UP = 500;        // 往上翻的位置（离底 200 > 24 阈值）

/* =====================================================================
 *  用例 1：默认 = 跟随
 * ===================================================================== */
console.log("===== 用例 1：默认跟随 =====");
let p = makeApi();
p.api.render(mkLive({ done: true }));
const a = p.api.top();
console.log(`  首次重绘 → scrollTop=${a}（应=${BOTTOM}）`);
check("用例1", a === BOTTOM, `默认应贴底，实际 ${a}`);
check("用例1", p.api.follow() === true, "默认应为跟随");

/* =====================================================================
 *  用例 2：★ 核心 —— 用户往上翻之后，重绘不能把他拽回去
 * ===================================================================== */
console.log("\n===== 用例 2：往上翻之后必须稳住 =====");
p.api.userScrollTo(UP);                 // 用户自己往上滚
console.log(`  用户上滚到 ${UP} → follow=${p.api.follow()}（应=false）`);
check("用例2", p.api.follow() === false, "★往上滚了还不松开");

p.api.render(mkLive({ done: true }));
const b1 = p.api.top();
console.log(`  第 1 次重绘 → scrollTop=${b1}（应=${UP}）`);
check("用例2", b1 === UP, `★重绘把用户拽走了：期望 ${UP}，实际 ${b1}`);

for (let i = 0; i < 10; i++) p.api.render(mkLive({ done: true, text: "正文" + i }));
const b2 = p.api.top();
console.log(`  再连续重绘 10 次 → scrollTop=${b2}（应=${UP}）`);
check("用例2", b2 === UP, `★连续重绘后又把用户拽走了：实际 ${b2}`);
check("用例2", p.api.follow() === false, "稳住之后不该自己变回跟随");

/* =====================================================================
 *  用例 3：负向对照 —— 没往上翻过的，必须还在贴底跟着
 *    没有这一条，用例 2 可能是"永远不跟随"的假通过
 * ===================================================================== */
console.log("\n===== 用例 3：负向对照（没上翻过）=====");
p = makeApi();
p.api.render(mkLive({ done: false, text: "第一段" }));
const c1 = p.api.top();
p.api.render(mkLive({ done: false, text: "第一段第二段" }));
const c2 = p.api.top();
console.log(`  两次重绘 scrollTop = ${c1} / ${c2}（都应=${BOTTOM}）`);
check("用例3", c1 === BOTTOM && c2 === BOTTOM, "没往上翻时必须一直贴底");
check("用例3", p.api.follow() === true, "没往上翻时 follow 应保持 true");

/* =====================================================================
 *  用例 4：滚回底部 → 重新咬合
 * ===================================================================== */
console.log("\n===== 用例 4：滚回底部重新跟随 =====");
p.api.userScrollTo(UP);
p.api.render(mkLive({ done: true }));
check("用例4", p.api.follow() === false, "前提：先松开");
p.api.userScrollTo(BOTTOM);             // 用户自己滚回底部
console.log(`  滚回底部 → follow=${p.api.follow()}（应=true）`);
check("用例4", p.api.follow() === true, "★滚回底部后应重新咬合");
p.api.render(mkLive({ done: true, text: "又长了一段" }));
const d = p.api.top();
console.log(`  之后重绘 → scrollTop=${d}（应=${BOTTOM}）`);
check("用例4", d === BOTTOM, "重新咬合后应继续贴底");

/* =====================================================================
 *  用例 5：24px 阈值边界
 * ===================================================================== */
console.log("\n===== 用例 5：阈值边界（<24 算贴底）=====");
// distance = 1000 - scrollTop - 300。scrollTop=677 → distance=23 → 贴底
p.api.userScrollTo(677);
console.log(`  scrollTop=677（离底 23）→ follow=${p.api.follow()}（应=true）`);
check("用例5", p.api.follow() === true, "离底 23 应仍算贴底");
// scrollTop=670 → distance=30 → 松开
p.api.userScrollTo(670);
console.log(`  scrollTop=670（离底 30）→ follow=${p.api.follow()}（应=false）`);
check("用例5", p.api.follow() === false, "离底 30 应松开");

/* =====================================================================
 *  用例 6：新的一次生成 → 重新咬合
 * ===================================================================== */
console.log("\n===== 用例 6：新一代生成重新跟随 =====");
p = makeApi();
p.api.render(mkLive({ done: true }));
p.api.userScrollTo(UP);
p.api.render(mkLive({ done: true }));
check("用例6", p.api.follow() === false, "前提：松开");

// 新的一代：done true → false
p.api.render(mkLive({ done: false, text: "新一代" }));
const e1 = p.api.follow();
const e2 = p.api.top();
console.log(`  新一次生成开始 → follow=${e1}（应=true） scrollTop=${e2}（应=${BOTTOM}）`);
check("用例6", e1 === true, "★新一次生成开始时应重新跟随");
check("用例6", e2 === BOTTOM, "重新跟随后应贴底");

/* =====================================================================
 *  判定
 * ===================================================================== */
console.log("\n===== 判定 =====");
if (failures.length) {
  console.log("失败项:");
  for (const f of failures) console.log("  ✗ " + f);
  process.exit(1);
}
console.log("PASS: 默认跟随 / 往上翻后连续 10 次重绘稳住 / 负向对照仍贴底 / "
  + "滚回底部重新咬合 / 24px 阈值边界 / 新一代生成重新跟随 全部正确");
