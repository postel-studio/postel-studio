#!/usr/bin/env node
/*
 * S15 验收脚本：真浏览器里验证「内核被杀 → 重启 → 中断任务明示 + 显式重跑」。
 *
 * 流程：
 *   1. 起真内核（临时 POSTEL_HOME），建好 schema 后 SIGKILL（模拟崩溃）；
 *   2. 用 s15_seed.py 在临时库里造一个 running 任务（确定性，不依赖 opencode）；
 *   3. 重启内核 → mark_interrupted 把那条标成 interrupted；
 *   4. 真 Chrome 打开页面 → 选仓库/Bot → 页面 onopen 调 task.latest 恢复中断态；
 *   5. 断言 #task[data-state="interrupted"] + 状态行文案 + #task-rerun 可见 + 取消不可见；
 *   6. 点 #task-rerun → 新 taskId ≠ 旧 → 卡片不再停在 interrupted；
 *   7. 全程无 console 错误 / 未捕获异常；退出收内核，临时目录留给 /tmp 清理。
 *
 * 用法：node tools/acceptance/s15_recovery.mjs
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
const SEED = join(HERE, "s15_seed.py");
const PYTHON = join(REPO_ROOT, ".venv", "bin", "python");
const KERNEL_BOOT_SECONDS = 40;
const REPO_ID = "0123456789ab";
const BOT_ID = REPO_ID + "/builder";
const OLD_TASK_ID = "a".repeat(26);
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

function findPuppeteer() {
  const candidates = [
    "puppeteer-core",
    "/home/" + (process.env.USER || "") + "/.local/lib/node_modules/puppeteer-core",
    "/usr/lib/node_modules/puppeteer-core",
    "/usr/local/lib/node_modules/puppeteer-core",
  ];
  const require = createRequire(import.meta.url);
  for (const c of candidates) {
    try {
      return require(c);
    } catch {
      /* next */
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
  ].filter(Boolean);
  for (const c of candidates) {
    try {
      const { stdout } = await execFileP(c, ["--version"], { timeout: 20000 });
      return { path: c, version: stdout.trim() };
    } catch {
      /* next */
    }
  }
  return null;
}

function skip(reason) {
  process.stdout.write(`SKIP 浏览器验收未运行：${reason}\n`);
  process.stdout.write("      这不是失败——本脚本只在本机有 Chrome + puppeteer-core 时运行。\n");
  process.exit(0);
}

