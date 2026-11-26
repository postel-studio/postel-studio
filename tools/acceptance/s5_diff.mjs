#!/usr/bin/env node
/*
 * S5 验收脚本：在**真实浏览器**里跑「点分支 → 看到 diff」。
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
 *     node tools/acceptance/s5_diff.mjs
 *
 * 退出码：
 *   0 = 全部通过，或「环境不具备」而跳过（会写明原因）
 *   1 = 有断言失败（输出形如 `CHECK 2 ✗ ...`，并保留截图目录）
 */

import { execFile, spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
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
  if (kernel.proc.exitCode !== null || kernel.proc.signalCode !== null) {
    return; // 已经被杀掉（验收 10 的 SIGKILL）：别等一个不会再来的 exit 事件
  }
  kernel.proc.kill(signal);
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 15000);
    kernel.proc.on("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

// ---------------------------------------------------------------------------
// 三个真仓库（fixture 的期望值写死在本文件，与 tests/test_diff.py 的 D10 一致）
//
//   demo-main：main init ─ base(f.txt) ── later(m.txt)     ← m.txt **不该**进 diff（三点语义）
//                └── feature（f.txt 2 加 1 删 + 新增 中文.txt）
//   demo-big ：feature 新增 5000 行 big.txt（patch > 100k 字符 → truncated）
//   demo-xss ：feature 新增名为 `x<script>alert(1)<script>.txt` 的文件（XSS 验收）
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
  return repo;
}

async function commitAll(repo, message) {
  await gitIn(repo, ["add", "-A"]);
  await gitIn(repo, ["commit", "-q", "-m", message]);
}

async function makeStandardRepo(base) {
  const repo = await makeRepo(base, "demo-main");
  await gitIn(repo, ["commit", "-q", "--allow-empty", "-m", "init"]);
  await writeFile(join(repo, "f.txt"), "one\ntwo\nthree\n", "utf8");
  await commitAll(repo, "base");
  await gitIn(repo, ["checkout", "-q", "-b", "feature"]);
  await writeFile(join(repo, "f.txt"), "one\nTWO\nthree\nfour\n", "utf8");
  await writeFile(join(repo, "中文.txt"), "中文内容\n", "utf8");
  await commitAll(repo, "feature change");
  await gitIn(repo, ["checkout", "-q", "main"]);
  await writeFile(join(repo, "m.txt"), "main only\n", "utf8");
  await commitAll(repo, "main later"); // 三点语义：这一笔**不该**出现在 main...feature 里
  return repo;
}

async function makeBigRepo(base) {
  const repo = await makeRepo(base, "demo-big");
  await gitIn(repo, ["commit", "-q", "--allow-empty", "-m", "init"]);
  await gitIn(repo, ["checkout", "-q", "-b", "feature"]);
  // 每行 26 字符 + 前缀 + 换行 ≈ 28 字符 × 5001 行 ≈ 140k 字符 > 缺省 maxChars(100k) → 必截断
  const lines = [];
  for (let i = 0; i < 5000; i += 1) {
    lines.push(`line-${String(i).padStart(4, "0")}-xxxxxxxxxxxxxxxx`);
  }
  lines.push("中文行-用于可读性检查");
  await writeFile(join(repo, "big.txt"), lines.join("\n") + "\n", "utf8");
  await commitAll(repo, "big file"); // > 100000 字符 → repo.diff 缺省 maxChars 会截断
  await gitIn(repo, ["checkout", "-q", "main"]);
  return repo;
}

async function makeXssRepo(base) {
  const repo = await makeRepo(base, "demo-xss");
  await gitIn(repo, ["commit", "-q", "--allow-empty", "-m", "init"]);
  await gitIn(repo, ["checkout", "-q", "-b", "feature"]);
  // 文件名不能含 "/"（那就不是合法文件名了）；尖括号在 Linux 文件名里合法
  await writeFile(join(repo, "x<script>alert(1)<script>.txt"), "hello\n", "utf8");
  await commitAll(repo, "evil name");
  await gitIn(repo, ["checkout", "-q", "main"]);
  return repo;
}

// ---------------------------------------------------------------------------
// 页面读状态的小工具
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// D9 的期望串：· 是 U+00B7、− 是 U+2212（**不是** ASCII 连字符）。
const EXPECTED_STAT = "2 个文件 · +3 −1";
const TRUNCATED_TEXT = "已截断：差异太大，只显示前一部分";
const DEFAULT_BRANCH_TEXT = "这是默认分支，选择其它分支查看差异";
const DISCONNECTED_TEXT = "已断开：差异未知";
const BAD_REF_TEXT = "分支或提交不存在";

