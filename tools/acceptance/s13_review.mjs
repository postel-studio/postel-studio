#!/usr/bin/env node
/*
 * S13 验收：真浏览器 + 真 opencode，批准/拒绝改变状态，理由落库，默认列表隐藏已拒绝，无合并按钮。
 *
 * 覆盖验收 1–6：入口在仓库行下（无第 8 个 tab）、空态文案、行内容、状态筛选、
 * 点击复用 S11 详情、倒序与刷新后可见。多状态/倒序由 tests/test_inbox_list.py 覆盖。
 *
 * 用法：node tools/acceptance/s13_review.mjs
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
function check(a, b, detail) {
  // Accept both check(ok, label) and check(label, ok): earlier s12/s13 calls
  // passed the label first, which made every check silently green.
  const ok = typeof a === "string" ? b : a;
  const label = typeof a === "string" ? a : b;
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

const REVIEW_SNAPSHOT = () => {
  const el = (id) => document.getElementById(id);
  const cand = el("candidate");
  return {
    exists: Boolean(cand),
    state: cand ? cand.dataset.state : "",
    stateText: el("candidate-state") ? el("candidate-state").textContent || "" : "",
    approve: el("candidate-approve") ? {
      disabled: el("candidate-approve").disabled,
      text: el("candidate-approve").textContent || "",
    } : null,
    reject: el("candidate-reject") ? {
      disabled: el("candidate-reject").disabled,
      text: el("candidate-reject").textContent || "",
    } : null,
    reasonNote: el("candidate-reason-note") ? el("candidate-reason-note").textContent || "" : "",
    rejectReason: el("candidate-reject-reason") ? el("candidate-reject-reason").textContent || "" : "",
    mergeButtons: Array.from(document.querySelectorAll("button")).filter(
      (b) => (b.textContent || "").includes("合并")).length,
    inboxRows: Array.from(document.querySelectorAll("#inbox-list li")).map(
      (li) => li.dataset.taskId || ""),
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

  const base = await mkdtemp(join(tmpdir(), "postel-s13-"));
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

    const initial = await page.evaluate(REVIEW_SNAPSHOT);
    check("1 审核按钮存在", initial.approve !== null && initial.reject !== null,
      { approve: initial.approve, reject: initial.reject });
    check("1 按钮文案是批准 / 拒绝并说明",
      initial.approve.text === "批准" && initial.reject.text === "拒绝并说明",
      { approve: initial.approve.text, reject: initial.reject.text });
    check("1 无「合并」按钮", initial.mergeButtons === 0, initial.mergeButtons);

    await page.type("#bind-path", repoMain);
    await page.click("#bind-button");
    await page.waitForFunction(
      () => document.querySelectorAll("#repo-list li[data-repo-id]").length === 1,
      { timeout: 15000 },
    );
    await selectRepoRow(page);
    await page.waitForFunction(
      () => document.getElementById("bots")?.dataset.state === "ready", { timeout: 15000 },
    );
    await page.click("#bot-new");
    await page.waitForFunction(
      () => document.getElementById("bot-create-form")?.hidden === false, { timeout: 5000 },
    );
    await page.type("#bot-create-name", "builder");
    // 默认 L1：审核按钮应可用（D4 权限提示）。
    await page.click("#bot-create-submit");
    await page.waitForFunction(
      () => document.getElementById("bot-create-form").hidden === true, { timeout: 15000 },
    );
    await page.evaluate(() => {
      const li = Array.from(document.querySelectorAll("#bot-list li")).find(
        (n) => (n.textContent || "").includes("builder"));
      if (!li) throw new Error("找不到 builder 行");
      li.click();
    });
    await sleep(300);
    const ready = await page.evaluate(REVIEW_SNAPSHOT);
    check("2 L1 Bot 的审核按钮可用", ready.approve.disabled === false
      && ready.reject.disabled === false, ready.approve);
    check("2 权限原因文案为空", ready.reasonNote === "", ready.reasonNote);

    const preTask = await page.evaluate(() => ({
      task: document.getElementById("task")?.dataset.state || "",
      taskText: document.getElementById("task-prompt")?.textContent || "",
      workspace: document.getElementById("workspace")?.dataset.state || "",
      bot: Array.from(document.querySelectorAll("#bot-list li")).map(
        (li) => li.textContent || ""),
    }));
    process.stdout.write(`DEBUG preTask ${JSON.stringify(preTask)}\n`);
    await page.type("#task-prompt", "创建文件 hello-s13.txt，内容是 s13-review");
    await page.click("#task-dispatch");
    await page.waitForFunction(
      () => document.getElementById("task")?.dataset.state === "done", { timeout: WAIT_MS },
    ).catch(async (e) => {
      const state = await page.evaluate(() => ({
        task: document.getElementById("task")?.dataset.state || "",
        text: document.getElementById("task-status-line")?.textContent || "",
        error: document.getElementById("task-error")?.textContent || "",
      }));
      process.stdout.write(`DEBUG after dispatch ${JSON.stringify(state)}\n`);
      throw e;
    });
    // S13：data-state 直接是 reviewState（local_only），不再是 S12 的 done。
    await page.waitForFunction(
      () => document.getElementById("candidate")?.dataset.state === "local_only",
      { timeout: 30000 },
    );
    await page.waitForFunction(
      () => document.getElementById("inbox")?.dataset.state === "ready", { timeout: 15000 },
    );
    const before = await page.evaluate(REVIEW_SNAPSHOT);
    check("3 新候选默认 local_only", before.state === "local_only", before.state);
    check("3 收件箱有 1 行", before.inboxRows.length === 1, before.inboxRows);

    // 拒绝 + 理由 → 状态变 rejected，理由显示，默认列表隐藏。
    await page.type("#candidate-reason", "测试退回：内容不完整");
    await page.click("#candidate-reject");
    await page.waitForFunction(
      () => document.getElementById("candidate")?.dataset.state === "rejected",
      { timeout: 15000 },
    );
    await page.waitForFunction(
      () => document.getElementById("inbox")?.dataset.state === "empty", { timeout: 15000 },
    );
    const rejected = await page.evaluate(REVIEW_SNAPSHOT);
    check("4 拒绝后状态 rejected", rejected.state === "rejected", rejected.state);
    check("4 理由显示在详情卡", rejected.rejectReason.includes("测试退回：内容不完整"),
      rejected.rejectReason);
    check("4 默认列表隐藏已拒绝", rejected.inboxRows.length === 0, rejected.inboxRows);
    check("4 无合并按钮", rejected.mergeButtons === 0, rejected.mergeButtons);

    // 筛选「已拒绝」仍可见。
    await page.select("#inbox-filter", "rejected");
    await page.waitForFunction(
      () => document.getElementById("inbox")?.dataset.state === "ready", { timeout: 15000 },
    );
    const filtered = await page.evaluate(REVIEW_SNAPSHOT);
    check("5 筛选已拒绝可见", filtered.inboxRows.length === 1, filtered.inboxRows);
    await page.select("#inbox-filter", "all");
    await sleep(300);

    // 刷新后重选仓库：拒绝理由持久化（DB，不是内存）。
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(
      () => document.getElementById("status")?.dataset.state === "connected", { timeout: 20000 },
    );
    await selectRepoRow(page);
    await page.waitForFunction(
      () => ["rejected", "done", "empty"].includes(
        document.getElementById("candidate")?.dataset.state || ""), { timeout: 15000 },
    );
    const reloaded = await page.evaluate(REVIEW_SNAPSHOT);
    check("6 刷新后仍是 rejected + 理由在", reloaded.state === "rejected"
      && reloaded.rejectReason.includes("测试退回"), { state: reloaded.state,
        reason: reloaded.rejectReason });

    check("7 全程无 console 错误、无未捕获异常",
      consoleErrors.length === 0 && pageErrors.length === 0,
      { consoleErrors: consoleErrors.slice(0, 3), pageErrors: pageErrors.slice(0, 3) });
  } catch (error) {
    check(false, "场景执行中断", String(error).slice(0, 400));
  } finally {
    await browser.close().catch(() => {});
    await stopKernel(kernel);
    // 临时目录不删（沙箱 safe-delete 拦截 rm）；前缀 postel-s13- 肉眼可辨。
  }

  process.stdout.write(`\n浏览器验收：${checks} CHECK，${failures} 条失败\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  process.stdout.write(`FATAL ${error}\n`);
  process.exit(1);
});
