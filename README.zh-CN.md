# Pi + Herdr Shop

[English](README.md) · 简体中文

**本地 alpha。一个仓库、两个插件入口、一个权威 Python 核心。**
可见的独立 Pi 进程，文件工单交接，唯一主 Lead 负责派单和验收。
不是 Pi 子进程编排框架；不克隆对话、不运行守护调度器、不拦截通用工具。

## 功能与限制

- Herdr 插件：开工、收工、状态、恢复报告、环境检查。
- Pi：仅显式 `/shop <任务>` 委托；普通消息保持单 agent。工具包括 `shop_status`、`shop_patrol`、`shop_dispatch`、`shop_message`、`shop_handoff`，另有 `/shop-status` 查看状态。
- `/shop-ui`：只读看板、精确实例接手/回执、新 worktree 准备、交付/ff-only 集成计划、空闲席位模型申请、scope/pause/cancel 干预和白名单诊断导出。见[工作台](docs/zh-CN/WORKBENCH.md)。
- `/shop-config`：全局/受信任目录/会话模型与思考配置、显式旧配置迁移、CAS 保存、Pi→Herdr 启动候选。现有工位保留固定快照。见[配置](docs/zh-CN/MODELS.md)。
- 工单、绑定、checkpoint、验收、重试、清理；区分成员存在、缺失、移动、身份冲突与未知。
- 同 Shop envelope 校验发送/接收 launch ID；全新会话；只剩原 Architect 时可显式恢复。

尚不代表可直接替换生产工位：

- 部分/混合存活布局先看只读恢复计划，再显式执行，不自动重建。
- Herdr 事件仅提供有界、非权威事实；工作台显式刷新共享快照。
- `shop_message`/`shop_dispatch` 使用内置 transport 与持久回执，无 Herdr prompt 降级；CLI 只准备记录。busy 消息须显式 allow_busy；送达不是业务接受。
- 启动身份与 Pi 会话身份分离；短期 endpoint 广告会过期。旧 launch/session 的操作拒绝；会话切换和 endpoint 生命周期仍需现场验证。
- 动态角色注入仅 Architect；执行席位使用启动时提示文件。
- 不自动迁移旧安装；打包后的开工/恢复尚未完成真实端到端验收。
- POSIX 实现声明支持 Linux，但目前只在 macOS 测试；Windows 不支持（fcntl、shell wrapper）。

## 要求

- Herdr >= 0.9.0，CLI/server 协议一致；Pi API 依赖兼容 0.85.1。
- Python >= 3.9、Node >= 22.19.0、Git；Bun 仅开发测试需要。
- 用户自己配置 Pi Provider/模型。包内没有凭据或固定个人模型选择。
- 保留 `@ogulcancelik/pi-herdr`，用于通用 agent read/wait/control 工具。

## 本地开发安装

以下是**明确安装步骤**，不是自动安装脚本。不要替换活动中的旧工位。

```sh
cd /absolute/path/to/shop-plugin
npm ci --ignore-scripts
npm test
npm run typecheck

# Preview first. Refuses to overwrite an existing bridge on apply.
python3 core/plugin.py configure
python3 core/plugin.py configure --apply
```

默认 bridge：`~/.config/shop-workstation/bridge.json`，为两个入口指定权威代码、配置、状态目录。
用其他 bridge 时两个宿主应一致设置 SHOP_LOCATOR；配置时可用 SHOP_STATE_DIR、SHOP_CONFIG_DIR 选目录。不得把可变状态放进安装目录。
Herdr 插件上下文配置时可采用其插件配置/状态目录。

加载扩展后，在 Herdr Pi 用 `/shop-config` 设置各层模型与思考档位；各席位可不同。
旧 models.json 可 `/shop-config migrate` 明确导入，不自动覆盖。
Architect 保留已有 Pi 会话，模型用 `/model`、思考用 `/thinking`；Shop 不自动重置会话或切模型。

```sh
# Use compatible Herdr executable, not an old PATH copy.
herdr plugin link /absolute/path/to/shop-plugin
pi install /absolute/path/to/shop-plugin
```

