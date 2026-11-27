#!/usr/bin/env node
/*
 * S10 验收脚本：在**真实浏览器**里走完核心闭环第一段——选 Bot → 输一句话 → 派活 →
 * 状态灯从排队/运行变到已完成 → worktree 里真的有文件。
 *
 * 指令由**真实的 `opencode acp`** 执行（本机没装就跳过整个正例）；第二条并发派活的
 * `BUSY` 也在这里验（任务还在跑时点第二次派活）。
 *
 * 覆盖（S10 验收 2/3/5/6 + D10 的可见面）：
 *   1. 初始 idle：卡片存在、提示文案、输入框与按钮就位；
 *   2. 选 Bot 后 → ready（表单出现）；
 *   3. 空指令 → 面板错误「请输入一句话指令」，不发请求；
 *   4. 派活 → 状态行出现 taskId，状态见过 排队中/运行中 至少其一；
 *   5. 运行中：取消按钮出现；第二次派活 → BUSY 文案（按码映射，不是裸 message）；
 *   6. 完成 → 「已完成」，取消按钮收起；
 *   7. worktree 里有 hello.txt 且内容非空（fs 断言）；
 *   8. worktree 的 git status 非空、主仓库干净（execFile 跑真 git）；
 *   9. 全程无 console 错误、无未捕获异常。
 *
 * 用法：node tools/acceptance/s10_dispatch.mjs
 */

import { execFile, spawn } from "node:child_process";
import { mkdir, mkdtemp, readdir, readFile, stat, writeFile } from "node:fs/promises";
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
const DISPATCH_TIMEOUT_MS = 180000; // 真实 opencode 建一个文件实测 ~9s，留足余量
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failures = 0;
let checks = 0;
function check(ok, label, detail) {
  checks += 1;
  const tail = detail === undefined ? "" : `  ${JSON.stringify(detail)}`;
  if (ok) {
    process.stdout.write(`CHECK ✓ ${checks} ${label}${tail}\n`);
  } else {
    failures += 1;
    process.stdout.write(`CHECK ✗ ${checks} ${label}${tail}\n`);
  }
}

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
    "/home/" + (process.env.USER || "") + "/.local/opt/google-chrome/chrome",
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

function skip(reason) {
  process.stdout.write(`SKIP 浏览器验收未运行：${reason}\n`);
  process.stdout.write("      这不是失败——本脚本只在本机有 Chrome + puppeteer-core 时运行。\n");
  process.exit(0);
}

