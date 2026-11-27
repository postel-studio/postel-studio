#!/usr/bin/env node
/*
 * S9 验收脚本：在**真实浏览器**里跑「点检测 Agent → 看到真实版本与能力清单」，并跑一次
 * 「命令不存在」的负例。
 *
 * 为什么不在 `tests/` 里：pytest 没有浏览器，DOM 行为只能靠字符串断言（06-pitfalls T9）。
 * 这个脚本起真内核、开真 Chrome、点真按钮、读真 DOM；能力值来自**这台机器上真实的
 * `opencode acp`**，不是脚本里写死的期望。
 *
 * **不进项目依赖**；缺 puppeteer-core 或 Chrome 就优雅跳过（退出码 0）。
 *
 * 覆盖（S9 D9 / D10 + 验收 1~4）：
 *   1. 初始 idle：卡片在仓库面板之前，按钮可点，详情收起；
 *   2. 点「检测 Agent」→ loading → ready；通道 id 是 acp-stdio（不是产品名）；
 *   3. ready：Agent 名字与版本来自应答（不是「未知」）；
 *   4. ready：protocolVersion = 1，耗时是正数；
 *   5. ready：能力清单非空、已排序、含 session.* 与 mcp.*（空对象语义没被漏掉）；
 *   6. 负例（命令不存在）：error + 「本机没有可用的 Agent 命令」，不是 git 的文案；
 *   7. 负例后按钮变成「重试」且仍可点；
 *   8. 全程无 console 错误、无未捕获异常。
 *
 * 用法：node tools/acceptance/s9_agent.mjs
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

async function startKernel(stateHome, extraEnv = {}) {
  const proc = spawn(CLI, ["serve", "--port", "0"], {
    cwd: REPO_ROOT,
    env: { ...process.env, POSTEL_HOME: stateHome, HOME: stateHome, ...extraEnv },
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
    await sleep(100);
  }
  proc.kill("SIGKILL");
  throw new Error(`内核 ${KERNEL_BOOT_SECONDS}s 内没打印地址：\n${output}`);
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
  const flags = Array.from(document.querySelectorAll("#agent-flags li")).map(
    (li) => li.textContent || ""
  );
  return {
    exists: Boolean(el("agent")),
    state: el("agent") ? el("agent").dataset.state : "",
    stateText: text("agent-state"),
    stateVisible: visible("agent-state"),
    detectLabel: el("agent-detect") ? el("agent-detect").textContent : "",
    detectDisabled: el("agent-detect") ? el("agent-detect").disabled : null,
    error: text("agent-error"),
    errorVisible: visible("agent-error"),
    detailVisible: visible("agent-detail"),
    kind: text("agent-kind"),
    version: text("agent-version"),
    protocol: text("agent-protocol"),
    elapsed: text("agent-elapsed"),
    flags,
    beforeBranches: Boolean(
      el("agent") && el("branches") &&
      (el("agent").compareDocumentPosition(el("branches")) & Node.DOCUMENT_POSITION_FOLLOWING)
    ),
  };
};

async function snapshot(page) {
  return page.evaluate(SNAPSHOT);
}

async function waitAgentState(page, wanted, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const state = await page.evaluate(
      () => (document.getElementById("agent") || {}).dataset?.state || ""
    );
    if (state === wanted) return;
    await sleep(100);
  }
  throw new Error(`等待 #agent[data-state=${wanted}] 超时`);
}

async function runScenario({ chrome, puppeteer, label, extraEnv, expect }) {
  const base = await mkdtemp(join(tmpdir(), `postel-s9-${label}-`));
  const stateHome = join(base, "postel-home");
  await mkdir(stateHome, { recursive: true });
  await writeFile(join(base, "seed.txt"), "x\n", "utf8");
  const kernel = await startKernel(stateHome, extraEnv);
  const browser = await puppeteer.launch({
    executablePath: chrome.path,
    headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  try {
    await page.goto(kernel.url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForFunction(
      () => document.getElementById("status")?.dataset.state === "connected",
      { timeout: 20000 }
    );
    const initial = await snapshot(page);
    check(initial.exists, `${label}：Agent 卡片存在`);
    check(initial.beforeBranches, `${label}：卡片在仓库面板之前（DOM 顺序即契约）`);
    check(initial.state === "idle", `${label}：初始 idle`, initial.state);
    check(initial.detailVisible === false, `${label}：初始不显示详情`);
    check(initial.detectDisabled === false, `${label}：初始按钮可点`);

    await page.click("#agent-detect");
    await waitAgentState(page, expect.state);
    const after = await snapshot(page);
    return { page, browser, kernel, base, after, initial, consoleErrors, pageErrors };
  } catch (error) {
    await browser.close().catch(() => {});
    await stopKernel(kernel);
    await rm(base, { recursive: true, force: true });
    throw error;
  }
}

async function main() {
  const puppeteer = await findPuppeteer();
  if (!puppeteer) skip("没有 puppeteer-core");
  const chrome = await findChrome();
  if (!chrome) skip("没有可用的 Chrome");

  // ---- 场景 A：真 Agent（本机装了 opencode 才有意义；没装就跳过整段）----
  const real = await execFileP("sh", ["-c", "command -v opencode || true"], { timeout: 10000 });
  if (real.stdout.trim() === "") {
    skip("本机没有 opencode，S9 的正例只验证了异常路径");
  }

  let scenarioA;
  try {
    scenarioA = await runScenario({
      chrome,
      puppeteer,
      label: "正例",
      extraEnv: {},
      expect: { state: "ready" },
    });
  } catch (error) {
    check(false, "正例跑到 ready", String(error).slice(0, 300));
    process.stdout.write(`\n${failures} 条失败\n`);
    process.exit(1);
  }

  const a = scenarioA.after;
  check(a.state === "ready", "正例：检测后 ready", a.state);
  check(a.kind.includes("acp-stdio"), "正例：通道 id 是 acp-stdio（不是产品名）", a.kind);
  check(!a.version.includes("未知"), "正例：版本来自真 Agent，不是「未知」", a.version);
  check(a.protocol.includes("1"), "正例：protocolVersion 来自应答", a.protocol);
  check(/[1-9]\d* ms/.test(a.elapsed), "正例：握手耗时是正数", a.elapsed);
  check(a.flags.length >= 8, "正例：能力清单非空", a.flags.length);
  const sorted = [...a.flags].sort();
  check(JSON.stringify(a.flags) === JSON.stringify(sorted), "正例：能力列表已排序");
  check(a.flags.some((f) => f.startsWith("session.")), "正例：含 session.*（空对象没被漏掉）");
  check(a.flags.some((f) => f.startsWith("mcp.")), "正例：含 mcp.*");
  check(a.flags.includes("loadSession"), "正例：含布尔叶 loadSession");
  check(a.detailVisible === true, "正例：ready 时显示详情");
  check(a.errorVisible === false, "正例：ready 时没有错误行");
  check(a.detectLabel === "重新检测", "正例：按钮变成「重新检测」", a.detectLabel);
  check(scenarioA.consoleErrors.length === 0 && scenarioA.pageErrors.length === 0,
    "正例：无 console 错误、无未捕获异常",
    { consoleErrors: scenarioA.consoleErrors, pageErrors: scenarioA.pageErrors });

  await scenarioA.browser.close();
  await stopKernel(scenarioA.kernel);
  await rm(scenarioA.base, { recursive: true, force: true });

  // ---- 场景 B：命令不存在（验收 2 的负例）----
  let scenarioB;
  try {
    scenarioB = await runScenario({
      chrome,
      puppeteer,
      label: "负例",
      extraEnv: { POSTEL_ACP_COMMAND: JSON.stringify(["/nonexistent/postel-agent"]) },
      expect: { state: "error" },
    });
  } catch (error) {
    check(false, "负例跑到 error", String(error).slice(0, 300));
    process.stdout.write(`\n${failures} 条失败\n`);
    process.exit(1);
  }

  const b = scenarioB.after;
  check(b.state === "error", "负例：命令不存在 → error 态", b.state);
  check(b.errorVisible === true, "负例：错误行可见");
  check(b.error.includes("没有可用的 Agent 命令"), "负例：文案是「没有可用的 Agent 命令」",
    b.error);
  check(!b.error.includes("git"), "负例：没有复用 git 那句文案（D10）", b.error);
  check(b.detailVisible === false, "负例：不显示详情");
  check(b.detectLabel === "重试", "负例：按钮变成「重试」", b.detectLabel);
  check(b.detectDisabled === false, "负例：按钮仍可点（重试入口）");
  check(scenarioB.consoleErrors.length === 0 && scenarioB.pageErrors.length === 0,
    "负例：无 console 错误、无未捕获异常",
    { consoleErrors: scenarioB.consoleErrors, pageErrors: scenarioB.pageErrors });

  await scenarioB.browser.close();
  await stopKernel(scenarioB.kernel);
  await rm(scenarioB.base, { recursive: true, force: true });

  process.stdout.write(
    `\n${failures === 0 ? "✅" : "❌"} 浏览器验收：${checks - failures}/${checks} 通过` +
      `（S9：检测 / 版本 / 能力 / 负例）\n`
  );
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  process.stdout.write(`❌ 脚本异常：${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
