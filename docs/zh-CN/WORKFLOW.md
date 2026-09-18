# Shop 任务工作流

[English](../en/WORKFLOW.md) · [中文首页](../../README.zh-CN.md)

命令是文件/Herdr 的确定性封装，不是异步 agent 框架。JSON 为权威记录；Markdown 保存任务规格、计划、人工状态与审查。工具无法证明语义完成、阻止任意 shell 写入或原子冻结另一 pane 的人工输入。
界面语言不决定回复语言；遵循用户要求，不自动翻译代码、路径或证据。

## 1. 固定工位与 run 绑定

```bash
<package>/bin/shop-run new --independent "任务标题"
# Open workstation with Ctrl+B,U. Then explicitly bind the printed ID:
<package>/bin/herdr-shop bind <run-id>
<package>/bin/herdr-shop status
```

开工只安排席位，不自动选择 run 或启用委托。仅 `/shop <任务>` 对本次请求启用 Architect；普通消息仍由当前 Pi 直接处理，包括改代码/测试，但须检查已有写入者避免冲突。
`/shop` 要求调用方空闲且当前 Architect 登记 ready，否则报告未派送，不自动开其他工位。后续普通消息不自动转交 Lead；用 `/shop 补充：...` 明确补充。
跨项目访问不等于授权新建 Shop 或接管别的 Pi。现有绑定只用于对应任务。

未绑定且新任务目标明确时，用 `new --independent` 独立创建 run，不读写 current.json 或旧 run/绑定；绑定返回的 created ID。无关 current 指针不是阻塞，不强迫沿用，也不因此绕过 Lead。仅在更换现有绑定或任务意图不明确时询问。
主 Lead/Architect 只能绑定受管理的 active/blocked run。一个 run 仅一个工位所有者，一个工位最多一个 run。其他 tab 改 current.json 不改派已有任务。
`.shop/bindings.json` 阻止误删除，runtime 保存 run_id 和唯一 shop_id；失配/过期则拒绝并要求检查，不自动接管。仓库锁串行化 bind/finish/GC/contracts，工位锁串行化本地 pane 操作。

没有 shop_id 的旧工位须先安全 reset/reopen；不静默转换未管理 Markdown run，也不视作完成。从辅助 worktree 调用时始终传 `shop-run --repo /absolute/main/repo`。

## 2. 可复现开发基线

development 票要求精确 40/64 字符 base SHA、同 Git common 仓库的 worktree 根、干净 worktree（不计 `.shop` 协调文件）、HEAD 等于 base、非空 checks。创建和派单均校验。
不自动 commit/checkout/复制脏代码。先由用户决定保留改动方式；安全退路是 analysis，不是在旧 HEAD 偷做开发。worktree 不自动删除。

analysis 可明确读取主仓库当前脏源码，即使进程位于隔离 worktree；development 不享此例外。API 阻止两个开发票同时拥有同一 worktree，但不能管住人工编辑或原始 shell 写入。

## 3. 会话与 checkpoint

