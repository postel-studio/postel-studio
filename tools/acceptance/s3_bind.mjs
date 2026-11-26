#!/usr/bin/env node
/*
 * S3 验收脚本：在**真实浏览器**里跑「填路径 → 左栏出现这个仓库」。
 *
 * 为什么它不在 `tests/` 里：pytest 里没有浏览器，前端 DOM 行为只能靠文本断言
 * （06-pitfalls T9 的老问题）。这个脚本补的就是那一块：它起真内核、开真 Chrome、
 * 点真按钮、读真 DOM。
 *
 * **不进项目依赖**（裁决：允许入库，但不得进 pyproject.toml、不得引入前端构建链）。
 * 它只需要本机已安装的 puppeteer-core 与 Chrome；缺任何一样就**优雅跳过**并说明原因，
 * 退出码 0（跳过不是失败），所以它永远不会拖住 CI —— CI 里根本没跑它。
 *
 * 用法（详见 tools/acceptance/README.md）：
 *
 *     bash scripts/check.sh            # 先保证代码是绿的
 *     node tools/acceptance/s3_bind.mjs
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
const PYTHON = process.env.POSTEL_PYTHON || join(REPO_ROOT, ".venv", "bin", "python");
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

async function makeDemoRepo(base) {
  const repo = join(base, "demo");
  await mkdir(repo, { recursive: true });
  const git = (args) =>
    execFileP("git", ["-C", repo, "-c", "user.email=acceptance@example.invalid",
      "-c", "user.name=acceptance", ...args], { timeout: 30000 });
  await execFileP("git", ["init", "-q", "--initial-branch=main", repo], { timeout: 30000 });
  await git(["commit", "-q", "--allow-empty", "-m", "init"]);
  return repo;
}

// ---------------------------------------------------------------------------
// 页面读状态的小工具
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SNAPSHOT = () => ({
  state: document.getElementById("repos").dataset.state,
  stateText: document.getElementById("repo-state").innerText,
  stateVisible: document.getElementById("repo-state").offsetParent !== null,
  error: document.getElementById("repos-error").innerText,
  errorVisible: document.getElementById("repos-error").offsetParent !== null,
  emptyVisible: document.getElementById("repo-empty").offsetParent !== null,
  listVisible: document.getElementById("repo-list").offsetParent !== null,
  inputDisabled: document.getElementById("bind-path").disabled,
});

const ROWS = () =>
  Array.from(document.querySelectorAll("#repo-list li")).map((li) => ({
    id: li.dataset.repoId,
    text: li.innerText.replace(/\s+/g, " ").trim(),
    visible: li.offsetParent !== null,
  }));

async function snapshot(page) {
  return page.evaluate(SNAPSHOT);
}

async function rows(page) {
  return page.evaluate(ROWS);
}

async function bindPath(page, path) {
  await page.type("#bind-path", path);
  await page.click("#bind-button");
}

let failures = 0;
function check(name, ok, detail) {
  if (!ok) failures += 1;
  process.stdout.write(`CHECK ${ok ? "✓" : "✗"} ${name}${detail ? "  " + JSON.stringify(detail) : ""}\n`);
}

// ---------------------------------------------------------------------------
// 主流程：验收 1–5 + D1（断连时「没连上」与「没有仓库」可区分）
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

  const work = await mkdtemp(join(tmpdir(), "postel-s3-acceptance-"));
  const stateHome = join(work, "postel-home");
  const shots = join(work, "shots");
  await mkdir(stateHome, { recursive: true });
  await mkdir(shots, { recursive: true });

  process.stdout.write(`浏览器：${chrome.version}（${chrome.path}）\n`);
  process.stdout.write(`工作目录：${work}（截图与内核日志都在这）\n`);

  const repo = await makeDemoRepo(work);
  const kernel = await startKernel(stateHome);
  const browser = await puppeteer.launch({
    executablePath: chrome.path,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1200, height: 800 });

  try {
    await page.goto(kernel.url, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.getElementById("repos").dataset.state === "ready",
      { timeout: 15000 },
    );

    // 验收 1：输入真实 git 仓库路径 → 绑定 → 左栏出现该仓库（名称 + 默认分支）
    await bindPath(page, repo);
    await page.waitForFunction(() => document.querySelectorAll("#repo-list li").length > 0, {
      timeout: 15000,
    });
    await sleep(200);
    const bound = await rows(page);
    check("1 绑定后左栏出现仓库", bound.length === 1, bound);
    check(
      "1 显示名称与默认分支",
      bound.length === 1 && /demo/.test(bound[0].text) && /默认分支 main/.test(bound[0].text),
      bound.map((r) => r.text),
    );
    await page.screenshot({ path: join(shots, "1-bind.png") });

    // 验收 2：非 git 目录 → 可读中文错误，列表不变
    await bindPath(page, dirname(repo));
    await page.waitForFunction(
      () => document.getElementById("repos-error").offsetParent !== null,
      { timeout: 15000 },
    );
    await sleep(200);
    const afterBad = await snapshot(page);
    check("2 非仓库显示可读错误", afterBad.errorVisible && afterBad.error.includes("不是 git 仓库"), afterBad.error);
    check("2 错误后列表不变", JSON.stringify(await rows(page)) === JSON.stringify(bound));
    await page.screenshot({ path: join(shots, "2-not-a-repo.png") });

    // 验收 3：刷新页面 → 仓库仍在
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.getElementById("repos").dataset.state === "ready"
        && document.querySelectorAll("#repo-list li").length > 0,
      { timeout: 15000 },
    );
    await sleep(200);
    const afterReload = await rows(page);
    check("3 刷新后仍在", afterReload.length === 1 && afterReload[0].id === bound[0].id, afterReload);
    await page.screenshot({ path: join(shots, "3-after-reload.png") });

    // 验收 4：同一路径再绑一次 → 仍只有一行
    await bindPath(page, repo);
    await sleep(800);
    const afterRebind = await rows(page);
    check(
      "4 重复绑定仍一行",
      afterRebind.length === 1 && afterRebind[0].id === bound[0].id,
      afterRebind,
    );
    await page.screenshot({ path: join(shots, "4-rebind.png") });

    // D1：断连时「没有仓库」与「没连上」必须可区分
    // 用 CDP 断网而不是杀内核：内核还得活着给后面的重连用。
    const cdp = await page.createCDPSession();
    await cdp.send("Network.enable");
    await cdp.send("Network.emulateNetworkConditions", {
      offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1,
    });
    await page.waitForFunction(
      () => document.getElementById("repos").dataset.state === "disconnected",
      { timeout: 15000 },
    );
    await sleep(200);
    const offline = await snapshot(page);
    check("D1 断连后状态是 disconnected", offline.state === "disconnected", offline.state);
    check("D1 断连时显示「已断开」文案", /已断开/.test(offline.stateText) && offline.stateVisible, offline.stateText);
    check("D1 断连时不显示列表", offline.listVisible === false);
    check("D1 断连时不显示「还没有仓库」空态", offline.emptyVisible === false);
    await page.screenshot({ path: join(shots, "5-disconnected.png") });

    await cdp.send("Network.emulateNetworkConditions", {
      offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1,
    });
    await page.evaluate(() => document.getElementById("reconnect").click());
    await page.waitForFunction(
      () => document.getElementById("repos").dataset.state === "ready"
        && document.querySelectorAll("#repo-list li").length > 0,
      { timeout: 20000 },
    );
    await sleep(300);
    check("重连后列表回来", (await rows(page)).length === 1);
    await page.screenshot({ path: join(shots, "6-reconnected.png") });

    // 验收 5 的落盘部分：state.db 在状态根、仓库里没有多出文件
    const dbPath = join(stateHome, "state.db");
    const probe = await execFileP(PYTHON, [
      "-c",
      "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
        + "print(c.execute('select path, default_branch from repos').fetchall())",
      dbPath,
    ]);
    check("5 state.db 里是仓库根", probe.stdout.includes(repo), probe.stdout.trim());
    const status = await execFileP("git", ["-C", repo, "status", "--porcelain", "-uall"], {
      timeout: 30000,
    });
    check("5 没往用户仓库里写东西", status.stdout.trim() === "", status.stdout.trim());
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
  process.stdout.write("\n✅ 浏览器验收全部通过（验收 1–4、D1、验收 5 的落盘部分）\n");
}

main().catch((error) => {
  process.stdout.write(`\n❌ 剧本崩溃：${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});