const SNAPSHOT = () => ({
  state: document.getElementById("diff").dataset.state,
  title: document.getElementById("diff-title").textContent,
  stateText: document.getElementById("diff-state").innerText,
  stateVisible: document.getElementById("diff-state").offsetParent !== null,
  error: document.getElementById("diff-error").innerText,
  errorVisible: document.getElementById("diff-error").offsetParent !== null,
  truncated: document.getElementById("diff-truncated").innerText,
  truncatedVisible: document.getElementById("diff-truncated").offsetParent !== null,
  stat: document.getElementById("diff-stat").textContent,
  statVisible: document.getElementById("diff-stat").offsetParent !== null,
  patchText: document.getElementById("diff-patch").textContent,
  patchVisible: document.getElementById("diff-patch").offsetParent !== null,
  lineCount: document.querySelectorAll("#diff-patch .diff-line").length,
  add: document.querySelectorAll("#diff-patch .diff-line.diff-add").length,
  del: document.querySelectorAll("#diff-patch .diff-line.diff-del").length,
  hunk: document.querySelectorAll("#diff-patch .diff-line.diff-hunk").length,
  meta: document.querySelectorAll("#diff-patch .diff-line.diff-meta").length,
  // `+++ b/` 那一行的 class（D9：文件头是 meta，不是 add）
  fileHeaderKinds: Array.from(document.querySelectorAll("#diff-patch .diff-line"))
    .filter((row) => row.textContent.startsWith("+++ b/"))
    .map((row) => row.className),
  base: document.getElementById("diff-base").value,
  head: document.getElementById("diff-head").value,
  branchRows: Array.from(document.querySelectorAll("#branch-list li[data-branch-name]")).map(
    (li) => ({
      name: li.dataset.branchName,
      selected: li.dataset.selected === "true",
      role: li.getAttribute("role"),
      tabindex: li.getAttribute("tabindex"),
    }),
  ),
});

const REPO_ROWS = () =>
  Array.from(document.querySelectorAll("#repo-list li[data-repo-id]")).map((li) => ({
    name: (li.querySelector(".repo-name") || {}).textContent || "",
  }));

async function snapshot(page) {
  return page.evaluate(SNAPSHOT);
}

async function bindPath(page, path) {
  await page.type("#bind-path", path);
  await page.click("#bind-button");
  await sleep(500);
}