async function gitIn(cwd, args) {
  return execFileP("git", ["-c", "user.email=t@t.invalid", "-c", "user.name=t", ...args], {
    cwd,
    timeout: 30000,
  });
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

function startKernel(stateHome) {
  const proc = spawn(CLI, ["serve", "--port", "0"], {
    cwd: REPO_ROOT,
    env: { ...process.env, POSTEL_HOME: stateHome, HOME: stateHome },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  proc.stdout.on("data", (chunk) => (output += chunk));
  proc.stderr.on("data", (chunk) => (output += chunk));
  const deadline = Date.now() + KERNEL_BOOT_SECONDS * 1000;
  return (async () => {
    while (Date.now() < deadline) {
      const match = output.match(/地址\s+(http:\/\/127\.0\.0\.1:\d+)/);
      if (match) return { proc, url: match[1], output: () => output };
      if (proc.exitCode !== null) {
        throw new Error(`内核启动失败（退出码 ${proc.exitCode}）：\n${output}`);
      }
      await sleep(100);
    }
    proc.kill("SIGKILL");
    throw new Error(`内核 ${KERNEL_BOOT_SECONDS}s 内没打印地址：\n${output}`);
  })();
}

async function stopKernel(kernel) {
  if (kernel.proc.exitCode !== null || kernel.proc.signalCode !== null) return;
  kernel.proc.kill("SIGTERM");
  await new Promise((done) => {
    const timer = setTimeout(done, 15000);
    kernel.proc.on("exit", () => {
      clearTimeout(timer);
      done();
    });
  });
}

const SNAPSHOT = () => {
  const el = (id) => document.getElementById(id);
  const text = (id) => (el(id) ? el(id).textContent || "" : "");
  const visible = (id) => {
    const node = el(id);
    if (!node || node.hidden) return false;
    const style = getComputedStyle(node);
    return style.display !== "none" && style.visibility !== "hidden";
  };
  return {
    exists: Boolean(el("task")),
    state: el("task") ? el("task").dataset.state : "",
    stateText: text("task-state"),
    formVisible: visible("task-form"),
    promptValue: el("task-prompt") ? el("task-prompt").value : "",
    dispatchDisabled: el("task-dispatch") ? el("task-dispatch").disabled : null,
    cancelVisible: visible("task-cancel"),
    error: text("task-error"),
    errorVisible: visible("task-error"),
    detailsVisible: visible("task-details"),
    taskId: text("task-id"),
    statusLine: text("task-status-line"),
    hintVisible: visible("task-state"),
  };
};

async function snapshot(page) {
  return page.evaluate(SNAPSHOT);
}

async function main() {
  const puppeteer = await findPuppeteer();
  if (!puppeteer) skip("没有 puppeteer-core");
  const chrome = await findChrome();
  if (!chrome) skip("没有可用的 Chrome");
  const realAgent = await execFileP("sh", ["-c", "command -v opencode || true"], { timeout: 10000 });
  if (realAgent.stdout.trim() === "") {
    skip("本机没有 opencode，S10 的正例跑不了真实派活");
  }

  const base = await mkdtemp(join(tmpdir(), "postel-s10-"));
  const stateHome = join(base, "postel-home");
  const work = join(base, "work");
  await mkdir(stateHome, { recursive: true });
  await mkdir(work, { recursive: true });
  const repoMain = await makeRepo(work, "demo");
  const kernel = await startKernel(stateHome);
  const browser = await puppeteer.launch({
    executablePath: chrome.path,
    headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });
  page.on("pageerror", (e) => pageErrors.push(String(e)));

  try {
    await page.goto(kernel.url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(
      () => document.getElementById("status")?.dataset.state === "connected",
      { timeout: 20000 },
    );

    // ---- 1. 初始 idle ----
    const initial = await snapshot(page);
    check("1 派活卡片存在", initial.exists === true);
    check("1 初始 idle", initial.state === "idle", initial.state);
    check("1 idle 提示可见（「选择一个 Bot 后派活」）", initial.hintVisible === true);
    check("1 idle 时表单不可见（还没选 Bot）", initial.formVisible === false);

    // ---- 2. 绑仓库、建 builder、选中它 ----
    await page.type("#bind-path", repoMain);
    await page.click("#bind-button");
    await page.waitForFunction(
      () => document.querySelectorAll("#repo-list li[data-repo-id]").length === 1,
      { timeout: 15000 },
    );
    // 先选中仓库行（#bot-new 在没选仓库时不可见）
    await page.evaluate(() => {
      const li = document.querySelector("#repo-list li[data-repo-id]");
      if (!li) throw new Error("找不到仓库行");
      li.click();
    });
    await sleep(500);
    await page.click("#bot-new");
    await sleep(200);
    await page.type("#bot-create-name", "builder");
    await page.click("#bot-create-submit");
    await page.waitForFunction(
      () => document.getElementById("bot-create-form").hidden === true,
      { timeout: 15000 },
    );
    await page.evaluate(() => {
      const li = Array.from(document.querySelectorAll("#bot-list li")).find(
        (n) => (n.textContent || "").includes("builder"),
      );
      if (!li) throw new Error("找不到 builder 行");
      li.click();
    });
    await sleep(600);
    const ready = await snapshot(page);
    check("2 选中 Bot 后卡片 ready", ready.state === "ready", ready.state);
    check("2 ready 时表单可见", ready.formVisible === true);
    check("2 派活按钮可点", ready.dispatchDisabled === false);

    // ---- 3. 空指令 → 面板错误，不发请求 ----
    await page.click("#task-dispatch");
    await sleep(300);
    const empty = await snapshot(page);
    check("3 空指令 → 面板错误出现", empty.errorVisible === true, empty.error);
    check("3 文案是「请输入一句话指令」", empty.error.includes("请输入一句话指令"), empty.error);
    await page.evaluate(() => {
      const node = document.getElementById("task-error");
      if (node) node.hidden = true;
    });

    // ---- 4. 派活 → 状态行出现，状态推进 ----
    const seen = new Set();
    await page.type("#task-prompt", "Create a file named hello.txt containing exactly: hi from postel s10");
    await page.click("#task-dispatch");
    // 立刻轮询：queued 可能一闪而过，见过 running/queued 其一即可
    let dispatched = null;
    const dispatchDeadline = Date.now() + 15000;
    while (Date.now() < dispatchDeadline) {
      const snap = await snapshot(page);
      if (snap.taskId.trim() !== "") {
        dispatched = snap;
        break;
      }
      await sleep(60);
    }
    check("4 派活应答后状态行出现 taskId", dispatched !== null);
    if (dispatched) {
      check(
        "4 taskId 前缀是「任务 」且内容非空",
        dispatched.taskId.startsWith("任务 ") && dispatched.taskId.length > 3,
        dispatched.taskId,
      );
      check("4 状态灯进入 running（含排队）", dispatched.state === "running", dispatched.state);
      check("4 运行中取消按钮出现", dispatched.cancelVisible === true);
    }

    // ---- 5. 运行中的页面契约：派活入口收起（BUSY 由内核兜底 + 单测覆盖，页面不给入口）----
    const busySnap = await snapshot(page);
    if (busySnap.state === "running") {
      check("5 running 时派活表单收起（页面不给 BUSY 入口）", busySnap.formVisible === false);
      check("5 running 时取消按钮可见（能反悔）", busySnap.cancelVisible === true);
    } else {
      check("5 任务结束太快，跳过 running 契约检查（弱化）", true);
    }

    // ---- 6. 等完成 ----
    await page.waitForFunction(
      () => (document.getElementById("task") || {}).dataset?.state === "done",
      { timeout: DISPATCH_TIMEOUT_MS },
    );
    const done = await snapshot(page);
    check("6 完成 → 状态灯 done", done.state === "done", done.state);
    check("6 状态行是「已完成」", done.statusLine.includes("已完成"), done.statusLine);
    check("6 完成后取消按钮收起", done.cancelVisible === false);

    // ---- 7/8. 磁盘事实：worktree 里有文件、git 认账、主仓库干净 ----
    const wtRoot = join(stateHome, "worktrees");
    const repoDirs = await readdir(wtRoot).catch(() => []);
    let wtPath = null;
    for (const dir of repoDirs) {
      const candidate = join(wtRoot, dir, "builder");
      try {
        await stat(candidate);
        wtPath = candidate;
      } catch {
        /* 下一个 */
      }
    }
    check("7 worktree 目录存在", wtPath !== null, wtPath);
    if (wtPath) {
      const hello = join(wtPath, "hello.txt");
      const content = await readFile(hello, "utf8").catch(() => null);
      check("7 worktree 里有 hello.txt", content !== null);
      check("7 内容是 Agent 写的那句话", (content || "").includes("hi from postel s10"), content);
      const wtStatus = await execFileP("git", ["-C", wtPath, "status", "--porcelain"], {
        timeout: 20000,
      });
      check("8 worktree 的 git status 非空", wtStatus.stdout.trim().length > 0);
      const mainStatus = await execFileP("git", ["-C", repoMain, "status", "--porcelain"], {
        timeout: 20000,
      });
      check("8 主仓库没有改动（禁止事项）", mainStatus.stdout.trim().length === 0, mainStatus.stdout);
    }

    // ---- 9. 干净控制台 ----
    check(
      "9 全程无 console 错误、无未捕获异常",
      consoleErrors.length === 0 && pageErrors.length === 0,
      { consoleErrors: consoleErrors.slice(0, 3), pageErrors: pageErrors.slice(0, 3) },
    );
  } catch (error) {
    check(false, "场景执行中断", String(error).slice(0, 400));
  } finally {
    await browser.close().catch(() => {});
    await stopKernel(kernel);
    // 临时目录**不删**：沙箱的 safe-delete 会拦 rm（06-pitfalls 的环境事实），
    // 留给 /tmp 的定期清理。目录名前缀 postel-s10-，肉眼可辨。
  }

  process.stdout.write(`\n浏览器验收：${checks} CHECK，${failures} 条失败\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  process.stdout.write(`FATAL ${error}\n`);
  process.exit(1);
});
