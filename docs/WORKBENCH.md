# Shop 工作台

入口：Herdr 内 Pi 的 `/shop-ui`。需已登记 Shop；查看不派工、不聚焦、不关闭窗口。业务变更还要求显式绑定 run。

## 七个入口

| 入口 | 行为 | 不代表什么 |
| --- | --- | --- |
| 状态与身份 | 同一 `shop.snapshot/v1`、成员/票/attempt/checkpoint、请求、配置与 endpoint 广告 | idle 不代表任务完成；endpoint 广告不是 broker 连接证明 |
| 接手请求 | 有界目标/范围/验收/证据；精确接收实例通过 `shop_handoff` 或待办回执 | delivered/injected 不等于 accepted；handoff deliver 不等于 ticket accept |
| 开发准备 | 固定 SHA、clean 状态、分支、路径和 Git worktree 清单；确认后创建新分支/worktree | 不启动成员、不创建票、不提交脏改动 |
| 交付与集成 | 读取当前 attempt 的 result；预览并确认后仅 `merge --ff-only`；登记最终检查证据 | 不自动 push、unbind、finish 或释放资源 |
| 空闲席位配置 | 请求目标 Pi 在自己的待办中确认，用公共 API 应用 | 源端 requested 不等于目标 applied；不重启、不改会话文件 |
| 任务干预 | supplement / scope-change / pause / cancel 分开 | 收到请求、发 Esc、实际停止、取消工单是不同状态 |
| 诊断与脱敏导出 | 只读诊断；单独确认后写本地白名单 JSON | 不上传、不修复、不包含原始消息/提示词/环境/路径/身份 ID |

## 消息与接手

实际发送必须使用 Pi 工具 `shop_message`、`shop_dispatch` 或工作台。
`herdr-shop message/dispatch` CLI **只准备业务记录**，返回 `transport_request`，不会调用 Herdr prompt。
不要把 prepared 当 sent，也不要手动重放返回的 envelope。

`shop_dispatch` 固定 assigned/attempt，创建 handoff，再由调用 Pi 内置 transport 发送。
接收者检查固定票据后，用 `shop_handoff({id, transition: "accept"})` 接手；缺上下文用 `needs_context`，拒绝用 `reject`。
提交 result 与主 Lead 的 ticket accept 仍是独立步骤。

传输分开记录 submission 与 receipt；无回执/超时/崩溃窗口保留 unknown，不自动重发。
消息有 durable 去重日志，但轻消息不创建任务票。旧 launch/session、旧 attempt、错误 wire/body 身份拒绝。
Pi 会话生命周期及每 5 秒的成员登记检查仅连接/刷新 endpoint，不轮询派工、唤醒模型或重试消息。
登记文件不保存 transport epoch；其中 null 表示未知。运行时 epoch 由短期 endpoint 广告与 broker 校验提供。

## 开发与交付边界

第一版向导仅创建**新**分支和不存在的隔离 worktree；不接管已有目录，不创建嵌套在其他 worktree/登记 cwd 下的目录。
计划有效期 5 分钟，执行前重读 HEAD、分支、clean 状态和 worktree 清单。变化则重新预览。
创建失败保留目录、分支和 unknown 记录，不自动清理。随后按现有 `add-worker/add-lead --cwd`、ticket new、`shop_dispatch` 流程显式启用执行席位。

集成仅允许 accepted 结果、其他票已 accepted/cancelled、clean 目标，以及目标 HEAD 是选定结果提交的祖先。
UI 另要求操作者核实所有前台/后台写入者停止；核心复检其他登记成员 idle/done、无 assigned/review。
**人工停写声明不是 Herdr 自动证明**。协议缺少后台进程可见性，无法确认时只看计划，不执行。
不支持自动 cherry-pick、冲突处理、回滚或批量集成；失败保留现场。

最终检查入口记录操作者提供的命令、退出码、目标 commit、证据路径与 SHA-256。
插件不执行这些检查，不把它们标成独立复验。HEAD 或证据文件变化后，`current=false`。
运行结束仍按原流程：SUMMARY/REVIEW → 显式 unbind → shop-run finish；窗口关闭另走既有 shutdown 预检。

## 单席位模型申请

1. Architect/主 Lead 选择非 Architect 席位、模型和明确 thinking 档位。
2. 目标必须 idle/done，无 assigned/review；sender/recipient 的 launch、terminal 与 Pi session 必须仍匹配。
3. 请求落为 requested；目标用户在自己的 `/shop-ui` → 待办中再次确认。
4. 接收 Pi 再查 provider/model/thinking 能力，然后 claim → 公共 API → applied/rejected/unknown。
5. applying/unknown 阻止该席位派工。未知结果须在目标 Pi 查看原生当前配置，并明确“核对并解除阻塞”，不能自动重试。

可选择只改当前会话，或同步该 Shop 的目标席位 `model_profiles` 供后续启动使用。
不改 `/shop-config` 的全局/目录/会话层，不覆写历史 `launch_profile`。
运行时申请要求明确 thinking；Pi 默认/null 仍仅在 `/shop-config` 启动配置中使用。
Architect 继续使用原生 `/model`、`/thinking`，不接受远程申请。

## 需求、暂停、取消

- supplement：内置轻消息，可明确给 busy 成员 steer；不改范围/验收。
- scope-change：记录 revision 和受影响 ticket/attempt。主 Lead 确认后启用 revision；旧 ready 票必须显式 revise 才能 dispatch，assigned 不改写。
- pause：先 requested。主 Lead 可显式执行 Esc，结果仅为 `pause_requested`，不声称 stopped、不重新派工。
- cancel：主 Lead 显式执行；需停写声明与原 writer 身份/状态复检。未处理依赖拒绝；保留代码/checkpoint/result。
- retry：继续使用已有 `--writer-stopped` 与 attempt fence，不新增自动重派路径。

## 验证范围

离线验证覆盖业务状态机、真实临时 Git 仓库、假 Herdr 身份、内置 broker、Pi UI/API 替身。
真实 TUI、Provider/model、Herdr 快捷键、会话切换、扩员/恢复与后台停写均须单独受控验收。
自动测试不证明真实 AI 的任务理解或协作质量。使用前请在隔离环境验证实际 Provider、Pi 和 Herdr 组合，不要直接替换活动工位。
