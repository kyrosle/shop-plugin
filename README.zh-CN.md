# Pi + Herdr Shop

[English](README.md) · 简体中文

**把当前 Pi 扩展成可见、可配置、有交付记录的协作工位。**
你继续在原来的 Pi 中与 Architect 对话；主 Lead 负责拆解、派单和验收，Worker 执行具体任务。每个成员是 Herdr 中独立可见的 Pi，不克隆你的聊天历史。

> **当前为本地 alpha。** 已有离线回归、打包和安装检查，完整的真实模型协作闭环仍需受控验收。不是无人值守调度服务，也不保证一定更快、更省钱或自动完成任务。

[安装](#安装) · [配置](#配置) · [用法](#用法) · [效果与边界](#效果与边界) · [失败与重置](#失败与重置) · [升级](#升级与保留数据)

## 角色与模型：谁由 Shop 配置？

| 角色 | 职责 | 模型来源 | 启动方式 |
| --- | --- | --- | --- |
| **Architect** | 与你对话、明确目标、向主 Lead 委托、汇总交付 | **当前 Pi 原有模型与思考档位** | 保留原 Pi，不重新启动 |
| **主 Lead** | 唯一的拆单、派单、协调和验收负责人 | `/shop-config` 的主 Lead 配置 | 开工时启动 |
| **辅助 Lead** | 按需承担执行或复核，不是第二个调度者 | `/shop-config` 的辅助 Lead 配置 | 按需扩员 |
| **快速 Worker（低成本）** | 日常小任务、批量修改、快速执行 | 你为该用途选择的模型与思考档位 | 开工时启动 |
| **稳健 Worker（重可靠性）** | 复杂实现、疑难修复、关键改动 | 你为该用途选择的模型与思考档位 | 按需扩员 |

**Shop 不自动修改 Architect 的模型、思考档位或会话。** 在 Architect 中用 Pi 原生 `/model`、`/thinking` 调整；`/shop-config` 只管执行席位。

两个 Worker 使用同一类角色实现，只是允许使用不同模型配置。名称表达用途，**不是模型价格、质量保证，也不意味着自动按任务路由模型**。内部 ID 仍为 `worker` / `worker-2`，已有配置无需因改名迁移。

初始工位是 **Architect + 主 Lead + 快速 Worker**；最多 **2 个 Lead + 2 个 Worker**，另加原 Architect。扩员不是开工即全部启动，也不是后台无限自动扩容。

## 效果与边界

初始布局示意（不是实际运行截图）：

```text
┌──────────────────────┬──────────────────────┐
│                      │ 主 Lead              │
│ Architect            │ 拆单、协调、验收     │
│ 你原来的 Pi          ├──────────────────────┤
│ 原模型、原会话       │ 快速 Worker          │
│                      │ 执行具体任务         │
└──────────────────────┴──────────────────────┘
             按需增加辅助 Lead / 稳健 Worker
```

| 你获得的能力 | 不应误解为 |
| --- | --- |
| 可见的独立成员，按席位配置模型 | 复制 Architect 的聊天历史，或强制所有成员用同一模型 |
| 用途明确的模型配置，便于自己权衡成本与能力 | 自动测评模型、自动保证便宜或可靠 |
| 工单、checkpoint、结果和验收记录 | 看见 `idle` 或消息送达就代表任务完成 |
| 状态看板、显式接手、开发准备与交付操作 | 不经确认就创建 worktree、合并或 push |
| 普通对话保持原有 Pi 用法 | 开窗后所有消息都自动转给 Lead |

业务流程是：**明确目标 → 建立并绑定任务 → 主 Lead 派单 → 执行者交付 → 主 Lead 验收 → Architect 汇总**。具体组织由角色和工具配合完成，不是固定的自动流水线。

一次任务的预期交付包括：调查结论或修改摘要、适用的检查命令与结果证据、验收意见、未完成项和风险。主 Lead 保存任务的 `SUMMARY.md` / `REVIEW.md`，Architect 核对后向你汇总；不会把“已派工”或“窗口空闲”当成交付。

## 安装

### 1. 前提

- Herdr **0.9.0**，当前适配器固定协议 **22 / schema 1**；CLI 与运行中的 server 必须兼容。
- Pi **0.85.1 兼容 API**、Node **>=22.19.0**、Python **>=3.9**、Git。Bun 仅开发测试需要。
- 先在 Pi 配好 Provider、凭据和可用模型；Shop 不提供凭据或预设个人模型。
- 保留 `@ogulcancelik/pi-herdr`，用于通用 agent 查看、等待和控制能力。
- 当前仅在 macOS 测试；Linux 为 POSIX 实现路径，尚未现场验收；Windows 不支持。

插件以你的操作系统用户权限运行，不是沙箱。安装前检查源码，备份已有 Pi 设置、Herdr 插件/快捷键、Shop bridge、配置与运行登记。不要在执行成员工作或初始化操作仍在进行时切换代码。

### 2. 两端安装同一个 Git 提交

**两个入口都需要安装。** Pi 提供命令和配置界面；Herdr 提供快捷键、pane 生命周期操作和事件。一个不会自动安装另一个。

以下固定到包含 `/shop-reset` 的 alpha 代码基线，不会自动跟随 `main`。升级时换成你核对过的完整 SHA，两个命令始终使用同一值：

```sh
SHOP_REF='e7ec14cdf190ec2cedec505c5e0e55e38ad2fd69'
pi install "git:github.com/kyrosle/shop-plugin@$SHOP_REF"
herdr plugin install kyrosle/shop-plugin --ref "$SHOP_REF" --yes
```

安装只更新代码与包引用，不启动 Lead/Worker，不派工。不要同时加载旧的手装 `herdr-shop-mode` 和新扩展。

### 3. 首次安装：配置共享 bridge

以 **Herdr 托管 checkout** 为权威 Python 核心；Pi 扩展通过 bridge 调用它。下面从默认 Herdr 注册表读取真实安装路径，不猜目录后缀：

```sh
SHOP_CORE="$(python3 - <<'PY'
import json
from pathlib import Path
plugins = json.loads((Path.home() / '.config/herdr/plugins.json').read_text())
matches = [p for p in plugins if p['plugin_id'] == 'shop.workstation']
assert len(matches) == 1, 'Expected exactly one Shop plugin registration'
print(matches[0]['plugin_root'])
PY
)"
SHOP_CONFIG="$(herdr plugin config-dir shop.workstation)"
SHOP_STATE="$HOME/.local/state/shop-workstation"

# 先预览；检查路径后，再单独执行 --apply。
SHOP_CONFIG_DIR="$SHOP_CONFIG" SHOP_STATE_DIR="$SHOP_STATE" \
  python3 "$SHOP_CORE/core/plugin.py" configure

# 仅首次配置，且确认预览无误后执行。
SHOP_CONFIG_DIR="$SHOP_CONFIG" SHOP_STATE_DIR="$SHOP_STATE" \
  python3 "$SHOP_CORE/core/plugin.py" configure --apply
```

默认 bridge：`~/.config/shop-workstation/bridge.json`。**已有 bridge 的普通升级不要重跑 `configure --apply`，更不要删 bridge 绕过保护。** 自定义 Herdr profile / `SHOP_LOCATOR` 的路径设置见[完整安装指南](docs/zh-CN/INSTALLATION.md)。

### 4. 配置 Herdr 快捷键

备份 Herdr `config.toml`，添加或替换对应条目；已有条目不要重复追加：

```toml
[[keys.command]]
key = "prefix+u"
type = "plugin_action"
command = "shop.workstation.open"
description = "Open Shop"

[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "shop.workstation.close"
description = "Shop shutdown preview and checks"
```

修改后执行：

```sh
herdr config check
herdr server reload-config
```

默认 prefix 为 Ctrl+B：按下并松开 Ctrl+B，再按 U 开工；Shift+U 请求安全收工。远程使用时，插件应装在实际运行 pane 的主机，按键转发另看 Herdr 客户端配置。

### 5. 加载并验证

在你准备测试的 Herdr Pi tab 内执行：

```text
/reload
/shop-language zh-CN
/shop-config
```

语言可选 `en`、`zh-CN`、`auto`，也可单独运行 `/shop-language` 选择。语言只影响界面，不决定 agent 回复语言。

在前述 shell 中进行不启动成员的验证：

```sh
git -C "$HOME/.pi/agent/git/github.com/kyrosle/shop-plugin" rev-parse HEAD
git -C "$SHOP_CORE" rev-parse HEAD
python3 "$SHOP_CORE/core/plugin.py" doctor
```

两个 HEAD 应等于 `SHOP_REF`。doctor 是环境检查，不是 Provider 调用或协作任务验收。

## 配置

`/shop-config` 默认打开 **会话** 层；想作为以后新工位的常用配置，先按 Tab 切到 **全局**。

- Tab 切换全局 / 受信任目录 / 会话；↑↓ 选字段，Enter 编辑。
- 为主 Lead、辅助 Lead、快速 Worker、稳健 Worker 选择模型与思考档位；允许多个席位使用同一模型。
- 模型选择器支持模型 ID、名称、Provider 模糊搜索，最多 10 行的定高列表，可上下滚动或翻页。
- “继承父层”清除覆盖；思考档位随所选模型能力变化。
- S 预览并确认保存；R 预览清除当前层模型覆盖；Esc 取消。**这里的 R 不是 `/shop-reset`。**
- 可以保存不完整草稿，但开工前四个执行席位都必须解析到模型，包括尚未启动的辅助席位。

配置优先级：**会话 > 受信任目录 > 全局 > 内置默认**；每层内部 **席位 > 角色 > defaults**。内置不指定模型品牌。

### 改配置什么时候生效？

| 操作 | 影响 |
| --- | --- |
| 保存 `/shop-config` | 后续新建工位；不热切换已有成员，也不改 Pi 默认模型 |
| 当前工位扩员或恢复 | 使用该工位开工时固定的模型快照 |
| `/shop-ui` 的空闲席位模型申请 | 接收方用户另行确认后应用；可选择更新该工位对应席位快照 |
| Architect 的 `/model`、`/thinking` | 由你直接调整当前 Architect，不走远程席位申请 |

已有旧 `models.json` 可通过 `/shop-config migrate` 预览后导入，不自动覆盖。详情见[配置与继承](docs/zh-CN/MODELS.md)。

## 用法

### 1. 开工：只创建席位，不自动派任务

在项目目录中的 Herdr Pi，准备一个只有当前 Pi 的未缩放 tab，终端至少 **140 列 × 40 行**。确认配置已保存，再按 **Ctrl+B → U**。

Architect 保留，主 Lead 和快速 Worker 以全新 Pi 会话启动。辅助席位按需扩员；额外写入者须使用独立、干净、已登记的 worktree。不会自动 stash、reset 或提交你的改动。

### 2. 明确委托一个任务

在 **Architect** 中输入，例如：

```text
/shop 调查登录失败原因，先只读分析，给出复现步骤、根因和修复建议。
```

或明确授权修改：

```text
/shop 修复登录超时问题，修改范围限于 auth 和对应测试；完成后给出测试证据，不要自动 push。
```

`/shop` 仅为**这一次请求**启用委托。普通聊天、问答或修改请求仍由当前 Pi 处理；要把补充交给工位，用 `/shop 补充：…`。

工位须已就绪。目标明确的新任务、且工位未绑定 run 时，Architect 的流程是创建独立 run、写最小必要目标/计划、显式绑定，再交给主 Lead。已有不同任务绑定时先协调，不能混入旧任务。开窗本身不创建或自动选择 run。

默认流程要求 Architect 等待并核对交付后汇总；不是发送一句“已派工”就算完成。后台执行须单独要求，且没有自动唤醒或持续巡检保证。技术细节见[任务工作流](docs/zh-CN/WORKFLOW.md)。

### 3. 查看、干预与交付

| 命令 / 操作 | 用途 |
| --- | --- |
| `/shop-status` | 当前工位状态摘要 |
| `/shop-ui` | 状态与身份、接手/回执、开发准备、交付/集成、空闲席位配置、任务干预、诊断 |
| `/shop-config` | 配置后续新工位的执行模型与思考档位 |
| `/shop-language` | 选择中英文或跟随系统 |
| `/shop-reset` | 预览并确认归档当前 tab 的初始化早期失败登记 |
| Ctrl+B → Shift+U | 请求带身份与任务检查的安全收工，保留 Architect |

工作台查看不自动派单；业务修改还需要明确的 run 绑定。消息送达 ≠ 接手接受 ≠ 工单验收。交付集成只支持确认后的 fast-forward，不自动 push、关闭任务或释放资源。详见[工作台指南](docs/zh-CN/WORKBENCH.md)。

收工前先保存交付、处理完工单、确认写入者停止并显式解除绑定。**`idle` 不证明后台停止。** 当前 Herdr 协议缺少后台进程可见性，收工可能因 `background_state_unknown` 拒绝；不要强行删登记或关窗绕过。

## 失败与重置

开工失败时不要反复按 U：某个写操作可能已经成功，只是返回解析失败。

在**出问题的 tab** 内执行：

```text
/shop-reset
```

它先展示原始失败原因与身份差异，再让你确认。仅接受成员分屏前失败、没有成员或任务绑定、原 Pi pane/终端匹配的情况；显示名称丢失可在严格核对后确认。已有成员、额外 pane、替换终端或未知身份会拒绝。

成功时先把原始登记归档，再移除该 tab 的活动登记：**不关 Pi、不清模型配置、不删任务/worktree、不碰其他 tab，也不自动重新开工。** 准备好后手动按 U。

`/shop-reset` 不是旧 CLI `reset` 的成员关闭操作，也不是全局“强制清缓存”。其他故障走[安全恢复](docs/zh-CN/MIGRATION.md#重置初始化早期失败登记)。

## 升级与保留数据

1. 停止在途工位变更，核对执行成员、任务与写入者；备份配置、登记和旧安装引用。
2. 两端安装同一个新完整 SHA，检查两个 HEAD 和 bridge 指向，再在需要测试的 Pi 中 `/reload`。
3. 保留既有 bridge 和模型/语言配置。升级代码不等于重置工位，也不会自动迁移活动任务。
4. 若仅保留已核实不再执行的失败登记，可在明确选择只升级代码后再单独恢复；不因其他 tab 有旧登记就擅自清理它。详情见[升级与回退](docs/zh-CN/INSTALLATION.md#更新与回退)。

数据按职责集中存放，不全挤在一个文件里：

| 数据 | 默认位置 / 规则 |
| --- | --- |
| 共享代码定位 | `~/.config/shop-workstation/bridge.json` |
| 全局模型与个人语言 | `<bridge.config_dir>/settings.json`、`language.json` |
| 目录配置 / 会话配置 | 项目 `.pi/shop.json` / 当前 Pi 分支的 custom entry |
| 工位登记、成员会话及运行证据 | `<bridge.state_dir>/`；登记在 `runtime/`，按 socket + tab 独立定位 |
| 早期失败登记归档 | `<bridge.state_dir>/reset-archive/` |
| 任务、工单、绑定与交付证据 | 项目 `.shop/` |

安装采用上述命令时，状态目录为 `~/.local/state/shop-workstation`；配置目录由 Herdr 的 `plugin config-dir` 返回。可变数据不放进两份代码 checkout，卸载插件也不代表可以删除任务、会话或 worktree。

## 开发、验证与许可

```sh
npm ci --ignore-scripts
TEST_ROOT="$(mktemp -d)"
SHOP_LOCATOR="$TEST_ROOT/bridge.json" SHOP_CONFIG_DIR="$TEST_ROOT/config" \
  SHOP_STATE_DIR="$TEST_ROOT/state" npm test
npm run typecheck
npm pack --dry-run
```

开发时可显式 `herdr plugin link /absolute/path/to/shop-plugin` 和 `pi install /absolute/path/to/shop-plugin`。只在隔离、非活动环境使用开发 bridge；不要覆盖现有安装或启用两份角色扩展。

离线测试涵盖配置、身份、传输、工单、恢复及 UI 替身；不证明真实模型理解、协作质量或所有宿主交互正确。现场测试应使用专门 tab/隔离项目，别拿活动业务工位试错。没有永久调度器、自动重试或无条件强制关闭。

`private: true` 防止误发 npm，不影响 Git 安装。包不含运行状态、会话、凭据或 worktree。项目许可证尚待所有者决定；参见 [LICENSE](LICENSE)、[THIRD_PARTY.md](THIRD_PARTY.md) 和 [LICENSE.pi-intercom](LICENSE.pi-intercom)，不要假定第三方许可证自动适用于整个项目。

详细资料：[安装](docs/zh-CN/INSTALLATION.md) · [配置](docs/zh-CN/MODELS.md) · [工作台](docs/zh-CN/WORKBENCH.md) · [任务工作流](docs/zh-CN/WORKFLOW.md) · [架构](docs/zh-CN/ARCHITECTURE.md) · [语言](docs/zh-CN/LANGUAGE.md) · [迁移与恢复](docs/zh-CN/MIGRATION.md)
