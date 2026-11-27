"""ACP 客户端层（L0）：内核唯一「起 Agent 子进程、和它说话」的地方。

与 `gitops/` 同类——碰子进程、碰管道、可被单元测试用假服务端驱动，但**不许**碰
`api` / `web` / `storage` / `domain`（`scripts/gate-layers.py` 有登记）。
"""
