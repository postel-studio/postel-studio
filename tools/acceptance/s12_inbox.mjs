#!/usr/bin/env node
/*
 * S12 验收：真浏览器 + 真 opencode，收件箱空态 -> 派活后一条 -> 筛选 -> 点开详情 -> 刷新仍在。
 *
 * 覆盖验收 1–6：入口在仓库行下（无第 8 个 tab）、空态文案、行内容、状态筛选、
 * 点击复用 S11 详情、倒序与刷新后可见。多状态/倒序由 tests/test_inbox_list.py 覆盖。
 *
 * 用法：node tools/acceptance/s12_inbox.mjs
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

const INBOX_SNAPSHOT = () => {
  const el = (id) => document.getElementById(id);
  const visible = (id) => {
    const node = el(id);
    if (!node || node.hidden) return false;
    const style = getComputedStyle(node);
    return style.display !== "none" && style.visibility !== "hidden";
  };
  const inbox = el("inbox");
  return {
    exists: Boolean(inbox),
    state: inbox ? inbox.dataset.state : "",
    stateText: el("inbox-state") ? el("inbox-state").textContent || "" : "",
    emptyText: el("inbox-empty") ? el("inbox-empty").textContent || "" : "",
    emptyVisible: visible("inbox-empty"),
    rows: Array.from(document.querySelectorAll("#inbox-list li")).map((li) =>
      ({ taskId: li.dataset.taskId || "", text: li.textContent || "" })),
    entryLinks: Array.from(document.querySelectorAll(".repo-inbox-link")).map((n) =>
      n.textContent || ""),
    tabs: Array.from(document.querySelectorAll('[role="tab"]')).length,
    filter: el("inbox-filter") ? el("inbox-filter").value : "",
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

  const base = await mkdtemp(join(tmpdir(), "postel-s12-"));
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

    const initial = await page.evaluate(INBOX_SNAPSHOT);
    check("1 收件箱面板存在", initial.exists === true);
    check("1 初始 idle", initial.state === "idle", initial.state);
    check("1 仓库未绑定时没有入口链接", initial.entryLinks.length === 0, initial.entryLinks);
    check("1 无第 8 个一级 tab", initial.tabs === 0, initial.tabs);

    await page.type("#bind-path", repoMain);
    await page.click("#bind-button");
    await page.waitForFunction(
      () => document.querySelectorAll("#repo-list li[data-repo-id]").length === 1,
      { timeout: 15000 },
    );
    const withRepo = await page.evaluate(INBOX_SNAPSHOT);
    check("2 入口在仓库条目下", withRepo.entryLinks.length === 1
      && withRepo.entryLinks[0] === "变更收件箱", withRepo.entryLinks);

    // 点整行选中仓库（不是入口），此时 0 条候选 → 真实空态。
    await selectRepoRow(page);
    await page.waitForFunction(
      () => ["empty", "ready", "error"].includes(
        document.getElementById("inbox")?.dataset.state || ""), { timeout: 15000 },
    );
    const empty = await page.evaluate(INBOX_SNAPSHOT);
    check("3 0 条候选 → empty 态", empty.state === "empty", empty.state);
    check("3 空态文案「暂无待审变更」", empty.emptyText === "暂无待审变更"
      && empty.emptyVisible === true, { text: empty.emptyText, visible: empty.emptyVisible });
    check("3 空态不是空白列表", empty.rows.length === 0 && empty.emptyVisible === true);

    // 建 Bot + 派活 → 真实候选。
    await sleep(200);
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
    await sleep(300);
    await page.type("#task-prompt", "创建文件 hello-s12.txt，内容是 s12-inbox");
    await page.click("#task-dispatch");
    await page.waitForFunction(
      () => document.getElementById("task")?.dataset.state === "done", { timeout: WAIT_MS },
    );
    await page.waitForFunction(
      () => document.getElementById("inbox")?.dataset.state === "ready", { timeout: 30000 },
    );
    const one = await page.evaluate(INBOX_SNAPSHOT);
    check("4 1 条候选 → ready", one.state === "ready", one.state);
    check("4 行有标题/Bot/状态/统计/时间",
      one.rows.length === 1 && one.rows[0].text.includes("候选 · builder")
      && one.rows[0].text.includes("仅本地") && /\+\d+ −\d+/.test(one.rows[0].text)
      && /(刚刚|分钟前|小时前|天前)/.test(one.rows[0].text), one.rows);
    check("4 行带 taskId（点击详情用）", one.rows[0].taskId.length > 0, one.rows[0].taskId);

    // 筛选：仅本地（刚派活的候选状态就是 local_only）应仍有 1 条。
    await page.select("#inbox-filter", "local_only");
    await page.waitForFunction(
      () => document.getElementById("inbox")?.dataset.state === "ready", { timeout: 15000 },
    );
    const filtered = await page.evaluate(INBOX_SNAPSHOT);
    check("5 状态筛选「仅本地」返回 1 条", filtered.rows.length === 1, filtered.rows);
    check("5 筛选值发送正确", filtered.filter === "local_only", filtered.filter);
    await page.select("#inbox-filter", "pr_open");
    await page.waitForFunction(
      () => document.getElementById("inbox")?.dataset.state === "empty", { timeout: 15000 },
    );
    const noMatch = await page.evaluate(INBOX_SNAPSHOT);
    check("5 筛到 0 条时回 empty 态", noMatch.state === "empty", noMatch.state);
    await page.select("#inbox-filter", "all");
    await page.waitForFunction(
      () => document.getElementById("inbox")?.dataset.state === "ready", { timeout: 15000 },
    );

    // 点行 → S11 详情卡（复用，不另画）。
    await page.evaluate(() => {
      const li = document.querySelector("#inbox-list li[data-task-id]");
      if (!li) throw new Error("收件箱没有行");
      li.click();
    });
    await page.waitForFunction(
      () => document.getElementById("candidate")?.dataset.state === "local_only", { timeout: 15000 },
    );
    const detail = await page.evaluate(() => ({
      branch: document.getElementById("candidate-branch")?.textContent || "",
      state: document.getElementById("candidate")?.dataset.state || "",
    }));
    check("6 点击行复用 S11 详情", detail.state === "local_only"
      && /^分支 bot\/builder\//.test(detail.branch), detail);

    // 刷新 + 重选仓库 → 列表仍在（页面状态不落盘，同 S8/S11）。
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(
      () => document.getElementById("status")?.dataset.state === "connected", { timeout: 20000 },
    );
    await selectRepoRow(page);
    await page.waitForFunction(
      () => ["ready", "empty", "error"].includes(
        document.getElementById("inbox")?.dataset.state || ""), { timeout: 15000 },
    );
    const reloaded = await page.evaluate(INBOX_SNAPSHOT);
    check("6 刷新后候选仍在收件箱", reloaded.rows.length === 1, reloaded.rows);

    check("7 全程无 console 错误、无未捕获异常",
      consoleErrors.length === 0 && pageErrors.length === 0,
      { consoleErrors: consoleErrors.slice(0, 3), pageErrors: pageErrors.slice(0, 3) });
  } catch (error) {
    check(false, "场景执行中断", String(error).slice(0, 400));
  } finally {
    await browser.close().catch(() => {});
    await stopKernel(kernel);
    // 临时目录不删（沙箱 safe-delete 拦截 rm）；前缀 postel-s12- 肉眼可辨。
  }

  process.stdout.write(`\n浏览器验收：${checks} CHECK，${failures} 条失败\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  process.stdout.write(`FATAL ${error}\n`);
  process.exit(1);
});
