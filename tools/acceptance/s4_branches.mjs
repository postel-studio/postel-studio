#!/usr/bin/env node
/*
 * S4 验收脚本：在**真实浏览器**里跑「点左栏仓库行 → 主区出现分支列表」。
 *
 * 为什么它不在 `tests/` 里：pytest 里没有浏览器，前端 DOM 行为只能靠文本断言
 * （06-pitfalls T9 的老问题）。这个脚本补的就是那一块：起真内核、开真 Chrome、
 * 点真 DOM、读真 DOM。
 *
 * **不进项目依赖**（裁决：允许入库，但不得进 pyproject.toml、不得引入前端构建链）。
 * 它只需要本机已安装的 puppeteer-core 与 Chrome；缺任何一样就**优雅跳过**并说明原因，
 * 退出码 0（跳过不是失败），所以它永远不会拖住 CI —— CI 里根本没跑它。
 *
 * 用法（详见 tools/acceptance/README.md）：
 *
 *     bash scripts/check.sh                 # 先保证代码是绿的
 *     node tools/acceptance/s4_branches.mjs
 *
 * 退出码：
 *   0 = 全部通过，或「环境不具备」而跳过（会写明原因）
 *   1 = 有断言失败（输出形如 `CHECK 2 ✗ ...`，并保留截图目录）
 */

import { execFile, spawn } from "node:child_process";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
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
  // 1) 项目里（如果将来有人 npm i --no-save），2) 本机全局安装（当前机器的实际情况）。
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
    if (proc.exitCode !== null) throw new Error(`内核启动失败（退出码 ${proc.exitCode}）：\n${output}`);
    await new Promise((r) => setTimeout(r, 100));
  }
  proc.kill("SIGKILL");
  throw new Error(`内核 ${KERNEL_BOOT_SECONDS}s 内没打印地址：\n${output}`);
}

