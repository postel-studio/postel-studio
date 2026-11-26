#!/usr/bin/env node
/*
 * S7 验收脚本：在**真实浏览器**里跑「分配工作区 → 磁盘上真有 worktree → 回收」。
 *
 * 为什么它不在 `tests/` 里：pytest 里没有浏览器，前端 DOM 行为只能靠文本断言
 * （06-pitfalls T9 的老问题）。这个脚本补的就是那一块：起真内核、开真 Chrome、
 * 点真按钮、读真 DOM，并用 Node 的 fs / git 直接核对磁盘与 git 的事实。
 *
 * **不进项目依赖**（裁决：允许入库，但不得进 pyproject.toml、不得引入前端构建链）。
 * 它只需要本机已安装的 puppeteer-core 与 Chrome；缺任何一样就**优雅跳过**并说明原因，
 * 退出码 0（跳过不是失败），所以它永远不会拖住 CI —— CI 里根本没跑它。
 *
 * 覆盖（S7 D9 的 DOM 契约 + 验收 1~4）：
 *   1. 初始 idle：没选仓库之前不说「这个仓库还没有工作区」；
 *   2. 选仓库 → ready + 空态文案 + 基础分支预填默认分支 + 「分配」可用；
 *   3. 点「分配工作区」→ 路径与分支出现在页面上，且**磁盘上真有**、git 里真有（fs + git 双证）；
 *   4. 再点一次 → 同一路径，git 里仍只有一条记录（复用，不重复创建）；
 *   5. 脏工作区点「回收」→ 错误文案 + 目录与文件**原样保留**（不 --force）；
 *      清掉文件后再回收 → 目录消失、git 记录消失、分支保留（不删分支）；
 *   6. 基础分支不存在 → 可读错误（BAD_BASE_REF），没有留下半个目录；
 *   7. 回车（form submit）与点按钮是同一条路径；
 *   8. 断连（真 SIGKILL）→ #workspace 回 idle + 「已断开：工作区未知」+ 路径清空；
 *   9. 全程无 console 错误 / 无未捕获异常；两个用户仓库零改动（工作区只在状态根下）。
 *
 * 用法（详见 tools/acceptance/README.md）：
 *
 *     bash scripts/check.sh                 # 先保证代码是绿的
 *     node tools/acceptance/s7_worktree.mjs
 *
 * 退出码：0 = 全部通过，或「环境不具备」而跳过；1 = 有断言失败（输出 `CHECK <n> ✗ ...`）。
 */

import { execFile, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
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
  await new Promise((done) => {
    const timer = setTimeout(done, 15000);
    kernel.proc.on("exit", () => {
      clearTimeout(timer);
      done();
    });
  });
}

// ---------------------------------------------------------------------------
// 真仓库 + 真 git（核对工作区事实，不信页面自报）
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
  await writeFile(join(repo, "f.txt"), "one\n", "utf8");
  await gitIn(repo, ["add", "-A"]);
  await gitIn(repo, ["commit", "-q", "-m", "base"]);
  return repo;
}

/** `git worktree list --porcelain` 里的路径（测试自己的解析，与被测代码无关）。 */
async function worktreePaths(repo) {
  const { stdout } = await gitIn(repo, ["worktree", "list", "--porcelain"]);
  return stdout
    .split("\n")
    .filter((line) => line.startsWith("worktree "))
    .map((line) => line.slice("worktree ".length));
}

async function branchExists(repo, branch) {
  const { stdout } = await gitIn(repo, ["branch", "--list", branch]);
  return stdout.trim() !== "";
}

// ---------------------------------------------------------------------------
// 页面读状态的小工具
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const SNAPSHOT = () => {
  const text = (id) => document.getElementById(id).textContent;
  const visible = (id) => document.getElementById(id).offsetParent !== null;
  const rows = Array.from(document.querySelectorAll("#repo-list li[data-repo-id]"));
  return {
    state: document.getElementById("workspace").dataset.state,
    stateText: text("workspace-state"),
    stateVisible: visible("workspace-state"),
    error: text("workspace-error"),
    errorVisible: visible("workspace-error"),
    emptyVisible: visible("workspace-empty"),
    detailsVisible: visible("workspace-details"),
    bot: text("workspace-bot"),
    path: text("workspace-path"),
    branch: text("workspace-branch"),
    releaseVisible: visible("workspace-release"),
    base: document.getElementById("workspace-base").value,
    acquireDisabled: document.getElementById("workspace-acquire").disabled,
    repoId: rows.length ? rows[0].dataset.repoId : null,
  };
};

const snapshot = (page) => page.evaluate(SNAPSHOT);

async function bindPath(page, path) {
  await page.type("#bind-path", path);
  await page.click("#bind-button");
  await sleep(400);
}

