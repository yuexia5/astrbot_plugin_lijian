// 用最小 DOM stub 跑一遍 app.js，检查初始化不抛错、图片地址拼得对。
const fs = require("fs");
const vm = require("vm");

function makeEl(id) {
  const el = {
    id,
    hidden: false,
    value: "",
    textContent: "",
    innerHTML: "",
    readOnly: false,
    files: null,
    dataset: {},
    style: { setProperty() {} },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {},
    removeEventListener() {},
    appendChild() {},
    remove() {},
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 100 }),
    focus() {},
    select() {},
    click() {},
    closest: () => null,
    setAttribute() {},
  };
  return el;
}

const els = new Map();
const document = {
  getElementById(id) {
    if (!els.has(id)) els.set(id, makeEl(id));
    return els.get(id);
  },
  createElement: () => makeEl("created"),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  body: { classList: { add() {}, remove() {} }, appendChild() {} },
};

const calls = [];
const bridge = {
  ready: async () => {},
  apiGet: async (endpoint) => {
    calls.push(["get", endpoint]);
    if (endpoint === "status") {
      return {
        diary_template: "**天气**：\n{date}",
        api_base: "/api/v1/plugins/extensions/astrbot_plugin_lijian",
      };
    }
    if (endpoint === "signature") return { signature: "1|x|0" };
    if (endpoint === "notes") return { notes: [] };
    if (endpoint === "stats") return { notes: 0, words: 0 };
    return {};
  },
  apiPost: async () => ({}),
  upload: async () => ({ url: "media/" + "a".repeat(32) + ".png" }),
  download: async () => {},
  onContext() {},
};

const sandbox = {
  document,
  window: {
    AstrBotPluginPage: bridge,
    matchMedia: () => ({ matches: false }),
    innerWidth: 1200,
    innerHeight: 800,
    addEventListener() {},
    confirm: () => true,
    location: { pathname: "/", origin: "http://localhost" },
  },
  location: {
    pathname:
      "/api/v1/plugins/extensions/astrbot_plugin_lijian/pages/bookshelf/index.html",
    origin: "http://localhost",
  },
  navigator: { clipboard: { writeText: async () => {} } },
  CSS: { escape: (value) => value },
  requestAnimationFrame: (fn) => fn(),
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  console,
  Blob: class {},
  URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
};
sandbox.globalThis = sandbox;

const target = process.argv[2] || require("path").join(__dirname, "pages/bookshelf/app.js");
const source = fs.readFileSync(target, "utf8");
const code =
  source + "\n;globalThis.__probe = { safeImg, mediaUrl, renderMarkdown, splitTagList };";

let failed = 0;
function check(name, ok, detail) {
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " — " + detail : ""}`);
  if (!ok) failed += 1;
}

const context = vm.createContext(sandbox);
try {
  vm.runInContext(code, context, { filename: "app.js" });
  check("app.js 执行", true);
} catch (error) {
  check("app.js 执行", false, error.message);
  process.exit(1);
}

// init 是异步的，给它一轮微任务
setTimeout(() => {
  try {
    const probe = sandbox.__probe;
    check("status 被请求", calls.some(([m, e]) => m === "get" && e === "status"));
    check("notes 被请求", calls.some(([m, e]) => m === "get" && e === "notes"));

    const name = "a".repeat(32) + ".png";
    const url = probe.mediaUrl(name);
    check(
      "mediaUrl 拼绝对路径",
      url === "/api/v1/plugins/extensions/astrbot_plugin_lijian/media/" + name,
      url
    );

    const rendered = probe.safeImg("图", "media/" + name);
    check("safeImg 渲染本地图片", rendered.includes("<img") && rendered.includes(url), rendered);

    const evil = probe.safeImg("坏图", "media/../../etc/passwd");
    check("safeImg 拒绝路径穿越", !evil.includes("<img"), evil);

    const external = probe.safeImg("外链", "https://example.com/a.png");
    check("safeImg 保留外链", external.includes("https://example.com/a.png"));

    const md = probe.renderMarkdown("# 标题\n\n![图](media/" + name + ")\n\n正文");
    check("markdown 渲染本地图片", md.includes("<img") && md.includes("/media/" + name), md.slice(0, 120));

    check(
      "标签中英文分隔",
      JSON.stringify(probe.splitTagList("日记，日常、随手;tag")) ===
        JSON.stringify(["日记", "日常", "随手", "tag"]),
      JSON.stringify(probe.splitTagList("日记，日常、随手;tag"))
    );

    console.log(failed === 0 ? "\n全部通过" : `\n失败 ${failed} 项`);
    process.exit(failed === 0 ? 0 : 1);
  } catch (error) {
    console.log("[FAIL] 后置检查 —", error.stack);
    process.exit(1);
  }
}, 60);