async function stopKernel(kernel, signal = "SIGTERM") {
  if (kernel.proc.exitCode === null && kernel.proc.signalCode === null) {
    kernel.proc.kill(signal);
  }
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 15000);
    kernel.proc.on("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

// ---------------------------------------------------------------------------
// 两个真仓库：A = main + feature（main 有 upstream），B = `git init` 之后不 commit
// ---------------------------------------------------------------------------

async function gitIn(cwd, args, { allowFail = false } = {}) {
  try {
    return await execFileP(
      "git",
      ["-C", cwd, "-c", "user.email=acceptance@example.invalid",
        "-c", "user.name=acceptance", "-c", "init.defaultBranch=main", ...args],
      { timeout: 30000 },
    );
  } catch (error) {
    if (allowFail) return { stdout: "", stderr: String(error) };
    throw error;
  }
}

async function makeBranchedRepo(base, name) {
  const repo = join(base, name);
  const remote = join(base, `${name}-remote.git`);
  await mkdir(repo, { recursive: true });
  await execFileP("git", ["init", "-q", "--bare", remote], { timeout: 30000 });
  await execFileP("git", ["init", "-q", "--initial-branch=main", repo], { timeout: 30000 });
  await gitIn(repo, ["remote", "add", "origin", remote]);
  await gitIn(repo, ["commit", "-q", "--allow-empty", "-m", "init"]);
  await gitIn(repo, ["branch", "feature"]);
  await gitIn(repo, ["push", "-q", "-u", "origin", "main"]);
  return repo;
}

async function makeEmptyRepo(base, name) {
  const repo = join(base, name);
  await mkdir(repo, { recursive: true });
  await execFileP("git", ["init", "-q", "--initial-branch=main", repo], { timeout: 30000 });
  return repo;
}

// ---------------------------------------------------------------------------
// 页面读状态的小工具
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SNAPSHOT = () => ({
  state: document.getElementById("branches").dataset.state,
  // 用 textContent：h2 上的 text-transform: uppercase 会让 innerText 变大写（视觉层，不是 DOM 文本）
  title: document.getElementById("branches-title").textContent,
  stateText: document.getElementById("branches-state").innerText,
  stateVisible: document.getElementById("branches-state").offsetParent !== null,
  error: document.getElementById("branches-error").innerText,
  errorVisible: document.getElementById("branches-error").offsetParent !== null,
  emptyText: document.getElementById("branch-empty").innerText,
  emptyVisible: document.getElementById("branch-empty").offsetParent !== null,
  listVisible: document.getElementById("branch-list").offsetParent !== null,
  rows: Array.from(document.querySelectorAll("#branch-list li")).map((li) => ({
    name: li.dataset.branchName,
    text: li.innerText.replace(/\s+/g, " ").trim(),
    sha: (li.querySelector(".branch-sha") || {}).textContent || "",
    current: (li.querySelector(".branch-current") || {}).textContent || "",
    upstream: (li.querySelector(".branch-upstream") || {}).textContent || "",
  })),
});

const REPO_ROWS = () =>
  Array.from(document.querySelectorAll("#repo-list li[data-repo-id]")).map((li) => ({
    id: li.dataset.repoId,
    name: (li.querySelector(".repo-name") || {}).textContent || "",
    selected: li.dataset.selected === "true",
    role: li.getAttribute("role"),
    tabindex: li.getAttribute("tabindex"),
  }));

async function snapshot(page) {
  return page.evaluate(SNAPSHOT);
}

async function repoRows(page) {
  return page.evaluate(REPO_ROWS);
}

async function bindPath(page, path) {
  await page.type("#bind-path", path);
  await page.click("#bind-button");
  await sleep(600);
}

async function waitBranchesReady(page) {
  await page.waitForFunction(
    () => document.getElementById("branches").dataset.state === "ready",
    { timeout: 15000 },
  );
  await sleep(200);
}

async function clickRepo(page, name) {
  await page.evaluate((rowName) => {
    const li = Array.from(document.querySelectorAll("#repo-list li[data-repo-id]"))
      .find((row) => ((row.querySelector(".repo-name") || {}).textContent || "") === rowName);
    if (!li) throw new Error(`找不到仓库行：${rowName}`);
    li.click();
  }, name);
  await waitBranchesReady(page);
}

let failures = 0;
function check(name, ok, detail) {
  if (!ok) failures += 1;
  process.stdout.write(`CHECK ${ok ? "✓" : "✗"} ${name}${detail !== undefined ? "  " + JSON.stringify(detail) : ""}\n`);
}

const SHA8 = /^[0-9a-f]{8}$/;

// ---------------------------------------------------------------------------
// 主流程：验收 1–5 + D6（空态 / 切回刷新 / 键盘可达 / 断连回 idle）
// ---------------------------------------------------------------------------

async function main() {
  const puppeteer = await findPuppeteer();
  if (!puppeteer) await skip("找不到 puppeteer-core（npm 包，本机没装）");
  const chrome = await findChrome();
  if (!chrome) await skip("找不到 Chrome / Chromium（可用 POSTEL_CHROME 指定路径）");
  try {
    await execFileP(CLI, ["version"], { timeout: 20000 });
  } catch {
    await skip(`找不到可用的 ${CLI}（先在仓库里跑一次 \`uv sync\`）`);
  }

  const work = await mkdtemp(join(tmpdir(), "postel-s4-acceptance-"));
  const stateHome = join(work, "postel-home");
  const shots = join(work, "shots");
  await mkdir(stateHome, { recursive: true });
  await mkdir(shots, { recursive: true });

  process.stdout.write(`浏览器：${chrome.version}（${chrome.path}）\n`);
  process.stdout.write(`工作目录：${work}（截图与内核日志都在这）\n`);

  const repoA = await makeBranchedRepo(work, "demo-main");
  const repoB = await makeEmptyRepo(work, "demo-empty");
  const kernel = await startKernel(stateHome);
  const browser = await puppeteer.launch({
    executablePath: chrome.path,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1200, height: 900 });

  try {
    await page.goto(kernel.url, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.getElementById("repos").dataset.state === "ready",
      { timeout: 15000 },
    );

    // 初始态：没选仓库 → #branches 是 idle，不显示空态也不显示错误（D6）
    const initial = await snapshot(page);
    check("D6 初始 idle", initial.state === "idle", initial.state);
    check("D6 初始文案「从左侧选择一个仓库」", initial.stateText.includes("从左侧选择一个仓库"), initial.stateText);
    check("D6 初始不显示空态/错误", !initial.emptyVisible && !initial.errorVisible);

    // 绑定两个真仓库
    await bindPath(page, repoA);
    await bindPath(page, repoB);
    await page.waitForFunction(
      () => document.querySelectorAll("#repo-list li[data-repo-id]").length === 2,
      { timeout: 15000 },
    );
    const rows = await repoRows(page);
    check("1 左栏两行仓库", rows.length === 2, rows.map((r) => r.name));
    check("D6 行是 role=button 且 tabindex=0",
      rows.every((r) => r.role === "button" && r.tabindex === "0"), rows);
    await page.screenshot({ path: join(shots, "1-repos.png") });

    // 验收 1/2/3：点仓库 A 行 → 主区出现分支列表，main 有「当前」徽章、8 位 sha 在
    await clickRepo(page, "demo-main");
    const branched = await snapshot(page);
    check("1 点击仓库行后分支列表出现", branched.state === "ready" && branched.listVisible
      && branched.rows.length === 2, branched.rows.map((r) => r.name));
    check("1 分支顺序是 refname 字典序", JSON.stringify(branched.rows.map((r) => r.name)) === '["feature","main"]',
      branched.rows.map((r) => r.name));
    check("2 main 是当前（有「当前」徽章）",
      branched.rows.find((r) => r.name === "main").current.includes("当前")
      && !branched.rows.find((r) => r.name === "feature").current, branched.rows);
    check("3 短 sha 是 8 位小写 hex", branched.rows.every((r) => SHA8.test(r.sha)),
      branched.rows.map((r) => r.sha));
    check("1 标题是「分支 · demo-main」", branched.title.includes("分支 · demo-main"), branched.title);
    check("D6 有 upstream 才显示 .branch-upstream",
      branched.rows.find((r) => r.name === "main").upstream.includes("origin/main")
      && branched.rows.find((r) => r.name === "feature").upstream === "", branched.rows);
    check("D6 选中行有 data-selected",
      (await repoRows(page)).find((r) => r.name === "demo-main").selected === true);
    await page.screenshot({ path: join(shots, "2-branches-main.png") });

    // 切到空仓库：ready + 空态文案（不是 error、不是空白）
    await clickRepo(page, "demo-empty");
    const empty = await snapshot(page);
    check("4 空仓库 data-state=ready", empty.state === "ready", empty.state);
    check("4 空仓库显示空态文案", empty.emptyVisible && empty.emptyText.includes("还没有任何分支"), empty.emptyText);
    check("4 空仓库列表为空且不是错误", empty.rows.length === 0 && !empty.errorVisible);
    check("4 标题跟着换", empty.title.includes("分支 · demo-empty"), empty.title);
    await page.screenshot({ path: join(shots, "3-empty-repo.png") });

    // 验收 5：切回 A 且列表**刷新**（期间真的新建了一个分支）
    await gitIn(repoA, ["branch", "hotfix"]);
    await clickRepo(page, "demo-main");
    const refreshed = await snapshot(page);
    check("5 切回后列表刷新（出现新分支 hotfix）",
      refreshed.rows.length === 3 && refreshed.rows.some((r) => r.name === "hotfix"),
      refreshed.rows.map((r) => r.name));
    await page.screenshot({ path: join(shots, "4-refreshed.png") });

    // D6 键盘可达：Enter 选空仓库；Space 切回 A
    await page.evaluate(() => {
      const li = Array.from(document.querySelectorAll("#repo-list li[data-repo-id]"))
        .find((row) => ((row.querySelector(".repo-name") || {}).textContent || "") === "demo-empty");
      li.focus();
    });
    await page.keyboard.press("Enter");
    await waitBranchesReady(page);
    const byEnter = await snapshot(page);
    check("D6 Enter 能选中仓库", byEnter.title.includes("demo-empty") && byEnter.emptyVisible, byEnter.title);

    await page.evaluate(() => {
      const li = Array.from(document.querySelectorAll("#repo-list li[data-repo-id]"))
        .find((row) => ((row.querySelector(".repo-name") || {}).textContent || "") === "demo-main");
      li.focus();
    });
    await page.keyboard.press(" ");
    await waitBranchesReady(page);
    const bySpace = await snapshot(page);
    check("D6 Space 能选中仓库", bySpace.title.includes("demo-main") && bySpace.rows.length === 3, bySpace.title);
    await page.screenshot({ path: join(shots, "5-keyboard.png") });

    // D6 断连：回 idle + 清空列表 + 文案「已断开：分支列表未知」；恢复后自动重取
    const cdp = await page.createCDPSession();
    await cdp.send("Network.enable");
    await cdp.send("Network.emulateNetworkConditions", {
      offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1,
    });
    await page.waitForFunction(
      () => document.getElementById("branches").dataset.state === "idle",
      { timeout: 15000 },
    );
    await sleep(200);
    const offline = await snapshot(page);
    check("D6 断连回 idle", offline.state === "idle", offline.state);
    check("D6 断连文案「已断开：分支列表未知」", offline.stateText.includes("已断开：分支列表未知"), offline.stateText);
    check("D6 断连清空分支列表", offline.rows.length === 0, offline.rows);
    await page.screenshot({ path: join(shots, "6-disconnected.png") });

    await cdp.send("Network.emulateNetworkConditions", {
      offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1,
    });
    await page.evaluate(() => document.getElementById("reconnect").click());
    await page.waitForFunction(
      () => document.getElementById("repos").dataset.state === "ready"
        && document.getElementById("branches").dataset.state === "ready",
      { timeout: 20000 },
    );
    await sleep(300);
    const reconnected = await snapshot(page);
    check("D6 重连且仍有选中仓库 → 自动重取分支", reconnected.rows.length === 3
      && reconnected.title.includes("demo-main"), reconnected.rows.map((r) => r.name));
    await page.screenshot({ path: join(shots, "7-reconnected.png") });
  } finally {
    await browser.close().catch(() => {});
    await stopKernel(kernel);
    const log = join(work, "kernel.log");
    await writeFile(log, kernel.output(), "utf8");
    process.stdout.write(`内核日志：${log}\n截图目录：${shots}\n`);
    if (/WebSocket.*[?]ticket=(?!<REDACTED>)/.test(kernel.output())) {
      // 05 §4.1：ticket 不得进日志（S2 的回归点，验收时顺手再看一眼）
      failures += 1;
      process.stdout.write("CHECK ✗ 内核日志里出现了未脱敏的 ticket\n");
    }
  }

  if (failures > 0) {
    process.stdout.write(`\n❌ ${failures} 项失败\n`);
    process.exit(1);
  }
  process.stdout.write("\n✅ 浏览器验收全部通过（S4 验收 1–5 + D6 的空态/切回刷新/键盘可达/断连清空）\n");
}

main().catch((error) => {
  process.stdout.write(`\n❌ 剧本崩溃：${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