async function waitWorkspaceState(page, state) {
  await page.waitForFunction(
    (want) => document.getElementById("workspace").dataset.state === want,
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
  await waitWorkspaceState(page, "ready");
}

/**
 * 把基础分支输入框的值换成 `value`（**选中再输入**，不是 append）。
 *
 * 第一版用 `click(sel, {clickCount: 3})` 想"三击全选"，实测在 headless 里没有选中，
 * `type()` 直接追加成了 `mainnosuchmain`——于是 step 6 的"不存在的 ref"用例其实测的是
 * 另一个字符串，而 step 7 的回车永远拿到 BAD_BASE_REF（真实浏览器验收当场抓到）。
 * 现在用 Ctrl+A + Backspace，并在返回前断言值真的等于 `value`。
 */
async function replaceBase(page, value) {
  await page.click("#workspace-base");
  await page.keyboard.down("Control");
  await page.keyboard.press("KeyA");
  await page.keyboard.up("Control");
  await page.keyboard.press("Backspace");
  await page.type("#workspace-base", value);
  const actual = await page.$eval("#workspace-base", (el) => el.value);
  if (actual !== value) throw new Error(`基础分支没被替换：想要 ${value}，实际 ${actual}`);
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

  const work = await mkdtemp(join(tmpdir(), "postel-s7-acceptance-"));
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

    // ---- 1. 初始态：没选仓库 → idle，不装成「这个仓库还没有工作区」 ----
    const initial = await snapshot(page);
    check(
      "1 初始 #workspace 是 idle 且提示「选择一个仓库后分配工作区」",
      initial.state === "idle" &&
        initial.stateText.trim() === "选择一个仓库后分配工作区" &&
        initial.stateVisible,
      { state: initial.state, text: initial.stateText },
    );
    check(
      "1 初始不显示路径/分支/空态，「分配」不可点",
      !initial.detailsVisible &&
        !initial.emptyVisible &&
        !initial.releaseVisible &&
        initial.acquireDisabled === true,
    );

    // ---- 2. 绑定两个真仓库并选中第一个 ----
    await bindPath(page, repoMain);
    await bindPath(page, repoTwo);
    await clickRepoRow(page, "demo-main");
    const picked = await snapshot(page);
    check(
      "2 选仓库后 ready + 空态文案「这个仓库还没有分配工作区」",
      picked.state === "ready" && picked.emptyVisible === true,
      { state: picked.state, empty: picked.emptyVisible },
    );
    check(
      "2 基础分支输入框预填默认分支 main",
      picked.base === "main",
      picked.base,
    );
    check(
      "2 卡片说明作用在哪个 Bot 上（Bot：builder）",
      picked.bot.trim() === "Bot：builder",
      picked.bot,
    );
    check("2 「分配」按钮此时可点", picked.acquireDisabled === false);
    await page.screenshot({ path: join(shots, "1-ready-empty.png") });

    // ---- 3. 点「分配工作区」→ 页面 + 磁盘 + git 三处都得对上 ----
    await page.click("#workspace-acquire");
    await waitWorkspaceState(page, "ready");
    const allocated = await snapshot(page);
    const repoId = allocated.repoId;
    const expectedPath = join(stateHome, "worktrees", repoId, "builder");
    check(
      "3 页面显示路径", allocated.path === `路径：${expectedPath}`, allocated.path,
    );
    check(
      "3 页面显示分支 bot/builder/workspace",
      allocated.branch === "分支：bot/builder/workspace",
      allocated.branch,
    );
    check(
      "3 磁盘上真有这个目录（含 .git 文件）",
      existsSync(expectedPath) && existsSync(join(expectedPath, ".git")),
      expectedPath,
    );
    const afterAcquire = await worktreePaths(repoMain);
    check(
      "3 原仓库 git worktree list 里有它",
      afterAcquire.filter((p) => p === expectedPath).length === 1,
      afterAcquire,
    );
    check(
      "3 工作区不在用户仓库的工作目录里",
      !expectedPath.startsWith(repoMain),
      { path: expectedPath, repo: repoMain },
    );
    check("3 回收按钮此时可见", allocated.releaseVisible === true);
    await page.screenshot({ path: join(shots, "2-allocated.png") });

    // ---- 4. 再点一次：复用，不重复创建 ----
    await page.click("#workspace-acquire");
    await waitWorkspaceState(page, "ready");
    const again = await snapshot(page);
    const afterSecond = await worktreePaths(repoMain);
    check("4 第二次点击仍是同一路径", again.path === allocated.path, again.path);
    check(
      "4 git 里仍只有一条记录（没有重复创建）",
      afterSecond.filter((p) => p === expectedPath).length === 1,
      afterSecond,
    );

    // ---- 5. 脏工作区回收：拒绝，且不删文件 ----
    const burn = join(expectedPath, "burn.txt");
    await writeFile(burn, "uncommitted\n", "utf8");
    await page.click("#workspace-release");
    await waitWorkspaceState(page, "error");
    const dirty = await snapshot(page);
    check(
      "5 脏工作区回收 → error + 可读文案（按码映射）",
      dirty.state === "error" &&
        dirty.error.trim() === "工作区目录被占用，或里面有未提交的改动" &&
        dirty.errorVisible,
      dirty.error,
    );
    check(
      "5 目录与未提交的文件都还在（不 --force，不丢用户数据）",
      existsSync(burn),
    );
    await page.screenshot({ path: join(shots, "3-dirty-conflict.png") });

    // 清掉用户自己放的文件之后，回收才成功；**分支必须保留**（D3）
    await rm(burn, { force: true });
    await page.click("#workspace-release");
    await waitWorkspaceState(page, "ready");
    const released = await snapshot(page);
    check(
      "5 干净后回收 → 目录消失、空态文案回来",
      !existsSync(expectedPath) && released.emptyVisible === true && !released.detailsVisible,
      { exists: existsSync(expectedPath), empty: released.emptyVisible },
    );
    const afterRelease = await worktreePaths(repoMain);
    check(
      "5 git worktree list 里不再有它",
      afterRelease.filter((p) => p === expectedPath).length === 0,
      afterRelease,
    );
    check(
      "5 分支 bot/builder/workspace 保留（回收不删分支）",
      await branchExists(repoMain, "bot/builder/workspace"),
    );

    // ---- 6. 基础分支不存在 → BAD_BASE_REF，可读，且不留半个目录 ----
    await replaceBase(page, "nosuch");
    await page.click("#workspace-acquire");
    await waitWorkspaceState(page, "error");
    const badBase = await snapshot(page);
    check(
      "6 不存在的 baseRef → error + 「基础分支不存在或不是提交」",
      badBase.state === "error" &&
        badBase.error.trim() === "基础分支不存在或不是提交" &&
        badBase.errorVisible,
      badBase.error,
    );
    check("6 没有留下半个目录", !existsSync(expectedPath));

    // ---- 7. 回车 = 点按钮（同一个 form 提交路径）----
    await replaceBase(page, "main");
    await page.focus("#workspace-base");
    await page.keyboard.press("Enter");
    await waitWorkspaceState(page, "ready");
    const byEnter = await snapshot(page);
    check(
      "7 在基础分支输入框回车也能分配（与点按钮同一条路径）",
      byEnter.path === `路径：${expectedPath}` && existsSync(expectedPath),
      byEnter.path,
    );
    await page.screenshot({ path: join(shots, "4-by-enter.png") });

    // ---- 8. 断连（真 SIGKILL）→ 回 idle 并清空 ----
    kernel.proc.kill("SIGKILL");
    await page.waitForFunction(
      () => document.getElementById("workspace").dataset.state === "idle",
      { timeout: 15000 },
    );
    await sleep(200);
    const offline = await snapshot(page);
    check("8 断连 → #workspace 回 idle", offline.state === "idle", offline.state);
    check(
      "8 断连文案「已断开：工作区未知」",
      offline.stateText.includes("已断开：工作区未知"),
      offline.stateText,
    );
    check(
      "8 断连 → 路径/分支清空、详情与回收按钮隐藏",
      offline.path === "" && offline.branch === "" && !offline.detailsVisible,
      { path: offline.path, branch: offline.branch },
    );
    check("8 断连后无未捕获 JS 异常", pageErrors.length === 0, pageErrors);
    await page.screenshot({ path: join(shots, "5-disconnected.png") });

    check(
      "9 全程无 console 错误、无未捕获异常",
      consoleErrors.length === 0 && pageErrors.length === 0,
      { consoleErrors, pageErrors },
    );
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
    // 工作区只在状态根下：两个用户仓库的工作区都不该被内核写东西（S3 验收 5 的同一纪律）。
    // 注意：`git worktree add` 会写 `.git/worktrees/`，那不是工作区改动——`status --porcelain`
    // 只看工作区，正好是这条断言要的口径。
    for (const repo of [repoMain, repoTwo]) {
      const status = await gitIn(repo, ["status", "--porcelain"]).catch(() => null);
      if (status && status.stdout.trim() !== "") {
        failures += 1;
        process.stdout.write(`CHECK ✗ 内核往用户仓库的工作区里写了东西：${repo}\n${status.stdout}\n`);
      }
    }
    // 状态根下确实留下了 worktrees 目录（而不是落在了别处）
    const worktreesRoot = join(stateHome, "worktrees");
    const left = existsSync(worktreesRoot) ? await readdir(worktreesRoot) : [];
    process.stdout.write(`状态根下的 worktrees：${JSON.stringify(left)}\n`);
    await rm(repoMain, { recursive: true, force: true });
    await rm(repoTwo, { recursive: true, force: true });
  }

  if (failures > 0) {
    process.stdout.write(`\n❌ ${failures} 项失败\n`);
    process.exit(1);
  }
  process.stdout.write(
    "\n✅ 浏览器验收全部通过（S7：分配 / 复用 / 脏回收被拒 / 回收 / BAD_BASE_REF / 回车 / 断连清空）\n",
  );
}

main().catch((error) => {
  process.stdout.write(`\n❌ 剧本崩溃：${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
