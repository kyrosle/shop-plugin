# 一次性席位（预览）

[English](../en/SEATS.md) · [README](../../README.zh-CN.md)

这是常驻式 `/shop` 工作台的另一种用法：不预先开 pane，也不需要写工单文件。由你的 Pi（Architect）写一份简短的 SPEC 和 PLAN；之后每个下游席位都在新的 Herdr pane 中启动，用上一级的上下文生成的子 session 开始工作，通过工具汇报一次，然后自己关闭 pane。

```text
Architect（你的 Pi、你的模型）
  /shop-go <目标> → SPEC.md + PLAN.md → 上下文交接
      ▼
Lead（新 pane，Lead 模型）       把 PLAN 拆成任务、派发、审查
  shop_spawn_worker → 上下文交接
      ▼
Worker（新 pane，Worker 模型；相互独立的任务并行执行）
  shop_report → pane 关闭
      ▼
Lead 的 shop_report → 作为消息送到 Architect → Architect 对照 SPEC 核对
```

## 前提

安装、bridge 与配置和 Shop 相同（见 README）。Lead 与 Worker 的模型和 thinking 档位来自 `/shop-config`；Architect 沿用你当前 Pi 的模型。**不需要**先打开常驻 Shop（Ctrl+B → U），但 Pi 必须运行在 Herdr 中。

## 命令

| 命令 | 效果 |
| --- | --- |
| `/shop-go <目标>` | Architect 写精简的 `SPEC.md`（决策、约束、验收）和 `PLAN.md`，这一轮结束后自动启动 Lead |
| `/shop-spec <目标>` | 只写 `SPEC.md` 与 `PLAN.md`，便于你先审阅或修改 |
| `/shop-go` 或 `/shop-go <run 目录>` | 为已审阅的 run 启动 Lead（默认最近一次 `/shop-spec` 的 run），需确认 |

席位工作期间可以继续和 Architect 对话；Lead 的汇报会作为消息到达，Architect 再对照 SPEC 核对。

席位工具：Lead 有 `shop_spawn_worker`、`shop_wait_workers`（等待时不调用模型；Worker pane 未汇报就关闭时标记为 `lost`）和 `shop_report`；Worker 只有 `shop_report`。席位只加载这些 Shop 工具。

## 上下文如何交接

- 交接的是上一级完整的消息历史；没有结果的工具调用（例如正在执行的派生调用）及其孤立结果会被去掉。
- 由接收方决定形式。**16k token 以内原样转交**：有提示缓存时很便宜，也不丢信息。超过**接收方模型上下文窗口的 30%** 时**必须用** [pi-context-curator](https://github.com/kyrosle/pi-context-curator) **压缩**到该预算以内。两者之间目前也压缩。
- 任务说明（给 Lead 的是 SPEC 和 PLAN；给 Worker 的是任务加 SPEC）放在席位的附加系统提示里，席位自己的自动压缩不会把它丢掉。
- curator 的分析模型使用 Worker 模型、low thinking，调用单独计量。
- 席位被要求只使用交接给它的内容，不得读取 Pi session 文件或其他席位的记录；缺少信息时报告 `blocked` 并说明缺什么。
- `SHOP_HANDOFF_MODE=raw|curate|brief` 可在实验时强制指定形式；超出预算的上下文仍会被压缩。

## 文件

一次 run 的全部内容都在项目的 `.shop/seats/<run>/` 下：

| 路径 | 内容 |
| --- | --- |
| `SPEC.md`、`PLAN.md` | Architect 的决定 |
| `seats/<id>.json` | pane、模型、交接决定（形式、原因、token 数、分析模型用量） |
| `prompts/<id>.md` | 席位的附加系统提示（角色 + 任务说明） |
| `sessions/*.jsonl` | 每个席位的 session，pane 关闭后保留；可用 `pi --session <文件>` 查看 |
| `reports/<id>.json` | 每个席位唯一的一次汇报 |

## 限制

- 预览功能。仅在 macOS 上通过隔离的真实 Provider 测试验证（见[测试](TESTING.md)）。
- 所有 Worker 共用一个工作区：并行任务不能改同一批文件。没有 worktree 隔离、提交、集成与收工核验；这些仍是常驻 `/shop` 的功能。
- 已关闭的席位不能恢复；其 session 保留供查看。
- Lead 汇报时如果 Architect 正忙，汇报会像普通输入一样排队。
