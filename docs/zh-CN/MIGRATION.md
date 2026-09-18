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
- 活动 run、未知写入者或部分完成操作存在时拒绝切换。活动 run 迁移不受支持；先完成协调并 unbind，不手改活动状态。
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

## 收工与语言

Shift+U 调用独立用户收工宿主（`core/plugin.py` → `core/shutdown.py`）。绑定 run 时拒绝；顺序仍为：处理完工单 → 显式 unbind → 用户收工。
`core/shop.py shutdown --preview` 只读诊断；执行必须具有 Herdr plugin-action 上下文。

语言偏好是独立 `<bridge.config_dir>/language.json`。无需迁移模型设置，语言切换不会重启或重新配置现有工位。旧版本可能忽略该文件；回退时保留，不混入 `settings.json` 或 runtime。
