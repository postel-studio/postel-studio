# 03 · repository — `src/postel_studio/api/repository.py`

> 层 L3 · 256 行 · 上游：[模块索引](README.md) · 相关：[gitops](gitops.md) · [storage-db](storage-db.md) · [domain](domain.md) · 接口：[07 §2.2/§4.1/§4.5](../../design/07-api-contract.md)

## 职责

`repo.bind` / `repo.list` / `repo.branches` 的**业务编排**：路径 → 校验 → git 探测 → 落库 → 帧载荷
（S4 的 `branches` 是只读的：`repoId` → 已绑定的 path 当允许根 → git → 帧载荷）。

**这一层不碰 fastapi**（S3 涉及文件表的要求）。它只接收 `dict` 参数、返回 `dict`，
把下游异常翻成 07 §1.3 的**稳定错误码**。怎么接上 WebSocket 是 `api/gateway.py` 的事。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `NOT_A_REPO` / `BAD_CWD` / `ADAPTER_UNAVAILABLE` / `REPO_NOT_BOUND` | 37-40 | 07 §1.3 的码（**不许新造**） | `bind` / `get` / `branches` |
| `MSG_*`（9 个） | 43-52 | 错误文案常量，**没有一个字来自用户输入** | `_fail` 调用点 |
| `BranchPayloads` | 56 | `list[dict[str, object]]` 的模块级别名（类里有方法叫 `list`，类体里直接写 `list[…]` 注解会被 mypy 判 `valid-type`） | `branches` 返回注解 |
| `_fail(code, message)` | 59 | 造 `frames.FrameError`（网关原样转发） | 内部 |
| `_require_path(params)` | 64 | `path` 必须是非空字符串，否则 `INVALID_PARAMS` | `bind` |
| `_inside_git_dir(root, target)` | 72 | **T29/T31：`root` → `target` 的相对路径里有 `.git` 段 → 不是仓库入口** | `_bindable_root` |
| `_bindable_root(target)` | 93 | **D2 子目录校正的总判定**（见下） | `bind` |
| `_split(repo)` | 129 | `Repo` → 帧载荷（`camelCase`，07 §4.2） | `list` / `bind` / `get` |
| `RepositoryService` | 144 | 业务对象（构造时注入 `RepoStore`） | `app.create_app` |
| `RepositoryService.list()` | 150 | `repo.list`：返回 `repo[]` | `gateway.Providers.repo_list` |
| `RepositoryService.bind(params)` | 154 | `repo.bind`：探测 + 落库 + 返回落库后的记录 | `gateway.Providers.repo_bind` |
| `RepositoryService._require_repo(params)` | 195 | `repoId` 校验 + 查库 → `Repo`（`INVALID_PARAMS` / `REPO_NOT_BOUND` 的落点） | `get` / `branches` |
| `RepositoryService.get(params)` | 210 | 按 `repoId` 取一条（`get` 本身暂无帧方法调用） | 暂无生产调用方 |
| `RepositoryService.branches(params)` | 214 | `repo.branches`：`repoId` → 允许根 → `gitops.list_branches` → `branch[]`（S4 D1/D5） | `gateway.Providers.repo_branches` |

## 关键决策与理由

### `_bindable_root`：D2 的「子目录校正」，以及它踩过的坑

判定顺序（**别改顺序**，也别改回"git 说有输出就算仓库"）：

```
0. target 自身或任何祖先的名字是 ".git"        → None（T29：.git 内部结构不是仓库入口）
1. target 是目录 且 target/.git 存在            → 绑 target
2. target 是目录 且 git 说 is-bare-repository   → 绑 target（裸仓库，D2 接受）
3. target 不是目录（文件 / 不存在）             → None
4. 从 target 逐级向上，第一个带 .git 的祖先     → 绑它（子目录校正 proj/src → proj）
5. 都没有                                        → None
```

