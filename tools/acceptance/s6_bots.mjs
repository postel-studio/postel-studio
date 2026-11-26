#!/usr/bin/env node
/*
 * S6 验收脚本：在**真实浏览器**里跑「左栏出现 Bot → 点开看 charter」。
 *
 * 为什么它不在 `tests/` 里：pytest 里没有浏览器，前端 DOM 行为只能靠文本断言
 * （06-pitfalls T9 的老问题）。这个脚本补的就是那一块：起真内核、开真 Chrome、
 * 点真 DOM、读真 DOM。
 *
 * **不进项目依赖**（裁决：允许入库，但不得进 pyproject.toml、不得引入前端构建链）。
 * 它只需要本机已安装的 puppeteer-core 与 Chrome；缺任何一样就**优雅跳过**并说明原因，
 * 退出码 0（跳过不是失败），所以它永远不会拖住 CI —— CI 里根本没跑它。
 *
 * 覆盖（D8 的 DOM 契约）：
 *   1. 左栏 Bots 区出现 builder（名字 / L1 徽标 / 摘要「尚未填写职责」）；
 *      （S8 起 `bot.list` 不再播种：这个 Bot 由剧本用「新建 Bot」表单建出来。）
 *   2. 点开看到 charter 全文 + 记忆目录**绝对路径**；
 *   3. 键盘 Enter 与 click 同一条路径；
 *   4. 切仓库两个面板一起清空；
 *   5. 断连（真 SIGKILL）两个面板回 idle 并清空；
 *   6. XSS：先往 charter 写 `<script>` 再点开 → 文本字面出现、**无 script 节点**；
 *   7. 无 console 错误 / 无未捕获异常。
 *
 * 用法（详见 tools/acceptance/README.md）：
 *
 *     bash scripts/check.sh                 # 先保证代码是绿的
 *     node tools/acceptance/s6_bots.mjs
 *
 * 退出码：0 = 全部通过，或「环境不具备」而跳过；1 = 有断言失败（输出 `CHECK <n> ✗ ...`）。
 */

import { execFile, spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileP = promisify(execFile);
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");
const CLI = join(REPO_ROOT, ".venv", "bin", "postel-studio");
const KERNEL_BOOT_SECONDS = 40;

// ---------------------------------------------------------------------------
// 环境探测：缺什么就跳过什么，并说清楚「为什么」——绝不静默地"绿"。
// ---------------------------------------------------------------------------

async function findPuppeteer() {
  const candidates = [
    "puppeteer-core",
    "/home/" + (process.env.USER || "") + "/.local/lib/node_modules/puppeteer-core",
    "/usr/lib/node_modules/puppeteer-core",
    "/usr/local/lib/node_modules/puppeteer-core",
  ];
  const require = createRequire(import.meta.url);
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch {
      /* 试下一个 */
    }
  }
  return null;
}

async function findChrome() {
  const candidates = [
    process.env.POSTEL_CHROME,
    process.env.CHROME_PATH,
    "/home/" + (process.env.USER || "") + "/.local/bin/google-chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      const { stdout } = await execFileP(candidate, ["--version"], { timeout: 20000 });
      return { path: candidate, version: stdout.trim() };
    } catch {
      /* 试下一个 */
    }
  }
  return null;
}

async function skip(reason) {
  process.stdout.write(`SKIP 浏览器验收未运行：${reason}\n`);
  process.stdout.write("      这不是失败——本脚本只在本机有 Chrome + puppeteer-core 时运行。\n");
  process.stdout.write("      安装方法见 tools/acceptance/README.md。\n");
  process.exit(0);
}

// ---------------------------------------------------------------------------
// 内核：真子进程 + 独立 POSTEL_HOME（绝不碰真实 ~/.postel，06-pitfalls T18）
// ---------------------------------------------------------------------------

