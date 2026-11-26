"""静态页面。只是一个字符串常量，不引入前端构建链（S1 禁止事项）。"""

from __future__ import annotations

import html
from typing import Final

INDEX_HTML: Final = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Postel Studio · 内核存活</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #0f1115; color: #e6e9ef;
    font: 16px/1.7 -apple-system, "Noto Sans CJK SC", "PingFang SC", sans-serif; }}
  main {{ padding: 40px; border: 1px solid #262b36; border-radius: 14px;
    background: #151922; box-shadow: 0 10px 40px rgb(0 0 0 / 45%); }}
  .dot {{ display: inline-block; width: 9px; height: 9px; margin-right: 8px;
    border-radius: 50%; background: #3fb950; box-shadow: 0 0 10px #3fb950; }}
  h1 {{ margin: 0 0 20px; font-size: 26px; font-weight: 600; letter-spacing: .5px; }}
  dl {{ display: grid; grid-template-columns: auto auto; gap: 6px 18px; margin: 0; }}
  dt {{ color: #8b95a7; }}
  dd {{ margin: 0; font-family: ui-monospace, "SF Mono", Menlo, monospace; }}
</style>
</head>
<body>
<main>
  <h1><span class="dot"></span>内核存活</h1>
  <dl>
    <dt>version</dt><dd>{version}</dd>
    <dt>apiVersion</dt><dd>{api_version}</dd>
    <dt>stateRoot</dt><dd>{state_root}</dd>
  </dl>
</main>
</body>
</html>
"""


def render_index(version: str, api_version: str, state_root: str) -> str:
    """填三个占位符；全部转义，避免路径里的 `&` / `<>` 破坏页面。"""
    return INDEX_HTML.format(
        version=html.escape(version),
        api_version=html.escape(api_version),
        state_root=html.escape(state_root),
    )
