# 架构

[English](../en/ARCHITECTURE.md) · [中文首页](../../README.zh-CN.md)

Shop 有两个插件入口和一个权威 Python 核心。Herdr 管理可见 Pi 进程；Pi 提供角色提示、工具与交互界面。两个入口都不是永久任务调度器。

## 组件

| 层 | 职责 |
| --- | --- |
| `herdr-plugin.toml`、`core/plugin.py` | Herdr 操作、事件、通知、独立用户收工宿主 |
| `extensions/index.ts`、`state.ts` | 显式 `/shop` 委托、限定范围的工具、紧凑状态视图 |
| `extensions/settings-ui.ts`、`configuration.ts` | 配置 UI、Pi custom entry、短期启动候选 |
| `extensions/workbench-ui.ts` | 只读看板与显式确认的业务操作 |
| `extensions/i18n.ts`、`language-ui.ts`、`core/language.py`、`locales/` | 中英词条、个人语言偏好、稳定菜单操作 ID |
| `extensions/transport.ts`、`endpoints.ts` | Pi 传输生命周期、短期 endpoint 广告、校验后的消息注入 |
| `core/shop.py`、`run.py`、`coordination.py` | 开工位、run 绑定、显式派单与协调 |
| `core/herdr.py`、`identity.py` | 有类型的 Herdr 适配与失败时拒绝授权 |
| `core/contracts.py` | 权威工单、attempt 校验、checkpoint/result 与验收 |
| `core/configuration.py`、`settings.py` | 配置解析、优先级、稀疏覆盖、安全写入 |
| `core/workbench.py`、`development.py` | 接手、模型申请、干预、预览后的 Git 操作 |
| `core/snapshot.py`、`events.py`、`supervision.py` | 共享状态文档、有界事件、有限巡检 |
| `core/transport.py`、`handoff.py`、`transport_cli.py` | 持久去重、发送/回执记录、业务接手状态 |
| `core/repair.py`、`shutdown.py` | 显式恢复、带日志和身份复检的收工 |
| `transport/` | 不依赖宿主的 broker、client 与严格协议 |

Python 的 Herdr 调用均经过 `core/herdr.py`：检查二进制/协议版本、校验 pane/agent schema、设置超时。pane 记录不能证明 agent 名称，授权必须检查 agent 端点。

## 状态与权威

- 安装目录外的 bridge 指定共享 `core_root`、`state_dir`、`config_dir`。
- 本机状态保存成员、启动身份、会话和操作日志；项目 `.shop/` 保存权威绑定、票据与证据。
- 一个 Shop 最多绑定一个 run；一个 run 仅有一个工位所有者。`current.json` 变化不会改派已有任务。
- 唯一主 Lead 负责派单与协调。只有当前 assigned 实例能提交自己的 checkpoint/result，且必须匹配当前 attempt。
- 运行状态、凭据、对话历史和用户配置不属于发布包。

锁与身份校验约束遵守协议的 Shop 调用方。插件以操作系统用户权限运行，不是沙箱；无法阻止任意 shell 写入，也不能原子冻结另一 pane 的人工输入。

## 身份与观测

持久 phase（如 creating、ready、partial）描述操作阶段，不代表实时健康。ready 登记仍可能包含缺失、移动或替换成员；pane 缺失不证明后台进程停止。

launch、terminal、Pi session 是不同身份。transport epoch 属于短期 endpoint 广告，不写入持久成员记录。广告用于发现精确目标，broker 在发送时检查 epoch；未知或过期身份不授权改目标、重启或重放。

所有状态界面复用 `core/snapshot.py` 的 `shop.snapshot/v1`：

- `desired`：登记身份与位置。
- `observed`：实时事实，可附事件注解。
- 工单：状态、owner、attempt、delivery、依赖与有界 checkpoint。
- transport/handoff：分别记录技术证据和业务证据。
- attention：检查建议，不是自动判定卡死、死亡或完成。

实时读取优先于事件。事件没有业务授权，不派单、验收、暂停、关闭、聚焦或重试。支持固定 schema 的 `pane.agent_status_changed`、`pane.closed`、`pane.exited`，忽略错误/外来事件。每个 Shop 事件事实最多 2,000 行 / 512 KiB。

