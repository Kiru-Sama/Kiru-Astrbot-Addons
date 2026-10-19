/* 思考块「展开后被重绘收回」的回归测试。
 *
 * 现象（用户报的）：监控页面里点开「🧠 思考过程」，刚点开就被收回。
 *
 * 根因（不是"刷新太频繁"，而是"重绘不保留状态"）：
 *   /ncd-monitor/sysinfo 是【1 秒】轮询 → refreshSys → renderLive → paintLive，
 *   而 paintLive 每次都用 `body.innerHTML = thinkHtml + ...` 把整块【重建】。
 *   <details> 的 open 状态不会被浏览器保留，于是：
 *     · 生成中：open 是默认带的 → 看起来正常
 *     · 生成完：open 不带（刻意的"生成完收起"）→ 用户点开，1 秒后被下一拍收回
 *
 * 修法：把用户的开合选择记在 _thinkUserOpen（三态：null = 没表过态）。
 *
 * ★ 这里【不是手抄副本】：paintLive / rememberThink / currentLive / esc 全部
 *   按 测试-身份解析.js 的老办法从 pages/monitor/index.html 里【原样抽出来】跑。
 *
 * ★ 血的教训，别重蹈：
 *   1. 一开始用 node 的 `vm` 把整段 <script> 丢进沙箱跑。能跑通，
 *      但它会以 0xC0000005（访问违例）随机崩，而且【stdout 一点都留不下】——
 *      Node 对管道是异步写、stderr 才是同步，硬崩时 stdout 的缓冲全丢，
 *      只能看到一行 EXIT=-1073741819，完全不知道跑到哪。
 *      抽函数 + new Function 这条路没有这些问题，也算跟仓库既有约定一致。
 *   2. 也不要为了"无缓冲输出"去 `const console = {...}` 换掉 console：
 *      实测那样做同样会把 node 搞崩。
 *   3. ★ **本文件名不能太长。** 同样一份内容实测（各跑 5 遍）：
 *        测试-思考块.js         (9 字符) → 5/5 正常
 *        测试-思考展开.js        (10 字符) → 5/5 正常
 *        测试-思考块展开.js       (11 字符) → 5/5 正常   ← 就是本文件
 *        测试-思考块展开状态.js    (13 字符) → 5/5 崩 0xC0000005
 *      ASCII 名（ascii_probe.js）也 5/5 正常 —— 所以是 **SnowLuma 自带那个
 *      node.exe 对这些中文文件名有 bug**，跟脚本内容无关。
 *      以后给这个测试改名，先连跑 5 遍确认不崩。
 *
 * 跑：node 测试-思考块展开.js
 */

const fs = require("fs");
const path = require("path");

const HTML = path.join(__dirname, "pages", "monitor", "index.html");
const html = fs.readFileSync(HTML, "utf8");

/* ---- 从页面里抽真实源码 -------------------------------------------------- */
function grabFn(name) {
  const start = html.indexOf("function " + name + "(");
  if (start < 0) throw new Error("找不到函数: " + name);
  const end = html.indexOf("\n}", start);
  if (end < 0) throw new Error("找不到函数结尾: " + name);
  return html.slice(start, end + 2);
}

const escMatch = html.match(/const esc = [\s\S]*?\)\);/);
if (!escMatch) throw new Error("找不到 esc 的定义");

/* 页面里必须真的有这个三态变量与内联回调，否则下面的行为无从谈起 */
if (!/let _thinkUserOpen = null;/.test(html)) {
  throw new Error("页面里找不到 `let _thinkUserOpen = null;` —— 三态变量没了");
}
if (!/ontoggle="rememberThink\(this\)"/.test(html)) {
  throw new Error('思考块上没有挂 ontoggle="rememberThink(this)" —— 记不住用户操作');
}

const SRC = `
${escMatch[0]}

/* 页面的三态状态变量（声明照抄，每个 api 实例各有一份，互不串味） */
let _thinkUserOpen = null;
let _liveHidden = false, _livePrevDone = true, _lastLives = [], _liveSelKey = null,
    _queueOpen = false, _liveWasAtBottom = false, _lastGate = null;

/* 只桩掉与本用例无关的东西：门控胶囊 / 排队框 / 后台自刷新 */
function renderGateChips() {}
function renderQueueBox() {}
function nudgeContexts() {}

${grabFn("currentLive")}
${grabFn("rememberThink")}
${grabFn("paintLive")}

return {
  render(live) { _lastLives = [live]; paintLive(); },
  remember(open) { rememberThink({ open }); },
  html() { return document.getElementById("liveBody").innerHTML; },
};
`;

