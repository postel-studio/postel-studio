#!/usr/bin/env node
/*
 * S18 验收：真浏览器 + 真内核，逐个看空态与错误态。
 *
 * 覆盖验收 1–5 的**用户可见层**：
 *   1 无仓库 / 无 Bot / 无候选 / 无审计记录 → 各自有空态，且"没选"不被说成"没有"；
 *   2 错误按码映射成中文，页面与提示里没有路径/堆栈；
 *   3 杀掉内核后点「重连」→ 显示「内核不可达」+ 原因 + 重连按钮，不白屏；
 *   4 L0 Bot 派活被拦时同屏给原因（不是灰一个按钮了事）；
 *   5 浅色与暗色下正文/次要文字的对比度都 ≥ 4.5:1。
 *
 * 用法：node tools/acceptance/s18_states.mjs
 */

import { execFile, execFileSync, spawn } from "node:child_process";
import { mkdir, mkdtemp } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileP = promisify(execFile);
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");
const CLI = join(REPO_ROOT, ".venv", "bin", "postel-studio");

function agentEnv(stateHome) {
  try {
    const out = execFileSync(join(REPO_ROOT, ".venv", "bin", "python"),
      [join(HERE, "agent_env.py"), stateHome], { cwd: REPO_ROOT, timeout: 30000, encoding: "utf8" });
    return JSON.parse(out.trim() || "{}");
  } catch {
    return {};
  }
}

const BOOT_SECONDS = 40;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
let checks = 0;
function check(label, ok, detail) {
  checks += 1;
  const tail = detail === undefined ? "" : `  ${JSON.stringify(detail)}`;
  if (ok) process.stdout.write(`CHECK ✓ ${checks} ${label}${tail}\n`);
  else {
    failures += 1;
    process.stdout.write(`CHECK ✗ ${checks} ${label}${tail}\n`);
  }
}

async function findPuppeteer() {
  const require = createRequire(import.meta.url);
  for (const name of [
    "puppeteer-core",
    "/home/" + (process.env.USER || "") + "/.local/lib/node_modules/puppeteer-core",
    "/usr/lib/node_modules/puppeteer-core",
  ]) {
    try { return require(name); } catch { /* 下一个 */ }
  }
  return null;
}

async function findChrome() {
  for (const path of [
    process.env.POSTEL_CHROME, process.env.CHROME_PATH,
    "/home/" + (process.env.USER || "") + "/.local/bin/google-chrome",
    "/usr/bin/google-chrome", "/usr/bin/chromium",
  ].filter(Boolean)) {
    try {
      const { stdout } = await execFileP(path, ["--version"], { timeout: 20000 });
      return { path, version: stdout.trim() };
    } catch { /* 下一个 */ }
  }
  return null;
}

function skip(reason) {
  process.stdout.write(`SKIP ${reason}\n`);
  process.exit(0);
}

async function makeRepo(base) {
  const repo = join(base, "demo-main");
  await mkdir(repo, { recursive: true });
  await execFileP("git", ["init", "-q", "--initial-branch=main", repo], { timeout: 30000 });
  await execFileP("git", ["-c", "user.email=t@t.invalid", "-c", "user.name=t",
    "commit", "-q", "--allow-empty", "-m", "base"], { cwd: repo, timeout: 30000 });
  return repo;
}

