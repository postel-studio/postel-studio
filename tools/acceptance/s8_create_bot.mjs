#!/usr/bin/env node
/*
 * S8 验收脚本：在**真实浏览器**里跑「新建 Bot → 左栏出现它 → 刷新后仍在 → 改权限」。
 *
 * 为什么它不在 `tests/` 里：pytest 没有浏览器，前端 DOM 行为只能靠文本断言
 * （06-pitfalls T9 的老问题）。这个脚本补的就是那一块：起真内核、开真 Chrome、点真按钮、
 * 读真 DOM，并用 Node 的 fs 直接核对**磁盘上的事实**（记忆目录与 charter 真的建出来了）。
 *
 * **不进项目依赖**（允许入库，但不得进 pyproject.toml、不得引入前端构建链）。缺 puppeteer-core
 * 或 Chrome 就**优雅跳过**并说明原因，退出码 0（跳过不是失败），所以它永远不会拖住 CI。
 *
 * 覆盖（S8 D8 的 DOM 契约 + 验收 1~7）：
 *   0. 初始 idle：没选仓库时不给「新建 Bot」，表单收起，权限下拉不可见；
 *   1. 选仓库 → ready + 空列表 + 「这个仓库还没有 Bot。」+ 「新建 Bot」出现；
 *   2. 点「新建 Bot」→ 表单出现、名字框拿到焦点、默认选中 L1、下拉恰好四个选项（无 L3/L4）；
 *   3. 建 builder / L2 → 表单收起、左栏一行、徽标 L2、摘要「尚未填写职责」，磁盘上真有记忆目录；
 *   4. 非法名字（Builder）→ 说出规则的错误、表单还在、左栏没有多一行；
 *   5. 同名再建 → 「已经有叫这个名字的 Bot」，仍然只有一行；
 *   6. Escape 收起表单，焦点回到「新建 Bot」；
 *   7. 回车也能提交（真 <form>：回车与点按钮同一条路径）；
 *   8. 刷新页面 → 重新选中仓库，两个 Bot 都还在（持久化的真凭据）；
 *   9. 点 Bot → charter 面板的权限下拉显示它当前的级别；改成 L1 → 左栏徽标跟着变；
 *  10. 全程无 console 错误 / 无未捕获异常。
 *
 * 用法（详见 tools/acceptance/README.md）：
 *
 *     bash scripts/check.sh                 # 先保证代码是绿的
 *     node tools/acceptance/s8_create_bot.mjs
 *
 * 退出码：0 = 全部通过，或「环境不具备」而跳过；1 = 有断言失败（输出 `CHECK <n> ✗ ...`）。
 */

import { execFile, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readdir, rm, stat, writeFile } from "node:fs/promises";
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
    "/home/" + (process.env.USER || "") + "/.local/opt/google-chrome/chrome",
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

// ---------------------------------------------------------------------------
// 真仓库（`bot.create` 不读 git，但页面要先绑一个仓库才有 Bots 区）
// ---------------------------------------------------------------------------

