/* 身份标牌解析的回归测试。
 *
 * 目的：system_reminder 里「谁」的格式有两处来源、且格式不同，
 *       正则很容易看着对、实际错。这里抽的是 index.html 里【真实的】
 *       parseIdentity / stripIdentity / idChip 来跑，不是手抄副本。
 *
 * 跑：node 测试-身份解析.js
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

const consts = html.match(/const _RE_(?:UID|NICK|GROUP)\s*=\s*\/[^\n]+/g);
if (!consts || consts.length !== 3) {
  throw new Error("只抓到 " + (consts ? consts.length : 0) + " 个 _RE_ 常量");
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const src =
  consts.join("\n") +
  "\n" +
  ["parseIdentity", "stripIdentity", "idChip"].map(grabFn).join("\n");

const { parseIdentity, stripIdentity } = new Function(
  "esc",
  src + "\nreturn { parseIdentity, stripIdentity, idChip };",
)(esc);

/* 全部取自真实产物，不是编的 */
const CASES = [
  [
    "标准两行（identifier 开）",
    "User ID: 100000001, Nickname: 示例用户\nCurrent datetime: 2026-11-11 10:30 (CST), Weekday: Sunday",
  ],
  [
    "带群名三行（group_name_display 开）",
    "User ID: 100000001, Nickname: 示例用户\nGroup name: 示例群聊\nCurrent datetime: 2026-11-11 10:30 (CST), Weekday: Sunday",
  ],
  ["MessageMember.__str__ 的逗号紧贴版", "User ID: 100000001,Nickname: 示例用户"],
  [
    "昵称缺失",
    "User ID: 100000001, Nickname: N/A\nCurrent datetime: 2026-11-11 10:30",
  ],
  [
    "只有时间（identifier 关掉）",
    "Current datetime: 2026-11-11 10:30 (CST), Weekday: Sunday",
  ],
  ["空字符串", ""],
  ["undefined", undefined],
];

let bad = 0;
for (const [label, input] of CASES) {
  const who = parseIdentity(input);
  const rest = stripIdentity(input);
  console.log("── " + label);
  console.log("   输入  : " + JSON.stringify(input));
  console.log("   身份  : " + JSON.stringify(who));
  console.log("   剩余  : " + JSON.stringify(rest));
  // 身份抽到之后就不该再出现在剩余文本里，否则页面上会显示两遍
  if (who && who.nick && rest.includes(who.nick)) {
    console.log("   ❌ 昵称在剩余文本里重复出现");
    bad++;
  }
  if (who && who.id && rest.includes(who.id)) {
    console.log("   ❌ QQ号在剩余文本里重复出现");
    bad++;
  }
}
console.log(bad === 0 ? "\n全部样例通过：无重复、无残留" : "\n有 " + bad + " 处问题");
process.exit(bad === 0 ? 0 : 1);