function startKernel(stateHome) {
  const proc = spawn(CLI, ["serve", "--port", "0"], {
    cwd: REPO_ROOT,
    env: { ...process.env, ...agentEnv(stateHome), POSTEL_HOME: stateHome, HOME: stateHome },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  proc.stdout.on("data", (chunk) => (output += chunk));
  proc.stderr.on("data", (chunk) => (output += chunk));
  const deadline = Date.now() + BOOT_SECONDS * 1000;
  return (async () => {
    while (Date.now() < deadline) {
      const match = output.match(/地址\s+(http:\/\/127\.0\.0\.1:\d+)/);
      if (match) return { proc, url: match[1], output: () => output };
      if (proc.exitCode !== null) throw new Error(`内核启动失败：\n${output}`);
      await sleep(100);
    }
    proc.kill("SIGKILL");
    throw new Error(`内核 ${BOOT_SECONDS}s 内没打印地址：\n${output}`);
  })();
}

async function stopKernel(kernel) {
  if (kernel.proc.exitCode !== null || kernel.proc.signalCode !== null) return;
  kernel.proc.kill("SIGTERM");
  await new Promise((done) => {
    const timer = setTimeout(done, 15000);
    kernel.proc.on("exit", () => { clearTimeout(timer); done(); });
  });
}

const SNAPSHOT = () => {
  const el = (id) => document.getElementById(id);
  const visible = (node) => Boolean(node) && !node.hidden && getComputedStyle(node).display !== "none";
  return {
    repoEmptyText: el("repo-empty") ? el("repo-empty").textContent || "" : "",
    repoEmptyVisible: visible(el("repo-empty")),
    branchState: el("branches-state") ? el("branches-state").textContent || "" : "",
    branchEmptyVisible: visible(el("branch-empty")),
    botEmptyVisible: visible(el("bot-empty")),
    candidateState: el("candidate") ? el("candidate").dataset.state : "",
    candidateEmptyVisible: visible(el("candidate-empty")),
    auditState: el("audit") ? el("audit").dataset.state : "",
    auditStateText: el("audit-state") ? el("audit-state").textContent || "" : "",
    auditEmptyVisible: visible(el("audit-empty")),
    permissionNote: el("task-permission-note") ? el("task-permission-note").textContent || "" : "",
    permissionNoteVisible: visible(el("task-permission-note")),
    dispatchDisabled: el("task-dispatch") ? el("task-dispatch").disabled : null,
    diffError: el("diff-error") ? el("diff-error").textContent || "" : "",
    diffErrorVisible: visible(el("diff-error")),
    status: el("status") ? el("status").dataset.state : "",
    statusText: el("status-text") ? el("status-text").textContent || "" : "",
    statusHint: el("status-hint") ? el("status-hint").textContent || "" : "",
    statusHintVisible: visible(el("status-hint")),
  };
};

const CONTRAST = () => {
  function parse(color) {
    const m = color.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const parts = m[1].split(/[, ]+/).filter(Boolean).map(Number);
    return { r: parts[0], g: parts[1], b: parts[2] };
  }
  function lum(rgb) {
    const f = (v) => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(rgb.r) + 0.7152 * f(rgb.g) + 0.0722 * f(rgb.b);
  }
  function ratio(fg, bg) {
    const l1 = lum(fg);
    const l2 = lum(bg);
    const [hi, lo] = l1 > l2 ? [l1, l2] : [l2, l1];
    return (hi + 0.05) / (lo + 0.05);
  }
  const body = getComputedStyle(document.body);
  const bg = parse(body.backgroundColor);
  const text = parse(body.color);
  const mutedEl = document.getElementById("repo-empty");
  const muted = mutedEl ? parse(getComputedStyle(mutedEl).color) : null;
  return {
    bg: body.backgroundColor,
    textRatio: ratio(text, bg),
    mutedRatio: muted ? ratio(muted, bg) : null,
  };
};

async function ready(page) {
  await page.waitForFunction(
    () => document.getElementById("status")?.dataset.state === "connected", { timeout: 20000 },
  );
}

async function main() {
  const puppeteer = await findPuppeteer();
  if (!puppeteer) skip("没有 puppeteer-core");
  const chrome = await findChrome();
  if (!chrome) skip("没有可用的 Chrome");

  const base = await mkdtemp(join(tmpdir(), "postel-s18-"));
  const stateHome = join(base, "postel-home");
  await mkdir(stateHome, { recursive: true });
  const repoMain = await makeRepo(base);
  const kernel = await startKernel(stateHome);
  const browser = await puppeteer.launch({
    executablePath: chrome.path, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => pageErrors.push(String(e)));

  try {
    await page.goto(kernel.url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await ready(page);

    // ---- 验收 1：空态（无仓库 / 没选 ≠ 没有 / 无 Bot / 无候选 / 无审计）----
    const initial = await page.evaluate(SNAPSHOT);
    check("1 无仓库时空态可见", initial.repoEmptyVisible
      && initial.repoEmptyText.includes("还没有绑定仓库"), initial);
    check("1 没选仓库时分支面板说「从左侧选择一个仓库」，不显示空态",
      initial.branchState.includes("从左侧选择一个仓库") && !initial.branchEmptyVisible,
      { state: initial.branchState, visible: initial.branchEmptyVisible });

    await page.type("#bind-path", repoMain);
    await page.click("#bind-button");
    await page.waitForFunction(
      () => document.querySelectorAll("#repo-list li[data-repo-id]").length === 1,
      { timeout: 15000 },
    );
    await page.evaluate(() => {
      const li = document.querySelector("#repo-list li[data-repo-id]");
      if (!li) throw new Error("找不到仓库行");
      li.click();
    });
    await page.waitForFunction(
      () => document.getElementById("bots")?.dataset.state === "ready", { timeout: 15000 },
    );
    await page.waitForFunction(
      () => document.getElementById("candidate")?.dataset.state === "empty", { timeout: 15000 },
    );
    const afterRepo = await page.evaluate(SNAPSHOT);
    check("1 无 Bot 时空态可见", afterRepo.botEmptyVisible, afterRepo);
    check("1 无候选时显示候选空态", afterRepo.candidateState === "empty"
      && afterRepo.candidateEmptyVisible, afterRepo);

    // ---- 新建一个 L0 Bot（验收 4 用）----
    await page.click("#bot-new");
    await page.waitForFunction(
      () => document.getElementById("bot-create-form")?.hidden === false, { timeout: 5000 },
    );
    await page.type("#bot-create-name", "reader");
    await page.select("#bot-create-permission", "L0");
    await page.click("#bot-create-submit");
    await page.waitForFunction(
      () => document.getElementById("bot-create-form")?.hidden === true, { timeout: 15000 },
    );
    await page.evaluate(() => {
      const li = Array.from(document.querySelectorAll("#bot-list li")).find(
        (n) => (n.textContent || "").includes("reader"));
      if (!li) throw new Error("找不到 reader 行");
      li.click();
    });
    await page.waitForFunction(
      () => document.getElementById("audit")?.dataset.state === "empty", { timeout: 15000 },
    );
    const afterBot = await page.evaluate(SNAPSHOT);
    check("1 无审计记录时显示空态（且不是「审计读取失败」）",
      afterBot.auditEmptyVisible && !afterBot.auditStateText.includes("失败"), afterBot);
    check("4 L0 Bot 派活被拦时同屏给原因",
      afterBot.permissionNoteVisible && afterBot.permissionNote.includes("L0")
      && afterBot.dispatchDisabled === true, afterBot);

    // ---- 验收 2：错误按码映射，页面没有路径/堆栈 ----
    await page.type("#diff-base", "no-such-base");
    await page.type("#diff-head", "no-such-head");
    await page.click("#diff-compare");
    await page.waitForFunction(
      () => document.getElementById("diff")?.dataset.state === "error", { timeout: 15000 },
    );
    const errored = await page.evaluate(SNAPSHOT);
    check("2 错误显示可读中文（BAD_REF 映射）",
      errored.diffError.includes("分支或提交不存在"), errored.diffError);
    check("2 错误里没有绝对路径 / 堆栈",
      !/\/(home|tmp|usr)\//.test(errored.diffError) && !errored.diffError.includes("Traceback")
      && !errored.diffError.includes('File "'), errored.diffError);

    // ---- 验收 5：两套主题的对比度（headless 默认是浅色，必须先显式切到暗色）----
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
    const dark = await page.evaluate(CONTRAST);
    check("5 暗色主题真的生效（底色是暗的）", /rgb\(15, 17, 21\)/.test(dark.bg), dark);
    check("5 暗色正文对比度 ≥ 4.5", dark.textRatio >= 4.5, dark);
    check("5 暗色次要文字对比度 ≥ 4.5", dark.mutedRatio !== null && dark.mutedRatio >= 4.5, dark);
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
    const light = await page.evaluate(CONTRAST);
    check("5 浅色主题真的生效（背景变亮）",
      /rgb\(255, 255, 255\)/.test(light.bg), light);
    check("5 浅色正文对比度 ≥ 4.5", light.textRatio >= 4.5, light);
    check("5 浅色次要文字对比度 ≥ 4.5", light.mutedRatio !== null && light.mutedRatio >= 4.5,
      light);

    check("2/5 全程无 console 错误、无未捕获异常（杀内核前）",
      consoleErrors.length === 0 && pageErrors.length === 0,
      { consoleErrors: consoleErrors.slice(0, 3), pageErrors: pageErrors.slice(0, 3) });

    // ---- 验收 3：内核被杀 → 点重连 → 内核不可达 ----
    kernel.proc.kill("SIGKILL");
    await page.waitForFunction(
      () => document.getElementById("status")?.dataset.state === "disconnected", { timeout: 8000 },
    );
    await page.click("#reconnect");
    await page.waitForFunction(
      () => document.getElementById("status-text")?.textContent.includes("内核不可达"),
      { timeout: 15000 },
    );
    const offline = await page.evaluate(SNAPSHOT);
    check("3 断线后显示「内核不可达」", offline.statusText.includes("内核不可达"), offline.statusText);
    check("3 旁边给出下一步（确认进程再点重连）",
      offline.statusHintVisible && offline.statusHint.includes("重连"), offline.statusHint);
    check("3 重连按钮仍可见（不是只能用控制台）",
      await page.evaluate(() => {
        const b = document.getElementById("reconnect");
        return Boolean(b) && getComputedStyle(b).display !== "none";
      }));
  } catch (error) {
    check("场景执行中断", false, String(error).slice(0, 400));
  } finally {
    await browser.close().catch(() => {});
    await stopKernel(kernel);
    // 临时目录不删（沙箱 safe-delete 拦截 rm）；前缀 postel-s18- 肉眼可辨。
  }

  process.stdout.write(`\n浏览器验收：${checks} CHECK，${failures} 条失败\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  process.stdout.write(`FATAL ${error}\n`);
  process.exit(1);
});