加载前停用旧手装 herdr-shop-mode，安装后 `/reload`；不要同时启用两个角色注入扩展。
先备份，再手动配置快捷键：

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
description = "Close idle unbound Shop"
```

替换冲突条目，不重复追加。远程快捷键转发取决于 Herdr 客户端；插件安装在实际运行 pane 的服务器，远程行为需独立验证。
使用此包明确的 bin/herdr-shop、bin/shop-run 路径，不覆盖全局 wrapper。
必要时设置 SHOP_HERDR_BIN 指定兼容版本；Herdr 插件通常使用注入的 HERDR_BIN_PATH。

## 中英文

`/shop-language` 选择 English / 简体中文 / 跟随系统；也可直接执行：

```text
/shop-language zh-CN
/shop-language en
/shop-language auto
```

CLI：`bin/herdr-shop language [auto|zh-CN|en]`。语言是个人偏好，与模型配置分开，不修改在途工位。
详情见[语言指南](docs/zh-CN/LANGUAGE.md)。

## 安全与恢复

插件以你的 OS 用户权限执行，不是沙箱。安装前检查代码。文件锁/身份检查只约束 Shop 操作，不控制任意 shell。

- Ctrl+B、U：从单个未缩放 Pi pane 开工，至少 140 列 × 40 行。新工位需要有效 Pi 配置候选；缺失时在原 Architect `/reload` 或 `/shop-config`。`setup --models-file ...` 是明确绕过会话层的替代入口。
- 保留 Architect，主 Lead/Worker 全新启动；最多 2 Lead + 2 Worker。
- 新增写入者必须独立、干净、登记的 worktree；不自动 stash/reset 用户改动。
- 人工关闭不取消票，也不证明后台停止；shop_status 只报告观测。
- 执行 pane 全部缺失时，开工可归档并恢复登记席位，不重发任务或自动 retry。
- 部分缺失、移动、替换时拒绝；先检查，不删登记绕过。
- pause 只发 Esc；retry --writer-stopped 前核实前台和后台作业。
- 验收看报告/结果，不看屏幕文本或 idle。
- 活动 run 阻止收工/清理；先处理完工单，再显式 unbind。
- 不自动唤醒模型；Lead 巡检只在其活动回合内执行。

详见[工作流](docs/zh-CN/WORKFLOW.md)、[架构](docs/zh-CN/ARCHITECTURE.md)、[迁移](docs/zh-CN/MIGRATION.md)。

## 分发与许可

包不包含运行状态、会话、用户配置、凭据、worktree。`private: true` 阻止误发 npm，仍可通过 Git 安装 Pi 包。
项目许可证仍待确定。THIRD_PARTY.md、LICENSE.pi-intercom、transport/NOTICE.md 仅适用各自组件，不自动授权整个项目。
两个入口须协议兼容，bridge 指定权威核心；切换前停止在途变更，保证状态兼容。卸载代码不删除项目数据。

## 收工、恢复与打包

收工是独立于 agent 的**用户权限**。Shift+U 的 close action 在独立 core/plugin.py 中预览、复检，才关闭 pane；调用方执行 pane 最后关闭，Architect 始终保留。
没有 Herdr plugin-action 上下文拒绝执行，不给 agent 增加强制关闭工具。

绑定 run/登记绑定、active/blocked/unknown、身份漂移、缺失/移动/替换/重复成员、管理 tab 内外来 pane、坏/超大状态、过期计划、未决传输/接手、前台工作、background_state_unknown 均失败拒绝，不关闭任何 pane。
协议 22 没有后台/后代进程清单，必须报告能力缺失，不能假定停止。

执行日志：归档 → shutdown_closing → 逐目标核实缺失 → 最终布局/Architect/agent 验证 → 回执/tombstone → 移除登记。
失败保留 shutdown_partial 与剩余名单，不发成功通知；重复关闭只有匹配回执且确认目标缺失才返回 already_closed。

recovery 生成只读协调计划，分类 present_exact/moved_exact/missing/identity_mismatch/replacement/duplicate/unknown，不恢复、改派、重放、关闭或删除。实际执行另需显式授权。

包/manifest 版本统一为 0.1.0-alpha.1。打包使用源码目录及双语文档/词条的明确白名单，保留许可证与第三方归属。
**LICENSE 尚待项目所有者决定；此前不要公开发布。** 切换与回退见[迁移](docs/zh-CN/MIGRATION.md)。