async function gitIn(cwd, args) {
  return execFileP("git", ["-c", "user.email=a@b.c", "-c", "user.name=a", ...args], {
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

// ---------------------------------------------------------------------------
// DOM 快照：一次读全，断言只看这份快照（不给页面"边读边变"的机会）
// ---------------------------------------------------------------------------

const SNAPSHOT = () => {
  const text = (id) => {
    const el = document.getElementById(id);
    return el ? el.textContent || "" : "";
  };
  const visible = (id) => {
    const el = document.getElementById(id);
    if (!el) return false;
    if (el.hidden) return false;
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden";
  };
  const options = (id) => {
    const el = document.getElementById(id);
    if (!el) return [];
    return Array.from(el.options || []).map((option) => ({
      value: option.value,
      label: (option.textContent || "").trim(),
      selected: option.selected,
    }));
  };
  return {
    botsState: document.getElementById("bots").dataset.state,
    botsStateText: text("bots-state"),
    botsStateVisible: visible("bots-state"),
    botsError: text("bots-error"),
    botsErrorVisible: visible("bots-error"),
    botEmptyVisible: visible("bot-empty"),
    newButtonVisible: visible("bot-new"),
    formVisible: visible("bot-create-form"),
    formHiddenAttr: document.getElementById("bot-create-form").hidden,
    nameValue: document.getElementById("bot-create-name").value,
    nameFocused: document.activeElement === document.getElementById("bot-create-name"),
    newFocused: document.activeElement === document.getElementById("bot-new"),
    createError: text("bot-create-error"),
    createErrorVisible: visible("bot-create-error"),
    createOptions: options("bot-create-permission"),
    createPermission: document.getElementById("bot-create-permission").value,
    submitDisabled: document.getElementById("bot-create-submit").disabled,
    rows: Array.from(document.querySelectorAll("#bot-list li[data-bot-id]")).map((li) => ({
      botId: li.dataset.botId,
      name: (li.querySelector(".bot-name") || {}).textContent || "",
      permission: (li.querySelector(".bot-permission") || {}).textContent || "",
      summary: (li.querySelector(".bot-charter-summary") || {}).textContent || "",
      selected: li.dataset.selected === "true",
    })),
    charterState: document.getElementById("charter").dataset.state,
    charterTitle: text("charter-title"),
    charterError: text("charter-error"),
    charterErrorVisible: visible("charter-error"),
    memory: text("charter-memory"),
    memoryVisible: visible("charter-memory"),
    content: text("charter-content"),
    contentVisible: visible("charter-content"),
    permissionRowVisible: visible("charter-permission-row"),
    permissionOptions: options("charter-permission"),
    permissionValue: document.getElementById("charter-permission").value,
    workspaceBot: text("workspace-bot"),
    repoRows: Array.from(document.querySelectorAll("#repo-list li[data-repo-id]")).map((li) => ({
      repoId: li.dataset.repoId,
      name: (li.querySelector(".repo-name") || {}).textContent || "",
    })),
  };
};

async function snapshot(page) {
  return page.evaluate(SNAPSHOT);
}

async function bindPath(page, path) {
  await page.type("#bind-path", path);
  await page.click("#bind-button");
  await sleep(400);
}

async function waitBotsState(page, state) {
  await page.waitForFunction(
    (want) => document.getElementById("bots").dataset.state === want,
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
  await waitBotsState(page, "ready");
}

async function clickBotRow(page, name) {
  await page.evaluate((rowName) => {
    const li = Array.from(document.querySelectorAll("#bot-list li[data-bot-id]")).find(
      (row) => ((row.querySelector(".bot-name") || {}).textContent || "") === rowName,
    );
    if (!li) throw new Error(`找不到 Bot 行：${rowName}`);
    li.click();
  }, name);
  await waitCharterState(page, "ready");
}

async function waitCharterState(page, state) {
  await page.waitForFunction(
    (want) => document.getElementById("charter").dataset.state === want,
    { timeout: 15000 },
    state,
  );
  await sleep(150);
}

/** 清空一个输入框（Ctrl+A + Backspace）。**不用三击**：headless 里三击不选中（S7 实测）。 */
async function clearInput(page, selector) {
  await page.click(selector);
  await page.keyboard.down("Control");
  await page.keyboard.press("KeyA");
  await page.keyboard.up("Control");
  await page.keyboard.press("Backspace");
}

async function waitRowCount(page, count) {
  await page.waitForFunction(
    (want) => document.querySelectorAll("#bot-list li[data-bot-id]").length === want,
    { timeout: 15000 },
    count,
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

  const work = await mkdtemp(join(tmpdir(), "postel-s8-acceptance-"));
  const stateHome = join(work, "postel-home");
  const shots = join(work, "shots");
  await mkdir(stateHome, { recursive: true });
  await mkdir(shots, { recursive: true });

  process.stdout.write(`浏览器：${chrome.version}（${chrome.path}）\n`);
  process.stdout.write(`工作目录：${work}（截图与内核日志都在这）\n`);

  const repoMain = await makeRepo(work, "demo-main");
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

    // ---- 0. 初始态：没选仓库，不装作能建 ----
    const initial = await snapshot(page);
    check(
      "0 初始 #bots 是 idle + 「选择一个仓库查看它的 Bot」",
      initial.botsState === "idle" &&
        initial.botsStateText.trim() === "选择一个仓库查看它的 Bot" &&
        initial.botsStateVisible,
      { state: initial.botsState, text: initial.botsStateText },
    );
    check("0 「新建 Bot」不可见（没选仓库）", initial.newButtonVisible === false);
    check("0 表单是收起的", initial.formHiddenAttr === true && initial.formVisible === false);
    check("0 权限下拉在 idle 时不可见", initial.permissionRowVisible === false);

    await bindPath(page, repoMain);
    await page.waitForFunction(
      () => document.querySelectorAll("#repo-list li[data-repo-id]").length === 1,
      { timeout: 15000 },
    );
    await clickRepoRow(page, "demo-main");

    // ---- 1. 选中一个还没有 Bot 的仓库 ----
    const empty = await snapshot(page);
    const repoId = empty.repoRows[0].repoId;
    check(
      "1 空仓库 → ready + 空列表 + 「这个仓库还没有 Bot。」",
      empty.botsState === "ready" && empty.rows.length === 0 && empty.botEmptyVisible,
      { state: empty.botsState, rows: empty.rows.length, empty: empty.botEmptyVisible },
    );
    check("1 「新建 Bot」出现且可点", empty.newButtonVisible === true);
    check(
      "1 工作区卡片指向那个动作（还没有 Bot，先新建一个）",
      empty.workspaceBot.trim() === "Bot：（还没有 Bot，先新建一个）",
      empty.workspaceBot,
    );
    await page.screenshot({ path: join(shots, "1-empty.png") });

    // ---- 2. 打开表单：焦点、默认值、选项 ----
    await page.click("#bot-new");
    await sleep(200);
    const opened = await snapshot(page);
    check("2 点「新建 Bot」→ 表单出现", opened.formVisible === true);
    check("2 名字输入框拿到焦点", opened.nameFocused === true, { focused: opened.nameFocused });
    check(
      "2 默认选中 L1",
      opened.createOptions.length === 4 && opened.createPermission === "L1" &&
        opened.createOptions[1].selected === true,
      opened.createOptions.map((o) => o.value),
    );
    check(
      "2 下拉恰好 L0/L1/L2/L2+，没有 L3/L4",
      JSON.stringify(opened.createOptions.map((o) => o.value)) ===
        JSON.stringify(["L0", "L1", "L2", "L2+"]),
      opened.createOptions.map((o) => o.value),
    );
    check("2 下拉的标签是可读的中文（不是裸码）", opened.createOptions[0].label.includes("只读"));

    // ---- 3. 建 builder / L2 ----
    await page.type("#bot-create-name", "builder");
    await page.select("#bot-create-permission", "L2");
    await page.click("#bot-create-submit");
    await page.waitForFunction(
      () => document.getElementById("bot-create-form").hidden === true,
      { timeout: 15000 },
    );
    await waitRowCount(page, 1);
    const created = await snapshot(page);
    check("3 创建成功后表单收起", created.formVisible === false && created.formHiddenAttr === true);
    check(
      "3 左栏出现一行 builder",
      created.rows.length === 1 && created.rows[0].name === "builder",
      created.rows,
    );
    check("3 徽标正是选的那个级别（L2）", created.rows[0].permission === "L2", created.rows[0]);
    check("3 摘要是「尚未填写职责」", created.rows[0].summary === "尚未填写职责");
    check("3 #bot-empty 隐藏了（空态文案不能留在有内容的时候）", created.botEmptyVisible === false);

    const memory = join(stateHome, "bots", repoId, "builder");
    const memoryFiles = existsSync(memory) ? await readdir(memory) : [];
    check(
      "3 磁盘上真有记忆目录 + charter.md / memory / sessions",
      existsSync(memory) &&
        ["charter.md", "memory", "sessions"].every((name) => memoryFiles.includes(name)),
      { memory, files: memoryFiles },
    );
    check(
      "3 记忆目录 0700、charter.md 0600（真 stat）",
      existsSync(memory) &&
        (await stat(memory)).mode % 0o1000 === 0o700 &&
        (await stat(join(memory, "charter.md"))).mode % 0o1000 === 0o600,
      { dir: (await stat(memory)).mode.toString(8) },
    );
    await page.screenshot({ path: join(shots, "2-created.png") });

    // ---- 4. 非法名字：说出规则、表单还在、没有多建 ----
    await page.click("#bot-new");
    await sleep(150);
    await page.type("#bot-create-name", "Builder");
    await page.click("#bot-create-submit");
    await page.waitForFunction(
      () => document.getElementById("bot-create-error").hidden === false,
      { timeout: 15000 },
    );
    await sleep(150);
    const rejected = await snapshot(page);
    check(
      "4 非法名字 → 错误可见且说出规则（名字不合法…小写字母开头）",
      rejected.createErrorVisible &&
        rejected.createError.includes("名字不合法") &&
        rejected.createError.includes("小写字母开头"),
      rejected.createError,
    );
    check("4 表单还在、填的值还留着（人可以改完再试）", rejected.formVisible === true &&
      rejected.nameValue === "Builder", { value: rejected.nameValue });
    check("4 左栏仍然只有 1 行（非法名字什么都没建）", rejected.rows.length === 1);

    // ---- 5. 同名：说清楚是撞名，不是"名字不合法" ----
    await clearInput(page, "#bot-create-name");
    await page.type("#bot-create-name", "builder");
    await page.click("#bot-create-submit");
    await page.waitForFunction(
      () =>
        (document.getElementById("bot-create-error").textContent || "").includes(
          "已经有叫这个名字的 Bot",
        ),
      { timeout: 15000 },
    );
    const duplicated = await snapshot(page);
    check(
      "5 同名再建 → 「这个仓库已经有叫这个名字的 Bot」",
      duplicated.createError.includes("已经有叫这个名字的 Bot"),
      duplicated.createError,
    );
    check("5 仍然只有 1 行", duplicated.rows.length === 1);

    // ---- 6. Escape 收起表单，焦点回到按钮 ----
    await page.keyboard.press("Escape");
    await sleep(150);
    const escaped = await snapshot(page);
    check("6 Escape 收起表单", escaped.formVisible === false);
    check("6 焦点回到「新建 Bot」", escaped.newFocused === true);
    check("6 收起后错误文案也清掉了", escaped.createError === "" && !escaped.createErrorVisible);

    // ---- 7. 回车 = 提交（真 <form>）----
    await page.click("#bot-new");
    await sleep(150);
    await page.type("#bot-create-name", "qa-1");
    await page.focus("#bot-create-name");
    await page.keyboard.press("Enter");
    await waitRowCount(page, 2);
    const two = await snapshot(page);
    check(
      "7 回车也能创建（qa-1 出现在左栏）",
      two.rows.length === 2 && two.rows.some((row) => row.name === "qa-1"),
      two.rows.map((row) => row.name),
    );
    check(
      "7 第二个 Bot 用默认级别 L1",
      two.rows.find((row) => row.name === "qa-1").permission === "L1",
      two.rows,
    );

    // ---- 8. 刷新后仍在（持久化的真凭据）----
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.getElementById("repos").dataset.state === "ready",
      { timeout: 15000 },
    );
    const afterReloadIdle = await snapshot(page);
    check(
      "8 刷新后回到 idle（页面状态不落盘，仓库要重选）",
      afterReloadIdle.botsState === "idle" && afterReloadIdle.rows.length === 0,
      afterReloadIdle.botsState,
    );
    await clickRepoRow(page, "demo-main");
    await waitRowCount(page, 2);
    const reloaded = await snapshot(page);
    check(
      "8 重新选中仓库后两个 Bot 都还在（builder / qa-1）",
      reloaded.rows.length === 2 &&
        reloaded.rows.some((row) => row.name === "builder") &&
        reloaded.rows.some((row) => row.name === "qa-1"),
      reloaded.rows.map((row) => row.name),
    );
    check(
      "8 权限徽标也还是原样（builder=L2 / qa-1=L1）",
      reloaded.rows.find((row) => row.name === "builder").permission === "L2" &&
        reloaded.rows.find((row) => row.name === "qa-1").permission === "L1",
      reloaded.rows.map((row) => [row.name, row.permission]),
    );

    // ---- 9. charter 面板改权限 → 左栏徽标跟着变 ----
    await clickBotRow(page, "builder");
    const charter = await snapshot(page);
    check("9 点 Bot → charter ready", charter.charterState === "ready", charter.charterState);
    check("9 标题「Charter · builder」", charter.charterTitle.trim() === "Charter · builder");
    check(
      "9 权限下拉可见且显示它当前的级别 L2",
      charter.permissionRowVisible === true && charter.permissionValue === "L2",
      { visible: charter.permissionRowVisible, value: charter.permissionValue },
    );
    check(
      "9 charter 面板的下拉也只有四个选项（与新建表单同源）",
      JSON.stringify(charter.permissionOptions.map((o) => o.value)) ===
        JSON.stringify(["L0", "L1", "L2", "L2+"]),
      charter.permissionOptions.map((o) => o.value),
    );
    check(
      "9 记忆目录仍显示绝对路径",
      charter.memoryVisible && charter.memory === `记忆目录：${memory}`,
      charter.memory,
    );

    await page.select("#charter-permission", "L1");
    await page.waitForFunction(
      () => {
        const row = Array.from(document.querySelectorAll("#bot-list li[data-bot-id]")).find(
          (li) => ((li.querySelector(".bot-name") || {}).textContent || "") === "builder",
        );
        return row && (row.querySelector(".bot-permission") || {}).textContent === "L1";
      },
      { timeout: 15000 },
    );
    const changed = await snapshot(page);
    check(
      "9 改成 L1 → 左栏徽标跟着变（一处事实源：bot.list）",
      changed.rows.find((row) => row.name === "builder").permission === "L1",
      changed.rows.map((row) => [row.name, row.permission]),
    );
    check("9 改权限没有把 charter 面板打回 loading", changed.charterState === "ready");
    check("9 下拉自己也是 L1", changed.permissionValue === "L1");
    await page.screenshot({ path: join(shots, "3-permission.png") });

    // ---- 10. 全程无 console 错误 ----
    check("10 全程无 console 错误", consoleErrors.length === 0, consoleErrors.slice(0, 3));
    check("10 全程无未捕获异常", pageErrors.length === 0, pageErrors.slice(0, 3));
  } finally {
    await browser.close();
    await stopKernel(kernel);
    const logPath = join(work, "kernel.log");
    await writeFile(logPath, kernel.output(), "utf8");
    process.stdout.write(`内核日志：${logPath}\n截图目录：${shots}\n`);
    if (/WebSocket.*[?]ticket=(?!<REDACTED>)/.test(kernel.output())) {
      failures += 1;
      process.stdout.write("CHECK ✗ 内核日志里出现了未脱敏的 ticket\n");
    }
    // bot 只写状态根：用户仓库的工作区必须零改动（S3 验收 5 的同一纪律）
    const status = await gitIn(repoMain, ["status", "--porcelain"]).catch(() => null);
    if (status && status.stdout.trim() !== "") {
      failures += 1;
      process.stdout.write(`CHECK ✗ 内核往用户仓库的工作区里写了东西：\n${status.stdout}\n`);
    }
    process.stdout.write(`状态根下的 bots：${JSON.stringify(await readdir(join(stateHome, "bots")))}\n`);
    await rm(repoMain, { recursive: true, force: true });
  }

  if (failures > 0) {
    process.stdout.write(`\n❌ ${failures} 项失败\n`);
    process.exit(1);
  }
  process.stdout.write(
    "\n✅ 浏览器验收全部通过（S8：空态 / 表单 / 默认 L1 / 四个选项 / 建 / 非法名字 / 撞名 / Escape / 回车 / 刷新 / 改权限）\n",
  );
}

main().catch((error) => {
  process.stdout.write(`\n❌ 剧本崩溃：${error && error.stack ? error.stack : error}\n`);
  process.exit(1);
});