async function waitBranchesReady(page) {
  await page.waitForFunction(
    () => document.getElementById("branches").dataset.state === "ready",
    { timeout: 15000 },
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
  await waitBranchesReady(page);
}

async function clickBranchRow(page, name) {
  await page.evaluate((branchName) => {
    const li = Array.from(document.querySelectorAll("#branch-list li[data-branch-name]")).find(
      (row) => row.dataset.branchName === branchName,
    );
    if (!li) throw new Error(`找不到分支行：${branchName}`);
    li.click();
  }, name);
}

async function focusBranchRow(page, name) {
  await page.evaluate((branchName) => {
    const li = Array.from(document.querySelectorAll("#branch-list li[data-branch-name]")).find(
      (row) => row.dataset.branchName === branchName,
    );
    if (!li) throw new Error(`找不到分支行：${branchName}`);
    li.focus();
  }, name);
}

async function submitDiff(page, base, head) {
  await page.evaluate(
    (b, h) => {
      document.getElementById("diff-base").value = b;
      document.getElementById("diff-head").value = h;
    },
    base,
    head,
  );
  await page.click("#diff-compare");
}

async function waitDiffState(page, state) {
  await page.waitForFunction(
    (want) => document.getElementById("diff").dataset.state === want,
    { timeout: 20000 },
    state,
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
// 主流程：D11 的 10 条
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

  const work = await mkdtemp(join(tmpdir(), "postel-s5-acceptance-"));
  const stateHome = join(work, "postel-home");
  const shots = join(work, "shots");
  await mkdir(stateHome, { recursive: true });
  await mkdir(shots, { recursive: true });

  process.stdout.write(`浏览器：${chrome.version}（${chrome.path}）\n`);
  process.stdout.write(`工作目录：${work}（截图与内核日志都在这）\n`);

  const repoMain = await makeStandardRepo(work);
  const repoBig = await makeBigRepo(work);
  const repoXss = await makeXssRepo(work);
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

    // 初始态：没选仓库 / 没点分支 → #diff 是 idle，只显示提示，不显示 stat/patch/错误
    const initial = await snapshot(page);
    check("D9 初始 idle", initial.state === "idle", initial.state);
    check("D9 初始只显示提示文案", initial.stateVisible && initial.stateText.includes("点左侧分支"));
    check(
      "D9 初始不显示 stat/patch/错误/截断",
      !initial.statVisible && !initial.patchVisible && !initial.errorVisible && !initial.truncatedVisible,
    );

    // 绑定三个真仓库
    await bindPath(page, repoMain);
    await bindPath(page, repoBig);
    await bindPath(page, repoXss);
    await page.waitForFunction(
      () => document.querySelectorAll("#repo-list li[data-repo-id]").length === 3,
      { timeout: 15000 },
    );
    check("0 左栏三行仓库", (await page.evaluate(REPO_ROWS)).length === 3);

    // ---- 验收 1/2/9：点 demo-main 的 feature 分支 → ready，stat 逐字、着色行 ----
    await clickRepoRow(page, "demo-main");
    const branches = await snapshot(page);
    check(
      "D9 分支行 role=button 且 tabindex=0",
      branches.branchRows.length === 2 &&
        branches.branchRows.every((row) => row.role === "button" && row.tabindex === "0"),
      branches.branchRows,
    );

    await clickBranchRow(page, "feature");
    await waitDiffState(page, "ready");
    const ready = await snapshot(page);
    check("1 点 feature 分支行 → data-state=ready", ready.state === "ready", ready.state);
    check("1 #diff-stat 逐字等于 fixture 期望串", ready.stat === EXPECTED_STAT, ready.stat);
    check("1 两个输入框被填上 main / feature", ready.base === "main" && ready.head === "feature", {
      base: ready.base,
      head: ready.head,
    });
    check("1 标题跟着仓库名变", ready.title.includes("差异 · demo-main"), ready.title);
    check("1 分支行有 data-selected", ready.branchRows.find((r) => r.name === "feature").selected === true);
    check(
      "2 着色：add / del / hunk 行各 ≥1",
      ready.add >= 1 && ready.del >= 1 && ready.hunk >= 1,
      { add: ready.add, del: ready.del, hunk: ready.hunk },
    );
    check(
      "2 `+++ b/` 行是 diff-meta 而不是 diff-add",
      ready.fileHeaderKinds.length >= 1 &&
        ready.fileHeaderKinds.every((cls) => cls.includes("diff-meta") && !cls.includes("diff-add")),
      ready.fileHeaderKinds,
    );
    check(
      "9 中文文件名/内容可读（textContent 含字面中文）",
      ready.patchText.includes("中文.txt") && ready.patchText.includes("中文内容"),
    );
    await page.screenshot({ path: join(shots, "1-diff-ready.png") });

    // ---- 验收 3：点默认分支本身 → 不发请求，idle + 固定文案 ----
    await clickBranchRow(page, "main");
    await waitDiffState(page, "idle");
    const onDefault = await snapshot(page);
    check("3 点默认分支 → data-state=idle", onDefault.state === "idle", onDefault.state);
    check("3 文案「这是默认分支，选择其它分支查看差异」", onDefault.stateText.includes(DEFAULT_BRANCH_TEXT), onDefault.stateText);
    check("3 默认分支不发请求：stat/patch 已清空", !onDefault.statVisible && !onDefault.patchVisible);
    await page.screenshot({ path: join(shots, "2-default-branch.png") });

    // ---- 验收 4：手填 main / feature 点「对比」→ ready + stat 正确 ----
    await submitDiff(page, "main", "feature");
    await waitDiffState(page, "ready");
    const manual = await snapshot(page);
    check("4 手填两个 ref 对比 → ready", manual.state === "ready", manual.state);
    check("4 手填对比的 stat 正确", manual.stat === EXPECTED_STAT, manual.stat);
    await page.screenshot({ path: join(shots, "3-manual-compare.png") });

    // ---- 验收 8：键盘 —— 聚焦分支行 + Enter 与 click 同一条路径 ----
    await clickBranchRow(page, "main"); // 先回到 idle，证明是键盘那一下触发的
    await waitDiffState(page, "idle");
    await focusBranchRow(page, "feature");
    await page.keyboard.press("Enter");
    await waitDiffState(page, "ready");
    const byKeyboard = await snapshot(page);
    check("8 聚焦分支行 + Enter → ready", byKeyboard.state === "ready" && byKeyboard.stat === EXPECTED_STAT);
    await page.screenshot({ path: join(shots, "4-keyboard.png") });

    // ---- 验收 5：手填不存在的 ref → error + 按码映射的可读文案 ----
    await submitDiff(page, "no-such-ref-xyz", "feature");
    await waitDiffState(page, "error");
    const errored = await snapshot(page);
    check("5 非法 ref → data-state=error", errored.state === "error", errored.state);
    check("5 #diff-error 文案等于 ERROR_TEXT.BAD_REF", errored.error === BAD_REF_TEXT, errored.error);
    check("5 错误态不是空白：不显示 stat/patch", !errored.statVisible && !errored.patchVisible);
    await page.screenshot({ path: join(shots, "5-bad-ref.png") });

    // ---- 验收 7：XSS —— 文件名是 `<script>` 时必须只是文本 ----
    await clickRepoRow(page, "demo-xss");
    await clickBranchRow(page, "feature");
    await waitDiffState(page, "ready");
    const xss = await snapshot(page);
    const xssScriptCount = await page.evaluate(
      () => document.querySelectorAll("#diff-patch script").length,
    );
    check("7 文件名里的 <script> 不生成 script 元素", xssScriptCount === 0, xssScriptCount);
    check(
      "7 文件名作为字面文本出现在 #diff-patch 里",
      xss.patchText.includes("x<script>alert(1)<script>.txt"),
    );
    await page.screenshot({ path: join(shots, "6-xss.png") });

    // ---- 验收 6：大 diff → 截断提示可见 + 文案逐字 + patch 非空 ----
    await clickRepoRow(page, "demo-big");
    await clickBranchRow(page, "feature");
    await waitDiffState(page, "ready");
    const big = await snapshot(page);
    check("6 大 diff → data-state=ready", big.state === "ready", big.state);
    check("6 #diff-truncated 可见", big.truncatedVisible, big.truncatedVisible);
    check("6 截断文案逐字匹配", big.truncated === TRUNCATED_TEXT, big.truncated);
    check("6 #diff-patch 非空且被渲染成行", big.patchVisible && big.lineCount > 0, {
      len: big.patchText.length,
      lines: big.lineCount,
    });
    check("6 统计数字不受截断影响（1 个文件 / +5001 −0）", big.stat === "1 个文件 · +5001 −0", big.stat);
    await page.screenshot({ path: join(shots, "7-truncated.png") });

    // ---- 验收 10：断连（真的杀掉内核）→ idle + 「已断开：差异未知」+ 清空 ----
    kernel.proc.kill("SIGKILL");
    await page.waitForFunction(
      () => document.getElementById("diff").dataset.state === "idle",
      { timeout: 15000 },
    );
    await sleep(200);
    const offline = await snapshot(page);
    check("10 断连 → data-state=idle", offline.state === "idle", offline.state);
    check("10 断连文案「已断开：差异未知」", offline.stateText.includes(DISCONNECTED_TEXT), offline.stateText);
    check("10 断连清空 patch/stat", !offline.patchVisible && !offline.statVisible);
    await page.screenshot({ path: join(shots, "8-disconnected.png") });
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
    // 六个 repo 目录都不该被内核写东西（S3 验收 5 的同一纪律，这里作为清理前的最后一瞥）
    for (const repo of [repoMain, repoBig, repoXss]) {
      const status = await gitIn(repo, ["status", "--porcelain"]).catch(() => null);
      if (status && status.stdout.trim() !== "") {
        failures += 1;
        process.stdout.write(`CHECK ✗ 内核往用户仓库里写了东西：${repo}\n${status.stdout}\n`);
      }
    }
    await rm(repoMain, { recursive: true, force: true });
    await rm(repoBig, { recursive: true, force: true });
    await rm(repoXss, { recursive: true, force: true });
  }

  if (failures > 0) {
    process.stdout.write(`\n❌ ${failures} 项失败\n`);
    process.exit(1);
  }
  process.stdout.write(
    "\n✅ 浏览器验收全部通过（S5 验收 1–7/9 + D9 的初始态 / 默认分支 / 键盘 / 断连清空）\n",
  );
}

main().catch((error) => {
  process.stdout.write(`\n❌ 剧本崩溃：${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
