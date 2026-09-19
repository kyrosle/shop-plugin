# 旧版本迁移策略

[English](../en/MIGRATION.md) · [中文首页](../../README.zh-CN.md)

当前 alpha 不自动迁移或替换已安装工位脚本。受控切换前保留旧快捷键和扩展；同一 Pi 会话不能同时启用新旧角色扩展。

切换前：

1. 清点旧工位、活动绑定、未完成工单与存活进程。
2. 完成或显式阻塞任务，验证写入者停止；关闭 pane 不证明进程停止。
3. 在 Git 仓库外备份旧配置、运行状态、角色提示与 Herdr 快捷键。
4. 在隔离状态目录验证新包测试与 doctor。
5. 配置新 bridge 与模型，停用旧扩展、安装新包、reload 目标 Pi。
6. 只替换冲突的 U / Shift+U 条目为 plugin_action，保留其他快捷键。
7. 新建测试工位，不将旧 ready 登记复制到新 runtime。

回退：安全停止新工作，恢复原快捷键/扩展配置并 reload。保留两代状态/会话作为证据；不 reset、stash 或删除业务 worktree。
活动 run 或会话跨代迁移需要未来专用迁移命令，不允许手改 JSON。

卸载/unlink 只移除登记或代码，不自动删除项目 `.shop`、bridge、配置、状态和会话。清理须另行预览并获得用户授权。

## 安装、切换与回退检查

包版本与 `herdr-plugin.toml` 必须一致（当前 `0.1.0-alpha.1`）。传输协议为 `pi-shop-transport` v1/schema 1，快照为 `shop.snapshot/v1`。不兼容时拒绝，不自动降级或改写。

### 切换前

- 清点活动工位/绑定/工单/进程、Pi 扩展来源、Herdr 插件链接、快捷键、Shop broker/socket/lock，以及已有 pi-intercom 实例。
- 活动 run、未知写入者或仍在进行的初始化/成员变更存在时拒绝切换。活动 run 迁移不受支持；先完成协调并 unbind，不手改活动状态。
- 保留的失败登记与仍在执行的操作不同。核实执行/变更已停止且没有未知写入者后，可明确授权只升级代码、逐字节保留登记。恢复/reset 是单独操作，只针对目标 tab；其他 tab 的旧记录不是清理授权。
- 预览每项文件/配置变化，在仓库外备份 bridge、模型/角色配置、语言偏好、Herdr 快捷键、旧插件引用与版本信息。
- 不修改已安装 pi-intercom、不接管其 socket，不把 unlink 当进程停止，不同时开两个 Shop 角色扩展。
- 先在隔离 profile 安装精确测试过的 tag/commit，运行 doctor、`npm test`、`npm run typecheck`、`npm pack --dry-run`。

### 切换

显式停用旧扩展，安装新包、配置 bridge、仅替换冲突快捷键、reload 选定 Pi，再创建未绑定的新测试工位。

### 回退

- 安全停止新工作；bound/active/unknown 时拒绝。
- 恢复备份的插件引用、快捷键、bridge 与配置，显式 reload。
- 通过真实 socket/进程证据确认新 broker/进程已停止；卸载不构成证明。
- 仅回退到协议兼容版本。至少保留两代状态/会话/证据，不删除 worktree 或凭据。

## 重置初始化早期失败登记

在出问题的 tab 中执行 `/shop-reset`，交互式重置该工位登记。不接受参数或强制选项；**不是**会关闭成员的旧 `core/shop.py reset`，也不是全局清缓存。

1. 先展示当前 tab 登记、原始开工错误及 Architect 名称差异；取消不会修改登记。
2. 仅允许 `partial`、尚未进入成员分屏阶段、没有 Lead/Worker 登记、没有 run 或对应项目绑定、tab 内只有原 Pi pane/终端的情况。同工位具名成员仍存在（包括移到其他 tab）时拒绝。
3. 名称丢失只有在原 pane、tab、终端及项目仍匹配时才可明确确认。另一个具名 agent、替换终端或未知证据均拒绝。没有阶段标记的旧登记，仅接受可明确定位为分屏前 Architect 重命名失败的记录。
4. 确认后取得同一 tab 的操作锁，重新核对预览 token。登记、绑定或身份变化会阻止执行。先将登记原始字节以私有权限归档至 `<bridge.state_dir>/reset-archive/`，再移除活动登记。结果未知时不自动重试，先查原登记与归档。
5. Pi、pane、模型/语言设置、票据、worktree 和其他 tab 均保留。不自动重启或开工；准备好后手动开工。

已就绪工位、成员创建中的失败、活动任务和身份不明，仍需安全关闭/恢复流程。不要通过删除文件绕过拒绝。离线测试覆盖上述守卫；真实 Pi/Herdr 恢复验收仍需另行执行。

## 收工与语言

Shift+U 调用独立用户收工宿主（`core/plugin.py` → `core/shutdown.py`）。绑定 run 时拒绝；顺序仍为：处理完工单 → 显式 unbind → 用户收工。
`core/shop.py shutdown --preview` 只读诊断；执行必须具有 Herdr plugin-action 上下文。

语言偏好是独立 `<bridge.config_dir>/language.json`。无需迁移模型设置，语言切换不会重启或重新配置现有工位。旧版本可能忽略该文件；回退时保留，不混入 `settings.json` 或 runtime。
