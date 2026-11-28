"""S16 · 把 Bot 的权限级别**翻译**成 opencode v2 的工具规则（纯函数，无 IO）。

阶梯的事实源仍在 `domain/models.py` 的 `permission_covers` / `Permission`；这里只做
「级别 → [{action, resource, effect}]」这一步，**不另造大小比较**，也不读 Agent 类型
（S16 禁止事项）。规则按数组顺序叠加，opencode 采用**后匹配优先**。

无法解析的级别返回 `None`——调用方必须 fail-closed（D6），不静默放行。
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Final

from postel_studio.domain.models import Permission, permission_parse

type Rule = tuple[str, str, str]  # (action, resource, effect)

_DENY_EDIT_ALL: Final[Rule] = ("edit", "*", "deny")
_DENY_SHELL_ALL: Final[Rule] = ("shell", "*", "deny")
_ALLOW_EDIT_ALL: Final[Rule] = ("edit", "*", "allow")

# S16 must-fix 1 / review round 2：opencode 的 shell resource 是 scanner 产出的命令
# 字符串，字面 `git push *` 匹配不到 `git -C <repo> push`，`*/git push *` 又匹配不到
# `/usr/bin/git -C <repo> push`（git 与 push 之间隔着 -C 参数）——两轮真执行探针都测出
# 过哨兵出现、bare remote 收到 ref 的 fail-open。这里每条规则都**前导通配**：git 前面
# 允许任意路径/包装器前缀（`/usr/bin/`、`env`、`sudo`），后面仍把 push 钉死。scanner
# 对无法安全解析的命令（如 `eval`）在存在任何 shell deny 规则时直接拒绝，所以未识别
# 的 push 语法同样 fail-closed（opencode 文档「Scanner」一节）。
_DENY_PUSH: Final[Rule] = ("shell", "*git push *", "deny")
_DENY_PUSH_BARE: Final[Rule] = ("shell", "*git push", "deny")
_DENY_PUSH_C: Final[Rule] = ("shell", "*git -C * push*", "deny")
_DENY_PUSH_OPTS: Final[Rule] = ("shell", "*git * push *", "deny")
_DENY_PUSH_ABS: Final[Rule] = ("shell", "*/git * push *", "deny")
# L2 例外同样前导通配：让 `/usr/bin/git ... push origin ...` 仍算「推本仓库 origin」，
# 但 `/usr/bin/git ... push <foreign>` 仍被上面的 deny 拦住（后匹配优先）。
_ALLOW_ORIGIN_PUSH: Final[Rule] = ("shell", "*git push origin *", "allow")
_ALLOW_ORIGIN_PUSH_C: Final[Rule] = ("shell", "*git -C * push origin *", "allow")

_DENY_ALL_PUSH: Final[tuple[Rule, ...]] = (
    _DENY_PUSH,
    _DENY_PUSH_BARE,
    _DENY_PUSH_C,
    _DENY_PUSH_OPTS,
    _DENY_PUSH_ABS,
)

_RULES: Final[dict[Permission, tuple[Rule, ...]]] = {
    Permission.L0: (_DENY_EDIT_ALL, _DENY_SHELL_ALL),
    Permission.L1: (_ALLOW_EDIT_ALL, *_DENY_ALL_PUSH),
    Permission.L2: (
        _ALLOW_EDIT_ALL,
        *_DENY_ALL_PUSH,
        _ALLOW_ORIGIN_PUSH,
        _ALLOW_ORIGIN_PUSH_C,
    ),
    Permission.L2_PLUS: (_ALLOW_EDIT_ALL,),
}


def rules_for(permission: object) -> tuple[Rule, ...] | None:
    """级别 → 有序规则；无法识别返回 `None`（调用方 fail-closed）。

    纯函数：不查库、不看 Agent 类型、不读环境变量。`L3` / `L4` / `"l1"` / `None` 都
    走同一条拒绝路径——01 §4 的那两级在另一根轴上，不参与这里的大小比较。
    """
    parsed = permission_parse(permission)
    return None if parsed is None else _RULES[parsed]


def _effect_of(rules: tuple[Rule, ...], action: str, resource: str) -> str:
    """opencode 的「后匹配优先」口径；无规则命中时它的默认策略是 `allow`。"""
    effect = "allow"
    for rule_action, rule_resource, rule_effect in rules:
        if fnmatchcase(action, rule_action) and fnmatchcase(resource, rule_resource):
            effect = rule_effect
    return effect