async function startKernel(stateHome) {
  const proc = spawn(CLI, ["serve", "--port", "0"], {
    cwd: REPO_ROOT,
    env: { ...process.env, POSTEL_HOME: stateHome, HOME: stateHome },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  proc.stdout.on("data", (chunk) => (output += chunk));
  proc.stderr.on("data", (chunk) => (output += chunk));

  const deadline = Date.now() + KERNEL_BOOT_SECONDS * 1000;
  while (Date.now() < deadline) {
    const match = output.match(/地址\s+(http:\/\/127\.0\.0\.1:\d+)/);
    if (match) return { proc, url: match[1], output: () => output };
    if (proc.exitCode !== null) {
      throw new Error(`内核启动失败（退出码 ${proc.exitCode}）：\n${output}`);
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  proc.kill("SIGKILL");
  throw new Error(`内核 ${KERNEL_BOOT_SECONDS}s 内没打印地址：\n${output}`);
}

async function stopKernel(kernel) {
  if (kernel.proc.exitCode !== null || kernel.proc.signalCode !== null) {
    return; // 已经被杀掉（断连验收）：别等一个不会再来的 exit 事件
  }
  kernel.proc.kill("SIGTERM");
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 15000);
    kernel.proc.on("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

// ---------------------------------------------------------------------------
// 两个真仓库（每行一个提交，供 repo.bind 用）
// ---------------------------------------------------------------------------

async function gitIn(cwd, args) {
  return execFileP(
    "git",
    [
      "-C",
      cwd,
      "-c",
      "user.email=acceptance@example.invalid",
      "-c",
      "user.name=acceptance",
      "-c",
      "init.defaultBranch=main",
      ...args,
    ],
    { timeout: 30000 },
  );
}

async function makeRepo(base, name) {
  const repo = join(base, name);
  await mkdir(repo, { recursive: true });
  await execFileP("git", ["init", "-q", "--initial-branch=main", repo], { timeout: 30000 });
  await gitIn(repo, ["commit", "-q", "--allow-empty", "-m", "init"]);
  await writeFile(join(repo, "f.txt"), "one\n", "utf8");
  await gitIn(repo, ["add", "-A"]);
  await gitIn(repo, ["commit", "-q", "-m", "base"]);
  return repo;
}

// ---------------------------------------------------------------------------
// 页面读状态的小工具
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SNAPSHOT = () => {
  const text = (id) => document.getElementById(id).textContent;
  const visible = (id) => document.getElementById(id).offsetParent !== null;
  return {
    botsState: document.getElementById("bots").dataset.state,
    botsStateText: text("bots-state"),
    botsStateVisible: visible("bots-state"),
    botsError: text("bots-error"),
    botsErrorVisible: visible("bots-error"),
    botEmptyVisible: visible("bot-empty"),
    botRows: Array.from(document.querySelectorAll("#bot-list li[data-bot-id]")).map((li) => ({
      botId: li.dataset.botId,
      name: (li.querySelector(".bot-name") || {}).textContent || "",
      permission: (li.querySelector(".bot-permission") || {}).textContent || "",
      summary: (li.querySelector(".bot-charter-summary") || {}).textContent || "",
      selected: li.dataset.selected === "true",
      role: li.getAttribute("role"),
      tabindex: li.getAttribute("tabindex"),
    })),
    charterState: document.getElementById("charter").dataset.state,
    charterTitle: text("charter-title"),
    charterStateText: text("charter-state"),
    charterError: text("charter-error"),
    charterErrorVisible: visible("charter-error"),
    memory: text("charter-memory"),
    memoryVisible: visible("charter-memory"),
    content: text("charter-content"),
    contentVisible: visible("charter-content"),
    contentTag: document.getElementById("charter-content").tagName,
    contentScripts: document.querySelectorAll("#charter-content script").length,
    repoRows: Array.from(document.querySelectorAll("#repo-list li[data-repo-id]")).map((li) => ({
      repoId: li.dataset.repoId,
      name: (li.querySelector(".repo-name") || {}).textContent || "",
    })),
  };
};

async function snapshot(page) {
  return page.evaluate(SNAPSHOT);
}

async function bindPath(page, path) {
  await page.type("#bind-path", path);
  await page.click("#bind-button");
  await sleep(400);
}

async function waitBotsState(page, state) {
  await page.waitForFunction(
    (want) => document.getElementById("bots").dataset.state === want,
    { timeout: 15000 },
    state,
  );
  await sleep(150);
}

async function clickRepoRow(page, name) {
  await page.evaluate((rowName) => {
    const li = Array.from(document.querySelectorAll("#repo-list li[data-repo-id]")).find(
      (row) => ((row.querySelector(".repo-name") || {}).textContent || "") === rowName,
    );
    if (!li) throw new Error(`找不到仓库行：${rowName}`);
    li.click();
  }, name);
  await waitBotsState(page, "ready");
}

async function clickBotRow(page, index = 0) {
  await page.evaluate((want) => {
    const rows = document.querySelectorAll("#bot-list li[data-bot-id]");
    const li = rows[want];
    if (!li) throw new Error(`找不到第 ${want} 行 Bot`);
    li.click();
  }, index);
}

async function focusBotRow(page, index = 0) {
  await page.evaluate((want) => {
    const rows = document.querySelectorAll("#bot-list li[data-bot-id]");
    const li = rows[want];
    if (!li) throw new Error(`找不到第 ${want} 行 Bot`);
    li.focus();
  }, index);
}

async function waitCharterState(page, state) {
  await page.waitForFunction(
    (want) => document.getElementById("charter").dataset.state === want,
    { timeout: 15000 },
    state,
  );
  await sleep(150);
}

/**
 * 用「新建 Bot」表单建一个 Bot（S8 的用户路径）。
 *
 * S8 起 `bot.list` 不再惰性播种：任何"左栏要有一个 Bot"的剧本都得自己建一个。输入用
 * Ctrl+A + Backspace 清空，不用三击——headless 里三击不选中（S7 的实测，见 replaceBase）。
 */
async function createBot(page, name, permission) {
  await page.click("#bot-new");
  await page.waitForFunction(() => document.getElementById("bot-create-form").hidden === false, {
    timeout: 5000,
  });
  await page.click("#bot-create-name");
  await page.keyboard.down("Control");
  await page.keyboard.press("KeyA");
  await page.keyboard.up("Control");
  await page.keyboard.press("Backspace");
  await page.type("#bot-create-name", name);
  if (permission) await page.select("#bot-create-permission", permission);
  await page.click("#bot-create-submit");
  await page.waitForFunction(() => document.getElementById("bot-create-form").hidden === true, {
    timeout: 15000,
  });
  await page.waitForFunction(
    (want) => {
      if (document.getElementById("bots").dataset.state !== "ready") return false;
      return Array.from(document.querySelectorAll("#bot-list li .bot-name")).some(
        (node) => node.textContent === want,
      );
    },
    { timeout: 15000 },
    name,
  );
  await sleep(150);
}

let failures = 0;
function check(name, ok, detail) {
  if (!ok) failures += 1;
  process.stdout.write(
    `CHECK ${ok ? "✓" : "✗"} ${name}${detail !== undefined ? "  " + JSON.stringify(detail) : ""}\n`,
  );
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------

async function main() {
  const puppeteer = await findPuppeteer();
  if (!puppeteer) await skip("找不到 puppeteer-core（npm 包，本机没装）");
  const chrome = await findChrome();
  if (!chrome) await skip("找不到 Chrome / Chromium（可用 POSTEL_CHROME 指定路径）");
  if (!existsSync(CLI)) await skip(`找不到 ${CLI}（先在仓库里跑一次 \`uv sync\`）`);
  try {
    await execFileP(CLI, ["version"], { timeout: 20000 });
  } catch {
    await skip(`找不到可用的 ${CLI}（先在仓库里跑一次 \`uv sync\`）`);
  }

  const work = await mkdtemp(join(tmpdir(), "postel-s6-acceptance-"));
  const stateHome = join(work, "postel-home");
  const shots = join(work, "shots");
  await mkdir(stateHome, { recursive: true });
  await mkdir(shots, { recursive: true });

  process.stdout.write(`浏览器：${chrome.version}（${chrome.path}）\n`);
  process.stdout.write(`工作目录：${work}（截图与内核日志都在这）\n`);

  const repoMain = await makeRepo(work, "demo-main");
  const repoTwo = await makeRepo(work, "demo-two");
  const kernel = await startKernel(stateHome);
  const browser = await puppeteer.launch({
    executablePath: chrome.path,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1200, height: 900 });
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(String(error)));

  try {
    await page.goto(kernel.url, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.getElementById("repos").dataset.state === "ready",
      { timeout: 15000 },
    );

    // ---- 初始态：没选仓库 → 两个面板都是 idle，不装成「这个仓库没有 Bot」 ----
    const initial = await snapshot(page);
    check(
      "S6 初始 #bots 是 idle 且提示「选择一个仓库查看它的 Bot」",
      initial.botsState === "idle" &&
        initial.botsStateText.trim() === "选择一个仓库查看它的 Bot" &&
        initial.botsStateVisible,
      { state: initial.botsState, text: initial.botsStateText },
    );
    check(
      "S6 初始 #charter 是 idle + 提示文案 + 全文/记忆目录隐藏",
      initial.charterState === "idle" &&
        initial.charterStateText.trim() === "点左侧的 Bot 查看它的 charter" &&
        !initial.memoryVisible &&
        !initial.contentVisible,
      { state: initial.charterState, text: initial.charterStateText },
    );
    check(
      "S6 初始 bot-list 为空、#bot-empty 不可见（没选仓库 ≠ 没有 Bot）",
      initial.botRows.length === 0 && !initial.botEmptyVisible,
      { rows: initial.botRows.length, emptyVisible: initial.botEmptyVisible },
    );

    // ---- 绑定两个真仓库 ----
    await bindPath(page, repoMain);
    await bindPath(page, repoTwo);
    await page.waitForFunction(
      () => document.querySelectorAll("#repo-list li[data-repo-id]").length === 2,
      { timeout: 15000 },
    );
    check("0 左栏两行仓库", (await page.evaluate(() => document.querySelectorAll("#repo-list li[data-repo-id]").length)) === 2);

    // ---- 验收：点仓库 → 左栏 Bots 区出现 builder ----
    // S8 起没有惰性播种：先点「新建 Bot」建一个（这本身是 S8 的路径，S6 只借它当准备）。
    await clickRepoRow(page, "demo-main");
    const emptyBots = await snapshot(page);
    check(
      "0 S8 起空仓库真的没有 Bot（列表空 + 空态可见）",
      emptyBots.botRows.length === 0 && emptyBots.botEmptyVisible,
      { rows: emptyBots.botRows.length, empty: emptyBots.botEmptyVisible },
    );
    await createBot(page, "builder");
    const bots = await snapshot(page);
    const mainRepoId = bots.repoRows.find((row) => row.name === "demo-main").repoId;
    check("1 点仓库 → #bots data-state=ready", bots.botsState === "ready", bots.botsState);
    check(
      "1 恰好 1 行 Bot 且是 builder",
      bots.botRows.length === 1 && bots.botRows[0].name === "builder",
      bots.botRows,
    );
    check(
      "1 权限徽标文本是 L1",
      bots.botRows[0].permission === "L1",
      bots.botRows[0].permission,
    );
    check(
      "1 职责摘要 = 「尚未填写职责」（空 charter 的空态文案）",
      bots.botRows[0].summary === "尚未填写职责",
      bots.botRows[0].summary,
    );
    check(
      "1 Bot 行键盘可达（role=button + tabindex=0）",
      bots.botRows[0].role === "button" && bots.botRows[0].tabindex === "0",
      { role: bots.botRows[0].role, tabindex: bots.botRows[0].tabindex },
    );
    check("1 #bot-empty 不可见", !bots.botEmptyVisible);
    await page.screenshot({ path: join(shots, "1-bots-ready.png") });

    // ---- 验收 3：点开 → charter 全文 + 记忆目录绝对路径 ----
    await clickBotRow(page, 0);
    await waitCharterState(page, "ready");
    const charter = await snapshot(page);
    check("3 点 Bot → #charter data-state=ready", charter.charterState === "ready", charter.charterState);
    check("3 标题「Charter · builder」", charter.charterTitle.trim() === "Charter · builder", charter.charterTitle);
    check(
      "3 记忆目录 = 「记忆目录：<状态根>/bots/<repoId>/builder」（绝对路径，D9 不伪装 ~）",
      charter.memoryVisible &&
        charter.memory === `记忆目录：${stateHome}/bots/${mainRepoId}/builder`,
      { memory: charter.memory, expected: `记忆目录：${stateHome}/bots/${mainRepoId}/builder` },
    );
    check(
      "3 charter 全文含「# builder」与「## 职责」",
      charter.contentVisible && charter.content.includes("# builder") && charter.content.includes("## 职责"),
    );
    check(
      "3 全文是一个 <pre>（textContent，不是 HTML）且无 script 元素",
      charter.contentTag === "PRE" && charter.contentScripts === 0,
      { tag: charter.contentTag, scripts: charter.contentScripts },
    );
    await page.screenshot({ path: join(shots, "2-charter-ready.png") });

    // ---- 验收：切仓库 → 两个面板一起回 idle 并清空（不留旧 charter） ----
    await clickRepoRow(page, "demo-two");
    const switched = await snapshot(page);
    const twoRepoId = switched.repoRows.find((row) => row.name === "demo-two").repoId;
    check(
      "4 切到另一个仓库 → 它还没有 Bot（S8 起不再自动播种）",
      switched.botsState === "ready" &&
        switched.botRows.length === 0 &&
        switched.botEmptyVisible,
      { state: switched.botsState, rows: switched.botRows.length },
    );
    check(
      "4 切仓库 → #charter 回 idle、全文与记忆目录清空、标题回 Charter",
      switched.charterState === "idle" &&
        switched.content === "" &&
        !switched.contentVisible &&
        switched.memory === "" &&
        !switched.memoryVisible &&
        switched.charterTitle.trim() === "Charter",
      { state: switched.charterState, content: switched.content, title: switched.charterTitle },
    );
    await page.screenshot({ path: join(shots, "3-switched-repo.png") });

    // S8：另一个仓库也要有一个 Bot，后面几条（键盘、XSS、断连）才有对象可点。
    await createBot(page, "builder");
    const twoBots = await snapshot(page);
    check(
      "4 在 demo-two 建一个 builder → 左栏有它（仓库隔离：与 demo-main 的不是同一行）",
      twoBots.botRows.length === 1 &&
        twoBots.botRows[0].name === "builder" &&
        twoBots.botRows[0].botId.startsWith(`${twoRepoId}/`),
      twoBots.botRows,
    );

    // ---- 验收：键盘 Enter 与 click 同一条路径 ----
    await focusBotRow(page, 0);
    await page.keyboard.press("Enter");
    await waitCharterState(page, "ready");
    const byKeyboard = await snapshot(page);
    check("5 聚焦 Bot 行 + Enter → #charter ready", byKeyboard.charterState === "ready", byKeyboard.charterState);
    check(
      "5 键盘打开的 charter 是 demo-two 的记忆目录",
      byKeyboard.memory === `记忆目录：${stateHome}/bots/${twoRepoId}/builder`,
      byKeyboard.memory,
    );
    check(
      "5 键盘打开后该行有 data-selected=true",
      byKeyboard.botRows[0].selected === true,
      byKeyboard.botRows[0].selected,
    );
    await page.screenshot({ path: join(shots, "4-keyboard.png") });

    // ---- 验收 6：XSS —— 先往 charter 写 <script> 再点开，必须只是文本 ----
    const malicious =
      "# builder\n\n## 职责\n\n<script>alert(1)</script>\n\n## 边界\n\n<!-- 注：不是 HTML -->\n";
    await writeFile(
      join(stateHome, "bots", twoRepoId, "builder", "charter.md"),
      malicious,
      "utf8",
    );
    await clickBotRow(page, 0);
    await waitCharterState(page, "ready");
    const xss = await snapshot(page);
    check(
      "6 charter 里的 <script> 不生成 script 元素",
      xss.contentScripts === 0,
      xss.contentScripts,
    );
    check(
      "6 <script> 作为字面文本出现在全文里",
      xss.content.includes("<script>alert(1)</script>"),
    );
    await page.screenshot({ path: join(shots, "5-xss.png") });

    // ---- 验收 7：console 错误（断连前）----
    check(
      "7 断连前无 console 错误、无未捕获异常",
      consoleErrors.length === 0 && pageErrors.length === 0,
      { consoleErrors, pageErrors },
    );

    // ---- 验收：断连（真 SIGKILL）→ 两个面板回 idle 并清空 ----
    kernel.proc.kill("SIGKILL");
    await page.waitForFunction(
      () =>
        document.getElementById("bots").dataset.state === "idle" &&
        document.getElementById("charter").dataset.state === "idle",
      { timeout: 15000 },
    );
    await sleep(200);
    const offline = await snapshot(page);
    check("8 断连 → #bots 回 idle", offline.botsState === "idle", offline.botsState);
    check(
      "8 断连文案「已断开：Bot 列表未知」",
      offline.botsStateText.includes("已断开：Bot 列表未知"),
      offline.botsStateText,
    );
    check("8 断连 → bot-list 清空", offline.botRows.length === 0, offline.botRows.length);
    check(
      "8 断连 → #charter 回 idle、全文与记忆目录清空",
      offline.charterState === "idle" &&
        offline.content === "" &&
        !offline.contentVisible &&
        offline.memory === "" &&
        !offline.memoryVisible,
      { state: offline.charterState, content: offline.content },
    );
    check("8 断连后无未捕获 JS 异常", pageErrors.length === 0, pageErrors);
    await page.screenshot({ path: join(shots, "6-disconnected.png") });
  } finally {
    await browser.close().catch(() => {});
    await stopKernel(kernel);
    const logPath = join(work, "kernel.log");
    await writeFile(logPath, kernel.output(), "utf8");
    process.stdout.write(`内核日志：${logPath}\n截图目录：${shots}\n`);
    // 05 §4.1：ticket 不得进内核日志（S2 的回归点，这里顺手再看一眼）
    if (/WebSocket.*[?]ticket=(?!<REDACTED>)/.test(kernel.output())) {
      failures += 1;
      process.stdout.write("CHECK ✗ 内核日志里出现了未脱敏的 ticket\n");
    }
    // 记忆只在状态根下：两个用户仓库都不该被内核写东西（S3 验收 5 / S6 D9 的同一纪律）
    for (const repo of [repoMain, repoTwo]) {
      const status = await gitIn(repo, ["status", "--porcelain"]).catch(() => null);
      if (status && status.stdout.trim() !== "") {
        failures += 1;
        process.stdout.write(`CHECK ✗ 内核往用户仓库里写了东西：${repo}\n${status.stdout}\n`);
      }
    }
    await rm(repoMain, { recursive: true, force: true });
    await rm(repoTwo, { recursive: true, force: true });
  }

  if (failures > 0) {
    process.stdout.write(`\n❌ ${failures} 项失败\n`);
    process.exit(1);
  }
  process.stdout.write("\n✅ 浏览器验收全部通过（S6 D8：Bots 区 / charter 全文与记忆路径 / 键盘 / 切仓库清空 / XSS / 断连清空）\n");
}

main().catch((error) => {
  process.stdout.write(`\n❌ 剧本崩溃：${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