/* ---- 极简 DOM 桩 -------------------------------------------------------- */
function makeEl(id) {
  const el = {
    id, tagName: "DIV", innerHTML: "", textContent: "", hidden: false, open: false,
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
    scrollHeight: 100, scrollTop: 0, clientHeight: 100, clientWidth: 300,
    querySelector() { return null; }, querySelectorAll() { return []; },
    appendChild() {}, removeChild() {}, addEventListener() {}, removeEventListener() {},
  };
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

/* ---- 断言 --------------------------------------------------------------- */
const failures = [];
function check(label, cond, msg) {
  if (!cond) failures.push(label + ": " + msg);
}

/* ---- 造 live 对象（字段照后端真实返回的来）------------------------------ */
function mkLive(over) {
  return Object.assign(
    {
      key: "k1", mode: "chat", done: false, ok: true, sent: 1, label: "私聊",
      group_name: "", sender_name: "某人", chars: 12, reasoning_chars: 8,
      elapsed: 1.5, text: "正文", reasoning: "思考内容",
    },
    over || {}
  );
}

/* 思考块此刻是否带 open（只看 think 块那一段，别被别处的 open 干扰） */
function thinkOpenOf(htmlText) {
  const at = htmlText.indexOf('class="think"');
  if (at < 0) return null;
  return /\bopen\b/.test(htmlText.slice(at, at + 60));
}

/* =====================================================================
 *  用例 1：默认行为没被改坏
 * ===================================================================== */
console.log("===== 用例 1：默认（用户没表过态）=====");

let p = makeApi();
p.api.render(mkLive({ done: false }));
const a1 = thinkOpenOf(p.api.html());
console.log("  生成中           → open=" + a1);
check("用例1", a1 === true, "生成中默认应展开");

p = makeApi();
p.api.render(mkLive({ done: true }));
const a2 = thinkOpenOf(p.api.html());
console.log("  生成完           → open=" + a2);
check("用例1", a2 === false, "生成完后默认应收起（这是原本刻意的设计）");

/* =====================================================================
 *  用例 2：★ 核心 —— 用户点开后，重绘不能把它收回
 * ===================================================================== */
console.log("\n===== 用例 2：生成完后点开，重绘必须保持 =====");

p = makeApi();
p.api.render(mkLive({ done: true }));
check("用例2", thinkOpenOf(p.api.html()) === false, "前提：生成完后默认是收起的");

p.api.remember(true);                       // 用户点开（内联 ontoggle 调的就是这个）
p.api.render(mkLive({ done: true }));
const b1 = thinkOpenOf(p.api.html());
console.log("  点开后第 1 次重绘  → open=" + b1);
check("用例2", b1 === true, "★点开后第 1 次重绘就被收回了");

for (let i = 0; i < 10; i++) p.api.render(mkLive({ done: true }));   // 模拟 1 秒轮询
const b2 = thinkOpenOf(p.api.html());
console.log("  再连续重绘 10 次   → open=" + b2);
check("用例2", b2 === true, "★连续重绘后又收回了");

// 负向对照：没点过的那一页必须仍是收起 —— 证明上面不是恒真
p = makeApi();
p.api.render(mkLive({ done: true }));
const b3 = thinkOpenOf(p.api.html());
console.log("  负向对照（没点过）→ open=" + b3);
check("用例2", b3 === false, "对照不成立：这个测试分辨不出有没有点过");

/* =====================================================================
 *  用例 3：用户主动收起，也要被尊重
 * ===================================================================== */
console.log("\n===== 用例 3：生成中主动收起 =====");

p = makeApi();
p.api.render(mkLive({ done: false }));
check("用例3", thinkOpenOf(p.api.html()) === true, "前提：生成中默认展开");

p.api.remember(false);
p.api.render(mkLive({ done: false }));
const c1 = thinkOpenOf(p.api.html());
console.log("  生成中收起后重绘  → open=" + c1);
check("用例3", c1 === false, "★用户收起后又被展开了");

p.api.render(mkLive({ done: true }));
const c2 = thinkOpenOf(p.api.html());
console.log("  随后生成完        → open=" + c2);
check("用例3", c2 === false, "用户收起过就不该被自动展开");

/* =====================================================================
 *  用例 4：用户表过态后，跨"下一代生成"依然生效
 * ===================================================================== */
console.log("\n===== 用例 4：跨一代生成仍然听用户的 =====");

p = makeApi();
p.api.render(mkLive({ done: true, reasoning: "第一代思考" }));
p.api.remember(true);

p.api.render(mkLive({ done: false, reasoning: "第二代思考" }));
const d1 = thinkOpenOf(p.api.html());
p.api.render(mkLive({ done: true, reasoning: "第二代思考" }));
const d2 = thinkOpenOf(p.api.html());
console.log(`  新一代 生成中 → open=${d1}   生成完 → open=${d2}`);
check("用例4", d1 === true, "新一代生成中该展开");
check("用例4", d2 === true, "★用户想看思考，换代后不该被收起");

/* =====================================================================
 *  用例 5：没有思考时不该冒出空的思考块
 * ===================================================================== */
console.log("\n===== 用例 5：没有思考内容 =====");
p = makeApi();
p.api.render(mkLive({ done: true, reasoning: "" }));
const e1 = thinkOpenOf(p.api.html());
console.log("  reasoning 为空 → 思考块 " + (e1 === null ? "不存在" : "存在（不该）"));
check("用例5", e1 === null, "没有思考时不该渲染思考块");
check("用例5", p.api.html().indexOf("思考过程") < 0, "没有思考时不该出现「思考过程」字样");

/* =====================================================================
 *  用例 6：思考块在正文之前（排版顺序不能被改掉）
 * ===================================================================== */
console.log("\n===== 用例 6：思考在前、正文在后 =====");
p = makeApi();
p.api.render(mkLive({ done: true, text: "这是正文", reasoning: "这是思考" }));
const all = p.api.html();
const iThink = all.indexOf("这是思考");
const iText = all.indexOf("这是正文");
console.log(`  顺序：思考@${iThink}  正文@${iText}`);
check("用例6", iThink >= 0 && iText >= 0 && iThink < iText, "思考块应在正文之前");

/* =====================================================================
 *  判定
 * ===================================================================== */
console.log("\n===== 判定 =====");
if (failures.length) {
  console.log("失败项:");
  for (const f of failures) console.log("  ✗ " + f);
  process.exit(1);
}
console.log("PASS: 默认（生成中展开/生成完收起）/ 点开后重绘保持（含连续 10 次轮询）/ "
  + "主动收起被尊重 / 跨代仍听用户的 / 无思考不渲染空块 / 思考在前 全部正确");
