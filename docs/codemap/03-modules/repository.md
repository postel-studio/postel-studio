# 03 · repository — `src/postel_studio/api/repository.py`

> 层 L3 · 164 行 · 上游：[模块索引](README.md) · 相关：[gitops](gitops.md) · [storage-db](storage-db.md) · [domain](domain.md) · 接口：[07 §2.2/§4.1](../../design/07-api-contract.md)

## 职责

`repo.bind` / `repo.list` 的**业务编排**：路径 → 校验 → git 探测 → 落库 → 帧载荷。

**这一层不碰 fastapi**（S3 涉及文件表的要求）。它只接收 `dict` 参数、返回 `dict`，
把下游异常翻成 07 §1.3 的**稳定错误码**。怎么接上 WebSocket 是 `api/gateway.py` 的事。

## 符号表

| 符号 | 行 | 做什么 | 谁在用 |
|---|---|---|---|
| `NOT_A_REPO` / `BAD_CWD` / `ADAPTER_UNAVAILABLE` / `REPO_NOT_BOUND` | 34-37 | 07 §1.3 的码（**不许新造**） | `bind` / `get` |
| `MSG_*`（6 个） | 40-45 | 错误文案常量，**没有一个字来自用户输入** | `_fail` 调用点 |
| `_fail(code, message)` | 48 | 造 `frames.FrameError`（网关原样转发） | 内部 |
| `_require_path(params)` | 53 | `path` 必须是非空字符串，否则 `INVALID_PARAMS` | `bind` |
| `_bindable_root(target)` | 61 | **D2 子目录校正的总判定**（见下） | `bind` |
| `_split(repo)` | 90 | `Repo` → 帧载荷（`camelCase`，07 §4.2） | `list` / `bind` / `get` |
| `RepositoryService` | 105 | 业务对象（构造时注入 `RepoStore`） | `app.create_app` |
| `RepositoryService.list()` | 111 | `repo.list`：返回 `repo[]` | `gateway.Providers.repo_list` |
| `RepositoryService.bind(params)` | 115 | `repo.bind`：探测 + 落库 + 返回落库后的记录 | `gateway.Providers.repo_bind` |
| `RepositoryService.get(params)` | 152 | 按 `repoId` 取（`REPO_NOT_BOUND` 语义的落点；S4 的入口） | 暂无帧方法调用 |

## 关键决策与理由

### `_bindable_root`：D2 的「子目录校正」，以及它踩过的坑

判定顺序（**别改顺序**，也别改回"git 说有输出就算仓库"）：

```
1. target 是目录 且 target/.git 存在            → 绑 target
2. target 是目录 且 git 说 is-bare-repository   → 绑 target（裸仓库，D2 接受）
3. target 不是目录（文件 / 不存在）             → None
4. 从 target 逐级向上，第一个带 .git 的祖先     → 绑它（子目录校正 proj/src → proj）
5. 都没有                                        → None
```

- 第 1、4 步只看 `path/.git` 是否存在（**文件系统检查，不依赖 git**）。worktree 里 `.git`
  是文件，`exists()` 同样为真。
- 第 2 步才问 git（裸仓库没有 `.git`，只能问 `rev-parse --is-bare-repository`）。
- **为什么不直接用 `git rev-parse --show-toplevel` 判"是不是仓库"**：它会向父目录 discover。
  实测：`tmp_path` 下的普通目录、`/tmp` 本身都会被答"是"，于是"绑定成功"了（T25）。
- **接受的近似**：第 4 步无法区分 `proj/src`（真子目录）与 `proj/某个普通目录`——两者在路径
  上等价。按 D2 的字面语义一律校到 `proj`。见 [06 §已知未决 U11](../06-pitfalls.md#已知未决问题)。

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
| 加 `repo.branches`（S4） | 复用 `get()` 拿到已绑定的 `path`，再 `gitops.run_git(root=path)`——**不要**接受新的路径参数 |
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
| `test_bind_relative_path_is_bad_cwd[~]` 等 5 个 | `BAD_CWD`（含 `~` 不展开） |
| `test_bind_nonexistent_path_is_not_a_repo_not_a_crash` / `test_bind_file_path_is_not_a_repo` | 不崩 |
| `test_missing_git_is_adapter_unavailable` | `PATH=""` |
| `test_bind_rejects_missing_or_non_string_path`（4 组） | `INVALID_PARAMS` |
| `test_get_reports_repo_not_bound_for_unknown_id` | `REPO_NOT_BOUND` |
| `test_failed_bind_leaves_no_half_state` | 验收 2「不留半状态」 |
| `test_list_returns_array_payload_with_camel_case_keys` | 07 §4.2 边界转换 |
| `test_websocket_bind_then_list_returns_the_repo` | 端到端 |