Lead/Worker 每次启动用 `--session-dir` 创建全新本机会话，位于 `<state_dir>/runtime/sessions/`。不使用 --continue、--fork、--resume，也不重放父会话。
关闭 pane 不删除会话。绑定 run 保存 session-refs/*.json，实际会话留在本机；旧 --no-session 实例无法追溯恢复。

Worker 每个有意义阶段或长操作前显式发布 checkpoint，每个 attempt 原子替换一份，不逐 token 记录、无后台计时器。崩溃后可读最近一次保存；未保存尾段仍可能丢失。
Pi 原生会话恢复是人工抢救手段，不是正常交接。GC 不删除 session 目录，因为席位重新绑定后可能被多个 run 引用；清理前核对引用和活动 agent，再单独授权。

## 4. 工单、结果、重试与唯一调度者

只有主 Lead 修改工单协调；只有精确 assigned 实例（name/pane/terminal ID）发布自己的 result/checkpoint。身份检查是协作防护，不是 OS 权限；不得手改权威 JSON 绕过。

在 run evidence/ 或临时文件创建 analysis 草稿：

```json
{
  "ticket_id": "T001",
  "owner": "<name from herdr-shop status>",
  "objective": "Analyze current project entry points",
  "kind": "analysis",
  "worktree": "/absolute/source/repo",
  "base_commit": null,
  "scope": ["apps/desktop/src", "package.json"],
  "checks": [],
  "depends_on": []
}
```

开发票使用 kind=development、精确 base_commit、独立 worktree 和必须执行的 checks（如 `bun run test`）。scope 是字面相对文件/目录，不是 glob；结果越界会拒绝。analysis 不得报告代码修改。依赖必须先存在，正常 API 因而不能创建环。

主 Lead：

```bash
shop-run --repo <main> ticket new <run> --file <draft.json>
# Pi tool (not shell): shop_dispatch({"ticket":"T001"})
# herdr-shop dispatch T001 only prepares a record; it does NOT send.
```

派单检查绑定、依赖验收、登记 owner idle/done、唯一分配与基线。先持久 assigned/attempt/实例身份并准备 handoff，再由调用 Pi 内置传输发送。
CLI 只准备记录，发送须用 `shop_dispatch`。submitted/delivered 不是业务接受或完成；未知结果必须检查，不盲重试或重复通知。
Lead 可用 Herdr wait/get 看生命周期，验收必须读 result。人工干预 Worker 应通知 Lead。

checkpoint 草稿：

```json
{
  "run_id": "<run>", "ticket_id": "T001", "attempt": 1,
  "owner": "<assigned-name>", "base_commit": null,
  "progress": "Read entry points", "next_steps": ["Inspect IPC"]
}
```

```bash
shop-run --repo <main> ticket checkpoint <run> T001 --file <checkpoint-draft.json>
```

result 草稿：

```json
{
  "run_id": "<run>", "ticket_id": "T001", "attempt": 1,
  "owner": "<assigned-name>", "base_commit": null,
  "status": "completed", "summary": "Entry points verified; see report",
  "result_commit": null,
  "changed_files": [], "checks": [], "remaining_work": []
}
```

每项已执行检查包含 `{ "command": "exact ticket command", "exit_code": 0, "evidence": "/absolute/existing/output-file" }`。
completed 开发结果必须覆盖全部要求的命令、检查成功、remaining_work 为空。未提交时 result_commit=null（不是自动 commit 授权）；提供时必须解析到提交。
blocked/failed 可报告缺失或失败检查。证据存在/schema 正确不证明真实执行，Lead 须独立检查/复跑，不用行数验收。

```bash
# Assigned Worker/auxiliary Sol:
shop-run --repo <main> ticket publish <run> T001 --file <result-draft.json>
# Primary Lead after independent review:
shop-run --repo <main> ticket accept <run> T001 --file /absolute/review.md
```

publish 先原子写不可变 `T001.a1.result.json`，再标记 review。第二步中断需协调恢复，不自动覆盖。
状态：ready → assigned → review → accepted；blocked/failed 是待 Lead 处理的结果。放弃用 cancel，并声明旧 writer 已停。

```bash
shop-run --repo <main> ticket retry <run> T001 --writer-stopped
shop-run --repo <main> ticket cancel <run> T001 --writer-stopped
```

retry 要求核实旧 writer 停止；busy/blocked/unknown 或移动实例被拒绝。旧票归档、attempt 增加、结果不可变，迟到写入拒绝。
retry 不发消息，需另行显式 dispatch。开发重试再次满足基线，保留半成品并询问用户，不自动 reset。

## 主 Lead 巡检与扩缩员

主 Lead 在当前活动任务回合中调用：

```bash
herdr-shop patrol --seconds 60
```

默认立即快照；seconds 范围 0..120。约每 5 秒刷新成员与状态，在变化、审查/检查提示或期限到达时返回。
等待时不持有 Shop/仓库锁，不阻塞结果、checkpoint 或成员操作。这是有限前台等待，不是服务或周期性模型调用。Lead 读报告后决定是否再次调用；单个 Herdr API 调用仍受自身超时管理，巡检窗口不等于杀进程期限。API 失败记未知，不宣布死亡。

快照含辅助成员、票据状态/attempt/delivery、有界 checkpoint 进度与时间、依赖、每角色最多 2 人的容量和可移除候选。checkpoint 缺失或超过约 180 秒只提示检查，不自动干预。必要时读约 60 行 recent-unwrapped；屏幕不是交付证据。

先复用空闲成员；独立 ready 工作且已有成员忙碌、确有并行收益时才 add-lead/add-worker，必须干净独立 worktree。新增/移除会在下次 patrol 体现。
辅助 Lead 只执行或审查，不是第二调度者。移除前其票全部 accepted/cancelled、无依赖和后续任务，避免反复开关。扩员只允许 Architect/主 Lead；布局与角色上限仍有效。

```bash
# Only primary Lead; records reason and sends Esc, never auto-kills/closes/reassigns:
herdr-shop pause <registered-executor-name> --reason "Observed wrong source directory ..."
```

pause 要求精确匹配 working Pi；blocked/unknown 先人工检查。结果只表示发 Esc，不是 writer 停止。核查生命周期与前台/后台作业，不丢改动，再用 retry 增加 attempt。
仅 ready 票可 revise：

```bash
shop-run --repo <main> ticket revise <run> <ticket> --file <patch.json>
```

可改 owner/objective/worktree/scope/checks/base_commit，不改 run/id/attempt/依赖；开发基线仍校验。用于缩范围或转给新成员，不转移活跃 assignment。revise 后显式派发，保留旧 attempt/result。移除后可复用逻辑名，但实例与 attempt 校验仍生效。

Lead 回合结束即停止巡检。用户取消、API 失败、Lead 崩溃必须报告/恢复，不承诺隐形后台监管。Architect 等待高层结果和重要阻塞，不逐屏监看 Worker。

## Architect 等待约定

默认同步文件交接：用 `shop_message` 把 run 路径/目标交主 Lead，通过 Herdr 生命周期工具等待，读取 SUMMARY.md/REVIEW.md 后向用户汇总。
等待 Herdr 不是持续模型循环。超时表示待完成：get 检查，working 则继续 wait，不重发原任务。
blocked/检查失败如实报告；idle/done 缺交付要检查并续接，不声称成功。只有明确要求后台/不用等才立即返回，且没有自动唤醒保证。不在未等待、无通知机制时承诺稍后自动总结。

## 5. 显式恢复，而非永久调度

```bash
herdr-shop resume
```

写 RECOVERY.json：实时 pane/进程、票据状态、checkpoint 路径；不启动、不发任务、不改派。检查失败记未知，不判死亡。先核对不确定派送与旧 writer，再决定 retry。

原主 Lead 崩溃且原 pane 已是 shell：

```bash
# Architect only
herdr-shop recover-lead --dry-run
herdr-shop recover-lead --apply
```

要求原 pane 存在且仍在原 tab、无检测到的 agent、仅前台 shell；运行中或被替换则拒绝。原地启动全新主 Lead，仅交 RECOVERY.json 路径并要求先报告恢复计划。
不关闭 Worker、不重放对话、不自动恢复票。已关闭/移动的 Lead pane 或过期 server ID 需要人工修复，不猜测替代者。

## 收尾与保留

辅助成员有未验收/未取消票时拒绝移除。顺序：

1. 保存 SUMMARY.md/REVIEW.md，票全部 accepted/cancelled。
2. 其他写入者停笔，`herdr-shop unbind --handoff-complete`。
3. `shop-run finish <run> --handoff-complete`。
4. Ctrl+B、Shift+U 再尝试关闭空闲席位。

finish/GC/delete 拒绝绑定 run，reset 拒绝绑定工位。崩溃后残留绑定是保守保护，先检查状态/进程再人工协调。
current.json 仅供人查看，不作派单路由。GC 先预览，保留最新 5 份及 7 天保护，仅清 tickets/evidence。session 引用/总结保留；物理 worktree/session 单独明确清理。

## 6. 用户收工与恢复计划

收工是**用户操作**，不是 agent 能力。Shift+U 启动独立 `core/plugin.py` close 操作，在 Herdr plugin-action 上下文中预览并执行。
agent 只能 `herdr-shop shutdown --preview`、`herdr-shop recovery` 诊断，不能执行关闭。

顺序仍为处理工单 → 显式 unbind → 用户收工。绑定或登记绑定产生 bound_run/outstanding_ticket 阻塞；收工不自动 unbind、finish 或 accept。

预览检查精确 socket/tab 登记、phase、绑定、member/launch/terminal 身份、实时状态、布局、未登记 pane、进程、transport/handoff 未决项，并在 `<STATE>/shutdown/plans/<shop>/` 写 plan_id、摘要、关闭顺序、时效和 0600 文件。
执行复检全部事实，变化即拒绝；日志顺序为归档 → shutdown_closing → 逐目标缺失验证 → 最终验证 → 回执/tombstone → 移除登记。调用方执行 pane 最后关闭，Architect 保留并核实。

以下均失败拒绝：绑定/未完成票、partial/removing/resetting/shutdown_*、active/blocked/unknown、缺失/移动/替换/重复/身份冲突成员、未核实 Architect、外来 pane、坏/超大状态、过期计划、transport unknown/pending、未完接手、前台工作、缺失进程事实、background_state_unknown。
Herdr 协议 22 无完整后台/后代进程清单，能力缺失须报告，不推定停止。失败留 shutdown_partial 和剩余名单，不发成功通知。
重复关闭只有匹配回执且已核实目标缺失才返回 already_closed，否则 unknown 且不操作。

recovery 对部分存活、移动、服务器重启、混合布局生成只读协调计划：分类成员、列显示建议，不恢复、改派、重放、关闭或删除。
实际恢复须匹配未过期计划，并由 Architect 显式执行独立 dry-run/归档流程。切换与回退见[迁移](MIGRATION.md)。
