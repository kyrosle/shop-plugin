# 测试

[English](../en/TESTING.md) · [README](../../README.zh-CN.md)

分三个层次，从免费到付费：

| 层次 | 命令 | 证明什么 |
| --- | --- | --- |
| 离线 | `npm test`、`npm run typecheck` | 配置、交接规则、压缩写入子 session、席位辅助函数、runner 安全性；不启动宿主，不调用模型 |
| 真实宿主，不调用模型 | `npm run test:host -- --run` | 真实 Herdr 中的真实 Pi 能加载 Shop；命令已注册；`/shop-config`、模型选择器、语言切换和 `/reload` 正常 |
| 真实宿主，真实 Provider | `python3 tests/host/run.py --run --scenario live-…` | 用真实模型完整跑一次席位流程，带正确性检查和费用/耗时统计 |

前提：macOS；PATH 中已有 Herdr ≥ 0.9.0（实测 0.9.1）、Pi、Node 和 Python 3；已执行 `npm ci --ignore-scripts`。runner 不会安装或升级宿主程序。不带 `--run` 时只打印计划，不启动任何进程。缺少前提视为错误，不会静默跳过。

## 隔离

每次运行都创建全新的私有 `/tmp/shop-host-*` 目录，内含独立的 Herdr 配置、socket、headless server 和 PTY；独立的 HOME、XDG、Pi agent 目录、session 和 Shop 配置/状态；以及一个临时 Git 项目。子进程环境采用白名单：不复制你的密钥、代理和 agent 上下文。runner 不会接管已有的 server，也不会查询你的其他 tab。

真实运行时模型可以执行命令，因此席位加载的是测试目录下的**源码私有副本**（`node_modules` 为软链接），Shop 路径不会指向你的工作区。这是配置/进程隔离，不是操作系统沙箱；只运行可信代码。

不调用模型的层次使用本地 fixture provider（为模型选择器提供 13 个模型条目），要求 **Provider 调用次数为零**。

## 真实 Provider 场景（显式启用，会产生费用）

```sh
python3 tests/host/run.py --run --scenario live-seats \
  --pi-bin "$(which pi)" \
  --live-model opencode-go/deepseek-v4.1-flash \
  --live-thinking architect=max,lead=high,worker=low \
  --live-budget-usd 0.30
```

| 场景 | 通过条件 |
| --- | --- |
| `live-seats` | `/shop-go <目标>`（或 `--seats-flow two-step`：先 `/shop-spec` 再确认 `/shop-go`）经由 Worker 交付，Architect 的回答写出 fixture 内容，所有席位 pane 关闭且 session 保留 |
| `live-fidelity` | Architect 的真实对话定下一条约束并否决一个方案，固定写入的 SPEC/PLAN 故意不写；约定的 `report.json` 必须写入仓库。使用 `--handoff-mode brief` 时，预期结果是如实报告 `blocked` 且不产出文件。任何席位读取 Pi session 文件都判为失败 |
| `live-parallel` | 两个相互独立的 Worker 结果都正确，且执行时间有重叠 |
| `live-failure` | `--failure-case missing`：输入缺失时以 `blocked` 结束，不伪造文件。`--failure-case lost`：Worker pane 中途被关闭时能被发现，Lead 要么恢复、要么报告失败。调用次数不超过上限 |
| `live-task` | 预置一个有真实 bug 和新功能需求的小模块：项目测试、runner 持有的隐藏测试和原有测试全部通过 |

参数：

- `--handoff-mode auto|raw|curate|brief`：强制指定交接形式。
- `--fidelity-size large`：先读约 90 KB 背景文档，使交接落在中间区间。
- `--handoff-budget-tokens N`：缩小接收方预算，强制压缩。
- `--live-seat-models architect=p/m,lead=p/m,worker=p/m`：按席位指定模型，未指定的席位使用 `--live-model`。
- `--repeat N`（最多 20）：运行 N 次相互独立的宿主，在 `$TMPDIR` 写出 `shop-host-aggregate-*.json`，包含通过率、失败步骤，以及各席位费用与耗时的最小/中位/最大值。
- `npm run` 优先使用仓库自带的 Pi；如果真实模型需要更新的 Pi 模型列表，请传 `--pi-bin`。

凭据与预算：

- 只从 `~/.pi/agent/auth.json` 复制所选 provider 的 **API key**。拒绝 OAuth，避免测试中的刷新轮换掉你自己的登录。
- 无论成败，每次运行结束后都会删除副本，报告中也不包含凭据。
- 每条 assistant 消息的用量都会记录，curator 分析模型的调用作为单独的 `curator` 席位计量。超过 `--live-budget-usd`（默认 0.30，最大 5）或 `--live-max-calls`（默认 80）即中止运行。

## 证据与清理

每次运行都会打印 `Artifacts: <私有目录>`。`report.json` 包含各步骤结果、版本、真实用量、各席位基线指标和场景判定。该次运行的 `project/.shop/seats/` 下保存 SPEC、PLAN、席位记录、提示、session 与汇报。产物包含本机路径和终端文本，分享前请先检查。

失败或中断时仍会清理自己创建的资源：runner 只停止它自己启动的 server，并确认它启动的 Pi 进程已退出。要停止后台运行，向 Python runner 的**确切 PID** 发送 SIGTERM，这样清理和凭据删除才会执行。产物会保留；检查后只删除那次运行的目录。

## 自动化

`npm test` 在普通 CI 中离线运行。手动触发的 **Real host gate** 工作流在专用的自托管 macOS runner（`shop-host-tests`）上运行不调用模型的层次。真实 Provider 场景从不在 CI 中运行。
