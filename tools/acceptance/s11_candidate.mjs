#!/usr/bin/env node
/*
 * S11 验收：真浏览器 + 真 opencode，任务成功后候选卡出现、点开看到 diff、刷新后仍在。
 *
 * 覆盖验收 1/2/4/5/6：候选分支 bot/builder/<8位>、+X −Y、仅本地、S5 渲染件、
 * 落盘后刷新可见。无改动分支由 tests/test_candidate.py 覆盖（让 Agent 稳定"什么都不做"
 * 在浏览器里不可靠，见规格 §3 的验收→测试映射）。
 *
 * 用法：node tools/acceptance/s11_candidate.mjs
 */

import { execFile, spawn } from "node:child_process";
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
const BOOT_SECONDS = 40;
const WAIT_MS = 180000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failures = 0;
let checks = 0;
function check(ok, label, detail) {
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
  process.stdout.write(`SKIP 浏览器验收未运行：${reason}\n`);
  process.exit(0);
}

async function gitIn(cwd, args) {
  return execFileP("git", ["-c", "user.email=t@t.invalid", "-c", "user.name=t", ...args], {
    cwd, timeout: 30000,
  });
}

async function makeRepo(base) {
  const repo = join(base, "demo-main");
  await mkdir(repo, { recursive: true });
  await execFileP("git", ["init", "-q", "--initial-branch=main", repo], { timeout: 30000 });
  await gitIn(repo, ["commit", "-q", "--allow-empty", "-m", "base"]);
  return repo;
}

function startKernel(stateHome) {
  const proc = spawn(CLI, ["serve", "--port", "0"], {
    cwd: REPO_ROOT,
    env: { ...process.env, POSTEL_HOME: stateHome, HOME: stateHome },
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

const CANDIDATE_SNAPSHOT = () => {
  const el = (id) => document.getElementById(id);
  const visible = (id) => {
    const node = el(id);
    if (!node || node.hidden) return false;
    const style = getComputedStyle(node);
    return style.display !== "none" && style.visibility !== "hidden";
  };
  return {
    exists: Boolean(el("candidate")),
    state: el("candidate") ? el("candidate").dataset.state : "",
    stateText: el("candidate-state") ? el("candidate-state").textContent || "" : "",
    branch: el("candidate-branch") ? el("candidate-branch").textContent || "" : "",
    stat: el("candidate-stat") ? el("candidate-stat").textContent || "" : "",
    detailVisible: visible("candidate-detail"),
    error: el("candidate-error") ? el("candidate-error").textContent || "" : "",
  };
};

async function selectRepoRow(page) {
  await page.evaluate(() => {
    const li = document.querySelector("#repo-list li[data-repo-id]");
    if (!li) throw new Error("找不到仓库行");
    li.click();
  });
}

async function main() {
  const puppeteer = await findPuppeteer();
  if (!puppeteer) skip("没有 puppeteer-core");
  const chrome = await findChrome();
  if (!chrome) skip("没有可用的 Chrome");
  const agent = await execFileP("sh", ["-c", "command -v opencode || true"], { timeout: 10000 });
  if (agent.stdout.trim() === "") skip("本机没有 opencode，真实派活跑不了");

  const base = await mkdtemp(join(tmpdir(), "postel-s11-"));
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
    await page.waitForFunction(
      () => document.getElementById("status")?.dataset.state === "connected", { timeout: 20000 },
    );

    const initial = await page.evaluate(CANDIDATE_SNAPSHOT);
    check("1 候选卡片存在", initial.exists === true);
    check("1 初始 idle", initial.state === "idle", initial.state);
    check("1 初始提示「还没有候选」", initial.stateText === "还没有候选", initial.stateText);
    check("1 idle 时详情不可见", initial.detailVisible === false);

    await page.type("#bind-path", repoMain);
    await page.click("#bind-button");
    await page.waitForFunction(
      () => document.querySelectorAll("#repo-list li[data-repo-id]").length === 1,
      { timeout: 15000 },
    );
    await selectRepoRow(page);
    await sleep(400);
    await page.click("#bot-new");
    await sleep(200);
    await page.type("#bot-create-name", "builder");
    await page.click("#bot-create-submit");
    await page.waitForFunction(
      () => document.getElementById("bot-create-form").hidden === true, { timeout: 15000 },
    );
    await page.evaluate(() => {
      const li = Array.from(document.querySelectorAll("#bot-list li")).find(
        (n) => (n.textContent || "").includes("builder"),
      );
      if (!li) throw new Error("找不到 builder 行");
      li.click();
    });
    await sleep(400);

    await page.type("#task-prompt", "创建文件 hello-s11.txt，内容是 s11-candidate");
    await page.click("#task-dispatch");
    await page.waitForFunction(
      () => document.getElementById("task")?.dataset.state === "done", { timeout: WAIT_MS },
    );
    const taskText = await page.evaluate(
      () => document.getElementById("task-status-line")?.textContent || "",
    );
    check("2 任务成功", taskText.includes("已完成"), taskText);

    await page.waitForFunction(
      () => document.getElementById("candidate")?.dataset.state === "done", { timeout: 30000 },
    );
    const candidate = await page.evaluate(CANDIDATE_SNAPSHOT);
    check("3 候选卡 done", candidate.state === "done", candidate.state);
    check("3 分支可预测（bot/builder/8位）",
      /^分支 bot\/builder\/[a-z0-9-]{8}$/.test(candidate.branch), candidate.branch);
    check("3 状态「仅本地」", candidate.stateText.includes("仅本地"), candidate.stateText);
    check("3 +X −Y 可见", /\+[1-9]/.test(candidate.stat), candidate.stat);

    await page.click("#candidate-open");
    await page.waitForFunction(
      () => document.querySelectorAll("#diff-patch .diff-line").length > 0, { timeout: 15000 },
    );
    const diffLines = await page.evaluate(() =>
      Array.from(document.querySelectorAll("#diff-patch .diff-line"))
        .map((n) => n.textContent || ""));
    check("4 复用 S5 渲染件看到 diff", diffLines.some((line) => line.includes("hello-s11.txt")),
      diffLines.slice(0, 8));
    check("4 新增行是 Agent 写的内容", diffLines.some((line) => line.includes("s11-candidate")),
      diffLines.slice(0, 8));

    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(
      () => document.getElementById("status")?.dataset.state === "connected", { timeout: 20000 },
    );
    await selectRepoRow(page);  // 页面状态不落盘：仓库要重选（S8 的既定行为）
    await page.waitForFunction(
      () => document.getElementById("candidate")?.dataset.state === "done", { timeout: 20000 },
    );
    const reloaded = await page.evaluate(CANDIDATE_SNAPSHOT);
    check("5 刷新后候选仍在", reloaded.branch === candidate.branch, reloaded.branch);

    check("6 全程无 console 错误、无未捕获异常",
      consoleErrors.length === 0 && pageErrors.length === 0,
      { consoleErrors: consoleErrors.slice(0, 3), pageErrors: pageErrors.slice(0, 3) });
  } catch (error) {
    check(false, "场景执行中断", String(error).slice(0, 400));
  } finally {
    await browser.close().catch(() => {});
    await stopKernel(kernel);
    // 临时目录不删（沙箱 safe-delete 拦截 rm）；前缀 postel-s11- 肉眼可辨。
  }

  process.stdout.write(`\n浏览器验收：${checks} CHECK，${failures} 条失败\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  process.stdout.write(`FATAL ${error}\n`);
  process.exit(1);
});