快照有界且采用字段白名单，排除提示词、对话、环境内容和消息正文。自由文本会脱敏，但本地状态不是匿名导出，仍可能包含相关路径和身份。对外分享应使用工作台独立的白名单诊断导出。

## 消息与接手链路

```text
显式 Pi 工具 / 工作台操作
  → Python 授权与持久准备
  → 调用 Pi 的传输客户端
  → Shop 私有 broker
  → 接收身份检查与持久去重
  → Pi 公共消息 API
  → 接收者显式业务回执
```

`herdr-shop message/dispatch` 只准备记录，实际发送通过 Pi 工具；不使用外部 intercom 路由或 Herdr prompt 降级。broker 仅传递有界 envelope，不派单或决定验收。

接收端先记录再注入；窗口内崩溃或切换会话会保留未决结果，不自动重注入。wire 身份与业务身份必须匹配；busy 消息用 Pi steering，不中断回合。

传输送达/注入不是接手接受；handoff 交付不是工单验收。任务完成仍由 result 与主 Lead review 决定。参见[工作流](WORKFLOW.md)、[工作台](WORKBENCH.md)。

## 配置与运行时变更

优先级：会话 > 受信任目录 > 全局 > 内置；每层内席位 > 角色 > defaults。Python 负责校验、稀疏覆盖、内容哈希 CAS 和原子写入；Pi 负责模型能力与当前分支 custom entry。参见[配置](MODELS.md)。

新工位固定 `model_profiles`；`launch_profile` 是请求值，不是实时观测。保存配置不改变现有会话。独立的空闲席位申请由接收 Pi 用户确认；applying/unknown 阻止派工，直至完成或人工检查。Architect 保持原生 `/model`、`/thinking`。

语言偏好存于独立 `language.json`，不参与这些配置层或工位快照。中英文共用词条键和稳定操作 ID，只改变显示；协议、原始证据与用户内容保持原样。参见[语言](LANGUAGE.md)。

## Git 与干预

开发准备仅在预览/确认后创建新的隔离分支和 worktree。集成仅支持显式 fast-forward；计划绑定仓库事实并有时效，执行前复检。不自动 stash、reset、清理、回滚或 push；失败保留现场。

需求修订、暂停请求、Esc 送达、已核实停写和取消是不同事件。保留历史 attempt 与证据。人工声明后台写入者停止时只记录声明，不伪造进程证据。

## 收工与恢复

执行收工的权威是独立 Herdr 用户操作宿主。agent 工具只能看预览，没有强制关闭能力。计划包含身份、绑定、进程、transport/handoff、布局事实、时效与状态摘要；执行复检，变化则拒绝。

绑定 run、active/unknown 成员、身份冲突、缺失进程事实、未决消息/接手均阻止收工。Herdr 协议 22 提供前台事实；`core/processes.py` 在 macOS 通过本地 Unix 对端身份与两次稳定的元数据快照核验 pane 后代、会话、终端及进程组，仅豁免核实后的直接 Pi。进程实例变化使计划失效，关闭后核实原 shell/Pi 退出。证据不足仍报 `background_state_unknown`。这是 pane 范围观测，不证明完全脱离 pane 的独立服务或所有仓库写入者均已停止。

操作日志依次记录归档、逐目标关闭及缺失验证、最终检查、回执，最后移除登记；Architect 保留。失败保留 `shutdown_partial` 和剩余名单。恢复计划只读，恢复执行另需显式操作；不自动 unbind、验收或删除代码、worktree、会话。参见[迁移与回退](MIGRATION.md)。

## 验证限制

离线测试使用临时 Git 仓库、本地 broker 与 Pi/Herdr 替身，验证机制与契约，不证明真实模型理解和质量。实际 Provider、Pi UI、Herdr 生命周期、后台停写安全必须在隔离环境独立验证，再用于活动项目。

来源与许可证见[第三方声明](../../THIRD_PARTY.md)和[传输层归属](../../transport/NOTICE.md)，不得删除版权归属。