async function startKernel(stateHome) {
  const proc = spawn(CLI, ["serve", "--port", "0"], {
    cwd: REPO_ROOT,
    env: { ...process.env, POSTEL_HOME: stateHome, HOME: stateHome },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  proc.stdout.on("data", (c) => (output += c));
  proc.stderr.on("data", (c) => (output += c));
  const deadline = Date.now() + KERNEL_BOOT_SECONDS * 1000;
  while (Date.now() < deadline) {
    const m = output.match(/地址\s+(http:\/\/127\.0\.0\.1:\d+)/);
    if (m) return { proc, url: m[1] };
    if (proc.exitCode !== null) throw new Error(`内核启动失败：${output}`);
    await sleep(100);
  }
  proc.kill("SIGKILL");
  throw new Error(`内核 ${KERNEL_BOOT_SECONDS}s 内没打印地址`);
}

async function stopKernel(kernel) {
  if (!kernel) return;
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

async function main() {
  const puppeteer = findPuppeteer();
  const chrome = await findChrome();
  if (!puppeteer || !chrome) skip(!chrome ? "没找到 Chrome" : "没找到 puppeteer-core");
  process.stdout.write(`Chrome: ${chrome.version}\n`);

  const work = await mkdtemp(join(tmpdir(), "postel-s15-"));
  const home = join(work, "home");
  await mkdir(home, { recursive: true });
  await mkdir(join(home, "repo"), { recursive: true });

  let first = null;
  let second = null;
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
    // 1. 第一任内核 → SIGKILL（不给收场机会）
    first = await startKernel(home);
    check(true, "第一任内核启动", first.url);
    first.proc.kill("SIGKILL");
    await new Promise((done) => first.proc.on("exit", done));
    check(true, "内核被 SIGKILL（模拟崩溃）");

    // 2. 在临时库里造 running 任务
    await execFileP(PYTHON, [SEED, join(home, "state.db"), REPO_ID, BOT_ID], {
      cwd: REPO_ROOT,
      timeout: 30000,
    });
    check(true, "临时库里造 running 任务");

    // 3. 重启内核 → mark_interrupted
    second = await startKernel(home);
    check(true, "第二任内核启动并完成恢复记账", second.url);

    // 4. 浏览器打开 → 连上
    await page.goto(second.url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(
      () => document.getElementById("status")?.dataset.state === "connected",
      { timeout: 20000 },
    );
    check(true, "浏览器连上内核");

    // 选仓库 → 选 Bot（触发 restoreInterruptedTask）
    await page.waitForFunction(
      (repoId) => !!document.querySelector(`li[data-repo-id="${repoId}"]`),
      { timeout: 10000 },
      REPO_ID,
    );
    await page.evaluate((repoId) => {
      const li = document.querySelector(`li[data-repo-id="${repoId}"]`);
      if (li) li.click();
    }, REPO_ID);
    await page.waitForFunction(
      (botId) => !!document.querySelector(`[data-bot-id="${botId}"]`),
      { timeout: 10000 },
      BOT_ID,
    );
    await page.evaluate((botId) => {
      document.querySelector(`[data-bot-id="${botId}"]`).click();
    }, BOT_ID);

    // 5. 等 interrupted 态出现（onopen 时 task.latest 恢复 + 选 Bot 时也恢复）
    await page.waitForFunction(
      () => document.getElementById("task")?.dataset.state === "interrupted",
      { timeout: 10000 },
    );
    const stateText = await page.$eval("#task-status-line", (el) => el.textContent || "");
    check(stateText.includes("被打断"), "UI 明示「被打断」", stateText);
    const rerunVisible = await page.$eval("#task-rerun", (el) => !el.hidden);
    check(rerunVisible, "重跑按钮可见");
    const cancelHidden = await page.$eval("#task-cancel", (el) => el.hidden);
    check(cancelHidden, "取消按钮不可见");

    // 6. 点重跑 → 新 taskId（≠ 旧）
    await page.click("#task-rerun");
    await page.waitForFunction(
      (old) => {
        const line = document.getElementById("task-id")?.textContent || "";
        return line.includes("任务 ") && !line.includes(old);
      },
      { timeout: 10000 },
      OLD_TASK_ID,
    );
    const newLine = await page.$eval("#task-id", (el) => el.textContent || "");
    check(true, "重跑生成新 taskId（不复用旧 id）", newLine);

    // 7. 干净控制台
    check(
      consoleErrors.length === 0 && pageErrors.length === 0,
      "10 全程无 console 错误 / 未捕获异常",
      { consoleErrors: consoleErrors.slice(0, 3), pageErrors: pageErrors.slice(0, 3) },
    );
  } catch (error) {
    const diag = await page.evaluate(() => ({
      status: document.getElementById("status")?.dataset.state,
      repos: document.getElementById("repo-list")?.innerHTML.slice(0, 500),
      bots: document.getElementById("bot-list")?.innerHTML.slice(0, 500),
      task: document.getElementById("task")?.dataset.state,
      taskId: document.getElementById("task-id")?.textContent,
    })).catch(() => ({}));
    check(false, "场景执行中断", { error: String(error).slice(0, 300), diag });
  } finally {
    await browser.close().catch(() => {});
    await stopKernel(second);
    await stopKernel(first);
    // 临时目录不删（沙箱 safe-delete 会拦 rm），留 /tmp 定期清理。
    process.stdout.write(`临时目录（未删）：${work}\n`);
  }

  process.stdout.write(`\n浏览器验收：${checks} CHECK，${failures} 条失败\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((e) => {
  process.stdout.write(`FATAL ${e}\n`);
  process.exit(1);
});