**第 0 条必须排在前面**：`.git/objects` 的祖先链里就有 `.git`，第 4 条会把它校成外层仓库
（S3 复审退回的缺陷，见 [06 T29](../06-pitfalls.md#t29--粘-git-内部路径会被静默改绑到外层仓库审核发现已修)）。
判据是**名字**而不是"`.git` 存在"——worktree 根里 `.git` 是**文件**，它是合法根，不能被误杀。

- 第 1、4 步只看 `path/.git` 是否存在（**文件系统检查，不依赖 git**）。worktree 里 `.git`
  是文件，`exists()` 同样为真。
- 第 2 步才问 git（裸仓库没有 `.git`，只能问 `rev-parse --is-bare-repository`）。
- **为什么不直接用 `git rev-parse --show-toplevel` 判"是不是仓库"**：它会向父目录 discover。
  实测：`tmp_path` 下的普通目录、`/tmp` 本身都会被答"是"，于是"绑定成功"了（T25）。
- **接受的近似**：第 4 步无法区分 `proj/src`（真子目录）与 `proj/某个普通目录`——两者在路径
  上等价。按 D2 的字面语义一律校到 `proj`。见 [06 §已知未决 U11](../06-pitfalls.md#已知未决问题)。

### S4：`branches` 的三态与错误码映射（D4/D5）

```python
# api/repository.py:214 —— 允许根只有一处来源：库里那条记录的 path
repo = self._require_repo(params)  # INVALID_PARAMS / REPO_NOT_BOUND
found = gitops.list_branches(Path(repo.path))
if found is None:  # git 非零退出 = 不是仓库 / 路径没了
    raise _fail(REPO_NOT_BOUND, MSG_REPO_PATH_MISSING)  # 「仓库路径已不存在」
return [
    {"name": b.name, "sha": b.sha[:8], "isCurrent": b.is_current, "upstream": b.upstream}
    for b in found
]
```

| gitops 返回 | 帧层结果 | 为什么 |
|---|---|---|
| `[]`（空仓库 0 提交） | **成功** `data: []` | D5：空仓库是正常状态，不是错误 |
| `None`（非零退出） | `REPO_NOT_BOUND` + 「仓库路径已不存在」 | D5：用户删了仓库是**正常处境**；`INTERNAL` 会报成"未预期错误需报障" |
| `GitUnavailableError` | `ADAPTER_UNAVAILABLE` | 同 S3 D5 |
| `GitTimeoutError` | `INTERNAL` | 同 S3 D6（S19 的 doctor 再细化） |

- **截 8 位发生在这一层**（D4 的边界纪律）：`gitops.BranchInfo.sha` 是完整 40 位，帧载荷是
  8 位小写 hex。两层各自可测，别把截断挪到 gitops 或前端。
- `upstream` 是 `None` 时**原样**放进载荷（`None`），不写成 `""`——D1 把它定为 `null`。
- `branches` **不落库、不缓存**（06-data-model §8）：每次按需问 git，所以"用户切了分支"立刻可见。
- `_require_repo` 是唯一允许的既有代码重构（S4 实施要求）：`get()` 与 `branches()` 共用，
  "允许根 = 已绑定 path"只有一个实现。

### 错误码只用 07 §1.3 的六个，一个都不新造（D6）

| 场景 | 码 | 在哪判 |
|---|---|---|
| `path` 缺失 / 不是字符串 / 空串 | `INVALID_PARAMS` | `_require_path` |
| `path` 不是绝对路径（含 `~` 开头） | `BAD_CWD` | `bind`（`Path(raw).is_absolute()`） |
| 不存在 / 是文件 / 不是仓库 | `NOT_A_REPO` | `_bindable_root` 返回 `None` |
| `git` 不在 `PATH` / 起不来 | `ADAPTER_UNAVAILABLE` | `except GitUnavailableError` |
| `git` 超时 | `INTERNAL` | `except GitTimeoutError`（**没有** `GIT_TIMEOUT`，D6 明确不造） |
| `repoId` 不存在 | `REPO_NOT_BOUND` | `get` |

### 05 §4.2：错误 message 不回显用户输入

**所有 `MSG_*` 都是常量**，没有一处 `f"{path}"`。这是最容易顺手写错的地方：
`f"路径不是 git 仓库：{target}"` 看起来更有用，但它把解析后的绝对路径送进了帧与日志。

```python
# ✅ 仓库里的写法（api/repository.py:41）
MSG_NOT_A_REPO: Final = "路径不是 git 仓库"
# ❌ 看起来更有用，但把解析后的绝对路径送进了帧与日志（05 §4.2）
#    raise _fail(NOT_A_REPO, f"{target} 不是仓库")
```

`test_not_a_repo_message_does_not_echo_any_user_path` 用一段独一无二的目录名片段断言
`message` / `str(exc)` / `repr(exc)` 里都不出现它。

### `isDirty` 不填假值

07 §4.1 的示例响应里有 `isDirty: true`，但 S3 不读工作区状态（那是 S4 读 diff 的前提）。
`_split` **不输出**这个字段，而不是输出 `false`——"不知道"与"干净"是两件事，
填假的 `false` 就是撒谎。客户端按缺省处理即可（07 §5：允许新增字段，客户端忽略未知字段）。

### `bind` 里 `gitops.origin_url` 只问一次

两次调用的答案理论上可能不同（用户恰好在改配置），只问一次让"算 id 用的 origin"与
"落库的 origin"一定是同一个值。

## 修改须知

| 如果你要… | 还波及 |
|---|---|
| 改 `_bindable_root` | T25 的回归锁 `test_root_level_plain_directory_that_contains_a_repo_is_not_a_repo`、D2 的验收（子目录校正）、`test_plain_directory_inside_another_working_tree_is_treated_as_that_repos_subdirectory` |
| 加错误码 | **禁止**（D6）；确实需要时先改 07 §1.3 表，再回来 |
| 让 `message` 带上路径 | **禁止**（05 §4.2）|
| 改 `repo.branches` | D5 的码表（07 §1.3，**不许新造**）、`gitops.list_branches` 的三态、D7 第 8 行；**不要**接受新的路径/ref 参数（允许根只能是已绑定的 path） |
| 在本层 `import fastapi` | **禁止**（S3 涉及文件表的要求）；帧形状用 `frames` 里的纯函数 |
| 在本层直接 `open()` / `subprocess` | **禁止**：那是 `storage` / `gitops` 的活（02-layers） |

## 测试

`tests/test_bind.py`（真实 git 仓库 + 真 SQLite，**不 mock**）：

| 测试 | 守什么 |
|---|---|
| `test_bind_regular_repo_returns_hex12_id_name_and_main` | 验收 1 的载荷 |
| `test_bind_subdirectory_stores_repo_root_not_the_subdirectory` | D2（**必须**断言） |
| `test_bind_path_with_trailing_slash_and_dotdot_resolves_to_root` / `..._through_symlink_...` | D4 `resolve()` |
| `test_bind_non_repo_directory_is_not_a_repo` / `test_not_a_repo_message_does_not_echo_any_user_path` | D6 + 05 §4.2 |
| `test_bind_git_directory_itself_is_not_a_repo` / `..._inside_the_git_directory_...` / `..._with_a_dot_git_segment` | T29：`.git` 内部结构不是入口 |
| `test_worktree_root_with_git_file_still_binds` / `..._subdirectory_still_corrects_...` | T29 防误伤（`.git` 是文件） |
| `test_bind_relative_path_is_bad_cwd[~]` 等 5 个 | `BAD_CWD`（含 `~` 不展开） |
| `test_bind_nonexistent_path_is_not_a_repo_not_a_crash` / `test_bind_file_path_is_not_a_repo` | 不崩 |
| `test_missing_git_is_adapter_unavailable` | `PATH=""` |
| `test_bind_rejects_missing_or_non_string_path`（4 组） | `INVALID_PARAMS` |
| `test_get_reports_repo_not_bound_for_unknown_id` | `REPO_NOT_BOUND` |
| `test_failed_bind_leaves_no_half_state` | 验收 2「不留半状态」 |
| `test_list_returns_array_payload_with_camel_case_keys` | 07 §4.2 边界转换 |
| `test_websocket_bind_then_list_returns_the_repo` | 端到端 |

### S4 的分支列表（`tests/test_branches.py`）

| 测试 | 守什么 |
|---|---|
| `test_branches_returns_names_in_refname_order_with_short_shas` | D7 第 1 行 / D1：四字段、`sha` 8 位小写 hex、顺序为 refname 字典序 |
| `test_branch_error_codes_for_bad_params`（4 组参数化） | D7 第 8 行 / D5：缺/非字符串/空串 → `INVALID_PARAMS`；未知 `repoId` → `REPO_NOT_BOUND` |
| `test_deleted_repo_path_maps_to_repo_not_bound` / `test_repo_whose_dot_git_was_removed_maps_to_repo_not_bound` | D5：路径没了 / `.git` 被删 → `REPO_NOT_BOUND`（文案「仓库路径已不存在」） |
| `test_empty_repo_returns_empty_list_as_success` | D5：空仓库 `data == []`，不是错误 |
| `test_missing_git_executable_maps_to_adapter_unavailable` | D5：`PATH=""` → `ADAPTER_UNAVAILABLE`（真让 git 找不到，不 mock 解析） |
| `test_git_timeout_maps_to_internal` | D5：超时 → `INTERNAL`（只在边界注入异常，钉住码映射） |
| `test_websocket_branches_roundtrip_matches_service_layer` | D7 第 9 行：真 WS 帧 `result.data` 形状与 `seq`，与直调服务层逐字段一致 |
