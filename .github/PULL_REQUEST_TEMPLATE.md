<!--
对照 docs/design/09-slice-dod.md 填写。空着的项写「不适用 + 原因」，不要删行。
数字一律粘自命令，不许凭记忆（09 §3.4）。
-->

## 用户路径

一句话：用户做什么 → 看到什么。

## 验收 → 测试映射

| # | 验收标准 | 测试 |
|---|---|---|
| 1 |  | `tests/test_x.py::test_y` |

## 契约变更

- 新增 / 变更字段：
- 错误码：只用了 `07-api-contract.md §1.3` 既有码（是 / 否，否的话在这里单独标）
- DOM id（改了页面才填）：

## 证据（贴**原始输出尾部**，不许只写「通过」）

```text
# bash scripts/check.sh all 尾部（passed 数 + 覆盖率）
```

```text
# node tools/acceptance/<x>.mjs 的 CHECK ✓/✗ 计数（含既有脚本回归）
```

## 预算（口径：[`04-task-breakdown.md §1.1`](docs/design/04-task-breakdown.md)，只计 `src/`）

- `src/`：N 文件 / +A −B（上限 12 文件 / 400 行）
- 全量 `git diff --stat`：

## 未做 / 未验（**分开写**）

- 未做：
- 未验（含被 SKIP 的验收脚本，写明原因）：

## 未决项变化

- 新增 U：
- 结案 U：

## 自查（对应 09 的条目）

- [ ] 规格是分支第一个 commit，含可执行证据（§1.1）
- [ ] 每条验收都有测试，映射表见上（§1.2）
- [ ] 边界矩阵覆盖了数据模型的状态，不是只测 happy path（§1.3）
- [ ] 本刀依赖的未决项已裁决（§1.4）
- [ ] 新增的硬约束配了反向注入（§2.1）
- [ ] 没有新增错误码（§2.2）
- [ ] `conftest` / CI / 依赖未静默改动（§2.3）
- [ ] `bash scripts/check.sh all` 全绿，`build` 单独跑过（§3.1）
- [ ] 改了前端 → 跑了**全部** `tools/acceptance/*.mjs`（§3.2）
- [ ] 所有数字粘自命令（§3.4）
- [ ] `docs/codemap/` 已同步，且 `docs` / `layers` 门禁绿
