#!/usr/bin/env node
/*
 * S17 验收：真浏览器 + 真内核，看到「本次会注入哪些上下文」的清单。
 *
 * 覆盖验收 1/2/3 的用户可见层：面板存在、选中 Bot 后显示 charter 来源、
 * 仓库根 AGENTS.md 写进去后清单立刻多一条、冲突时页面明确警告。
 * 深层断言（字节数/截断/终态码）由 tests/test_injection.py 覆盖。
 *
 * 用法：node tools/acceptance/s17_context.mjs
 */

import { execFile, execFileSync, spawn } from "node:child_process";
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

const CONTEXT_SNAPSHOT = () => {
  const el = (id) => document.getElementById(id);
  const panel = el("context");
  return {
    exists: Boolean(panel),
    state: panel ? panel.dataset.state : "",
    stateText: el("context-state") ? el("context-state").textContent || "" : "",
    rows: Array.from(document.querySelectorAll("#context-list li")).map(
      (li) => ({
        text: li.textContent || "",
        truncated: li.dataset.truncated === "true",
      })),
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

  const base = await mkdtemp(join(tmpdir(), "postel-s17-"));
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
    const initial = await page.evaluate(CONTEXT_SNAPSHOT);
    check("1 上下文面板存在且初始 idle", initial.exists && initial.state === "idle", initial.state);

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
    await page.waitForFunction(
      () => document.getElementById("context")?.dataset.state === "ready", { timeout: 15000 },
    );
    const first = await page.evaluate(CONTEXT_SNAPSHOT);
    check("2 选中 Bot 后清单含 charter", first.rows.some((r) => r.text.includes("charter.md")),
      first.rows);
    check("2 清单含字节数", first.stateText.includes("注入"), first.stateText);

    // 写一个仓库根 AGENTS.md，重新选 Bot → 清单应立刻多一条（实时读取）。
    await writeFile(join(repoMain, "AGENTS.md"), "# browser probe\n", "utf8");
    await page.evaluate(() => {
      const li = Array.from(document.querySelectorAll("#bot-list li")).find(
        (n) => (n.textContent || "").includes("builder"));
      if (li) li.click();
    });
    await sleep(600);
    const second = await page.evaluate(CONTEXT_SNAPSHOT);
    check("3 仓库 AGENTS.md 实时出现在清单",
      second.rows.some((r) => r.text.includes("AGENTS.md (仓库根)")), second.rows);

    // 冲突：仓库 L1 + charter L2 → 页面明确警告。
    await writeFile(join(repoMain, "AGENTS.md"), "permission: L1\n", "utf8");
    const stateHomePath = stateHome;
    // charter 在状态根里，直接改文件让预览读到 L2（create 时已建好）。
    const charterCandidates = await import("node:fs/promises");
    const repoIdDirs = await charterCandidates.readdir(join(stateHomePath, "bots"));
    if (repoIdDirs.length === 1) {
      await charterCandidates.writeFile(
        join(stateHomePath, "bots", repoIdDirs[0], "builder", "charter.md"),
        "# builder\n权限: L2\n", "utf8",
      );
    }
    await page.evaluate(() => {
      const li = Array.from(document.querySelectorAll("#bot-list li")).find(
        (n) => (n.textContent || "").includes("builder"));
      if (li) li.click();
    });
    await sleep(600);
    const conflict = await page.evaluate(CONTEXT_SNAPSHOT);
    check("4 冲突时页面明确警告", conflict.stateText.includes("冲突"), conflict.stateText);

    check("5 全程无 console 错误、无未捕获异常",
      consoleErrors.length === 0 && pageErrors.length === 0,
      { consoleErrors: consoleErrors.slice(0, 3), pageErrors: pageErrors.slice(0, 3) });
  } catch (error) {
    check("场景执行中断", false, String(error).slice(0, 400));
  } finally {
    await browser.close().catch(() => {});
    await stopKernel(kernel);
    // 临时目录不删（沙箱 safe-delete 拦截 rm）；前缀 postel-s17- 肉眼可辨。
  }

  process.stdout.write(`\n浏览器验收：${checks} CHECK，${failures} 条失败\n`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  process.stdout.write(`FATAL ${error}\n`);
  process.exit(1);
});
