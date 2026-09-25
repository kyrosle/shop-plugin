# Pi + Herdr Shop

[English](README.md) · 简体中文

**用最强的模型想清楚，让便宜的席位去做。** Shop 把你在 Herdr 里正在对话的 Pi 变成 Architect：你和它对齐思路，它写一份简短的 SPEC 和 PLAN，然后由 Lead 和 Worker 去落实并汇报。每个席位都是 Herdr 中一个独立 pane 里的独立 Pi，使用你为该角色选的模型。

> **Alpha。** 已在 macOS 上通过隔离的真实 Herdr / 真实 Pi 测试验证，包括调用真实 Provider 的付费测试（见[测试](docs/zh-CN/TESTING.md)）。它不是无人值守的调度器。

```text
┌──────────────────────────┬────────────────────────────┐
│ Architect                │ Lead               （新 pane）│
│ 你的 Pi、你的模型         │ 拆分 PLAN、派发、审查、汇报  │
│ /shop-go <目标>           ├────────────────────────────┤
│  → SPEC.md + PLAN.md     │ Worker           （新 pane）│
│  ← 最终回答               │ 每个只做一个任务，做完即关   │
└──────────────────────────┴────────────────────────────┘
```

## 角色

| 角色 | 职责 | 模型 |
| --- | --- | --- |
| **Architect** | 和你对话、写 SPEC/PLAN、核对结果、回答你 | 你当前 Pi 的模型和 thinking 档位（Shop 不会改动） |
| **Lead** | 把 PLAN 拆成任务、派发 Worker、审查汇报、向上汇报 | `/shop-config` → Lead |
| **快速 Worker** | 执行一个任务（默认） | `/shop-config` → 快速 Worker |
| **稳健 Worker** | 在 Lead 指定时执行一个复杂或关键任务 | `/shop-config` → 稳健 Worker |

席位是**一次性**的：不预先开 pane，需要时才启动，只汇报一次，然后自己关闭 pane；session 会保留。

## 安装

前提：Herdr **≥ 0.9.0**（实测 0.9.1）、Pi（实测 0.86.1）、Node **≥ 22.19**、Python **≥ 3.9**、Git。先在 Pi 中配置好 provider 与凭据；Shop 不附带任何凭据。目前只在 macOS 上测试过。

```sh
pi install git:github.com/kyrosle/shop-plugin@<commit>
```

然后在 Pi 中执行 `/reload`。Pi 会运行 `npm install`，通过 HTTPS 拉取锁定版本的 [pi-context-curator](https://github.com/kyrosle/pi-context-curator)。不需要安装 Herdr 插件，也不需要配置 bridge。从早期工作台 alpha 升级：见[安装](docs/zh-CN/INSTALLATION.md)。

## 配置

在 Pi 中执行 `/shop-config`，按会话、受信任项目或全局，选择 Lead、快速 Worker、稳健 Worker 的模型和 thinking 档位。Architect 使用 Pi 自己的 `/model` 和 `/thinking`。详见[配置](docs/zh-CN/MODELS.md)。界面语言：`/shop-language`（[语言](docs/zh-CN/LANGUAGE.md)）。

## 使用

在运行于 Herdr 中的 Pi 里：

```text
/shop-go 修复 textstats.py 中不区分大小写的词数统计，并新增 unique_words()；所有测试保持通过。
```

Architect 先写 `SPEC.md`（决策、约束、验收标准）和 `PLAN.md`，然后自动启动 Lead。期间可以继续和 Architect 对话。Lead 完成后，它的汇报会作为消息到达；Architect 先直接回答你的请求，再说明对照 SPEC 的核对结果。

想在启动前先审阅，用 `/shop-spec <目标>`，按需修改文件，再执行 `/shop-go`。

## 上下文交接

每个席位都从上一级历史生成的子 session 开始。16k token 以内原样转交，并标明这是上一级的历史；有提示缓存时成本很低，也不丢信息。超过接收方模型窗口 30% 时，压缩到该预算以内。SPEC、PLAN 和任务说明固定在席位的系统提示里，席位自己的自动压缩不会丢掉它们。席位不得读取其他 session；缺少信息时报告 `blocked`。详见[一次性席位](docs/zh-CN/SEATS.md)。

## 效果

在隔离的真实 Provider 测试中，所有席位都用 `deepseek-v4.1-flash`（中位数）：

| 流程 | 通过 | 费用 | 得到回答的时间 |
| --- | --- | --- | --- |
| 早期常驻工作台，工单文件（基线，5 次） | 5/5 | $0.021 | 186 秒 |
| `/shop-go`，同一任务（3 次） | 3/3 | $0.009 | 99 秒 |

其他真实测试：只在讨论中出现的约束能传到最终产出（原样转交 3/3，压缩 3/3）；并行 Worker 时间重叠；输入缺失时如实报告 `blocked`；中途被关掉的 Worker 会被替换；一个小型"修 bug + 加功能"的真实任务通过隐藏测试。这些都是小任务，模型分层在这些任务上没有体现出质量优势，只是费用更高。

## 文件与数据

| 内容 | 位置 |
| --- | --- |
| run：SPEC、PLAN、各席位记录、提示、session、汇报 | `<项目>/.shop/seats/<run>/`（已加入 `.git/info/exclude`） |
| 全局配置、语言 | `~/.config/shop-workstation/`（`SHOP_CONFIG_DIR`） |
| 项目 / 会话配置 | `<项目>/.pi/shop.json` / 当前 Pi 分支上的一条记录 |

## 限制

- 所有 Worker 共用一个工作区：并行任务不能改同一批文件。没有 worktree 隔离、自动提交或集成。
- 已关闭的席位不能恢复；其 session 保留供查看（`pi --session <文件>`）。
- Lead 汇报时如果 Architect 正忙，汇报会像普通输入一样排队。

## 开发

```sh
npm ci --ignore-scripts
npm test                     # Python + Bun，离线
npm run typecheck
npm run test:host -- --run   # 真实 Herdr + 真实 Pi，不调用模型
```

付费的真实 Provider 场景需要显式指定模型与预算，见[测试](docs/zh-CN/TESTING.md)。

`private: true` 防止误发布到 npm。项目许可证仍待所有者决定，见 [LICENSE](LICENSE)。
