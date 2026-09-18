# 通过 Git 安装与更新

[English](../en/INSTALLATION.md) · [中文首页](../../README.zh-CN.md)

Shop 必须安装**两个插件入口**。只装 Pi 或只装 Herdr 都不完整。
下面安装的是 Shop，不是 Pi/Herdr 应用本身。插件拥有当前 OS 用户权限；安装前检查所选提交的源码。

## 两端各做什么？

| 组件 | 职责 | 安装入口 |
| --- | --- | --- |
| Pi 扩展 | `/shop-config`、`/shop-ui`、任务委托、配置候选与 transport | `pi install` |
| Herdr 插件 | 开工/收工操作、pane 生命周期事件、原生快捷键 | `herdr plugin install` |
| 共享 Python 核心 | 身份校验、工单、配置与运行状态 | 两份 checkout 都包含，但 **bridge 只指定一个权威副本** |

两端固定到**同一个完整 Git commit**。只看包版本都叫 `0.1.0-alpha.1` 不够。
本文选 **Herdr 管理的 checkout** 作为权威核心。Pi 扩展读取 bridge 后调用它，不要再从 Pi checkout 配一份相互冲突的 bridge。

## 1. 检查前提，保留现有工作

- 当前适配器针对 Herdr **0.9.0**、协议 **22**、schema **1**。CLI 与正在运行的 server 必须兼容。manifest 的最低版本不代表所有更新版本都已支持。
- Pi API 兼容 **0.85.1**；Node **>=22.19.0**、Python **>=3.9**、Git。Bun 仅开发测试需要，普通安装不需要。
- 已配置 Pi Provider 认证及可用模型。保留通用 `@ogulcancelik/pi-herdr` 工具；Shop 不替代它。
- 安装在实际运行 Herdr pane 的主机上；远程快捷键转发另需客户端配置。

```sh
pi --version
herdr --version
herdr plugin list
pi list
```

改动前，在仓库外私有备份 Pi 包设置、Herdr 插件引用/快捷键、已有 Shop bridge/配置/状态。检查托管 checkout 是否存在本地改动：安装器可能重置或替换代码。

活动 run、未知写入者、部分完成操作都会阻止升级。不能用 `idle` 或关闭 pane 推断写入已停止。已有/旧工位按[迁移与回退](MIGRATION.md)处理；不要同时启用新旧角色注入扩展。

## 2. 两端安装同一个已检查提交

将占位符替换为本仓库 GitHub 历史中已检查的完整 commit。两条命令共用一个值，不要分别解析可能变化的分支。

```sh
SHOP_REF='REPLACE_WITH_REVIEWED_FULL_COMMIT'
pi install "git:github.com/kyrosle/shop-plugin@$SHOP_REF"
herdr plugin install kyrosle/shop-plugin --ref "$SHOP_REF" --yes
```

`--yes` 仅接受 Herdr 安装确认，不会开工。Pi 自动安装包依赖；不要在托管安装目录运行 `npm ci`，也不要手改安装文件绕过报错。

Herdr CLI 显示插件身份与配置目录，但可能不显示代码 checkout 路径。按下文从本地登记文件读取 `plugin_root`，不要猜目录后缀，也不要把配置目录当作代码目录。
Pi 默认全局 checkout 在 `~/.pi/agent/git/github.com/kyrosle/shop-plugin`。

## 3. 配共享 bridge——仅首次安装

默认 Herdr profile 可只读查询登记文件；自定义 profile/配置根目录时，改用对应的登记文件路径。先预览再应用：

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

SHOP_CONFIG_DIR="$SHOP_CONFIG" SHOP_STATE_DIR="$SHOP_STATE" \
  python3 "$SHOP_CORE/core/plugin.py" configure
# 核对 core_root、config_dir、state_dir 后再执行：
SHOP_CONFIG_DIR="$SHOP_CONFIG" SHOP_STATE_DIR="$SHOP_STATE" \
  python3 "$SHOP_CORE/core/plugin.py" configure --apply
```

默认 bridge：`~/.config/shop-workstation/bridge.json`。

| 文件/目录 | 用途 |
| --- | --- |
| `bridge.core_root` | Herdr 托管的权威代码 |
| `bridge.config_dir/settings.json` | Shop 全局模型/思考覆盖 |
| `bridge.config_dir/language.json` | 个人界面语言 |
| `bridge.state_dir` | 登记、配置候选、transport 与运行证据 |
| `~/.pi/agent/settings.json` | Pi 自身设置与包引用，**不是** Shop 模型设置 |

配置和状态必须放在两个代码 checkout 之外。不要把 Provider 凭据写进 bridge 或 Shop 配置。
若自定义 `SHOP_LOCATOR`，两个宿主必须一致；避免其环境中存在相互冲突的 `SHOP_CONFIG_DIR`/`SHOP_STATE_DIR` 覆盖。

**已有 bridge？先停，不运行 `--apply`。** 它会刻意拒绝覆盖。普通升级且路径不变时直接保留；若 `plugin_root` 改变，须备份后显式切换并保留原配置/状态路径，不删除 bridge 绕过检查。

## 4. Reload Pi，选择模型，配置快捷键

在已有的 **Herdr 内 Pi 会话**中执行：

```text
/reload
/shop-language zh-CN
/shop-config
```

语言命令可选，也支持 `en`、`auto`。`/shop-config` 默认打开 Session 层；按 Tab 切到 Global 可保存通用默认值。为四个席位（`lead`、`lead-2`、`worker`、`worker-2`）选择模型和支持的思考档位，按 S 预览并确认。尚未启动的辅助席位也必须能解析出模型。详见[模型配置](MODELS.md)。

Architect 保留当前 Pi 模型/会话；保存设置不会切换运行中的成员。加载、配置不会调用模型或打开执行 pane。

先备份 Herdr `config.toml`，确认无冲突后添加。旧快捷键冲突时替换，不要重复追加：

```toml
[[keys.command]]
key = "prefix+u"
type = "plugin_action"
command = "shop.workstation.open"
description = "开工"

[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "shop.workstation.close"
description = "收工预览与检查"
```

```sh
herdr config check
herdr server reload-config
```

默认前缀下，按 Ctrl+B，松开，再按 U 开工。**这一步才会启动新的 Lead/Worker Pi 进程**；准备好后，从单个未缩放 Pi pane 执行，窗口至少 140 列 × 40 行。安装本身不会执行开工。之后使用 `/shop-ui`。
Ctrl+B 后按 Shift+U 是检查后收工，不是强制关闭；后台写入安全性未知时可能拒绝。

## 5. 不启动成员的验证

```sh
git -C "$HOME/.pi/agent/git/github.com/kyrosle/shop-plugin" rev-parse HEAD
git -C "$SHOP_CORE" rev-parse HEAD
herdr plugin list
herdr plugin action list --plugin shop.workstation
python3 "$SHOP_CORE/core/plugin.py" doctor
```

两个 HEAD 与安装引用都应等于 `SHOP_REF`，检查 doctor 输出是否包含错误。Doctor 检查环境/server 可用性，**不证明每种 agent 返回结构、Provider 权限或完整业务流程都正常**。
`/reload` 或保存 `/shop-config` 后不应再出现“开工配置未发布”警告。要单独检查身份查询，可在原 Pi 会话中使用 `!` shell 命令，替换实际路径：

```text
!python3 /absolute/plugin_root/core/configuration.py --request '{"action":"identify"}'
```

这是只读身份查询。普通 shell pane 不是 Pi 身份，不能替代此检查；不要为测试在已占用 pane 内再启动一个 Pi。

## 更新与回退

1. 安全处理完 Shop 工作，清点绑定、写入者和部分操作；备份引用、bridge、配置、状态。不要升级活动执行成员。
2. 选定新完整 commit，重复执行**两端**安装命令。固定引用不会自动追踪 `main`；单独 `pi update` 不会协调 Herdr。
3. 检查新 `plugin_root`。路径未变则保留 bridge；路径改变则显式切换，保持配置/状态目录不变。不要对现有 bridge 重跑首次安装的 `configure --apply`。
4. 核对引用、doctor 和身份查询，再对受影响 Pi 会话 `/reload`、重新打开 `/shop-config`。不覆盖模型/语言文件，不重复追加快捷键。
5. 再单独授权真实工位试用。安装验证不等于任务交付验收。

回退须确认写入者停止，再安装上一对**匹配引用**、恢复兼容的 bridge/配置并 reload。保留状态与证据；卸载不代表进程停止。见[迁移](MIGRATION.md)。

## 常见问题

| 现象 | 检查/处理 |
| --- | --- |
| 没有 `/shop-config` | Pi 入口安装并启用了吗？项目/全局是否有重复来源？是否在 Herdr 内运行 Pi？执行 `/reload`。 |
| U 无反应 | Herdr 插件启用、快捷键无冲突、原生配置已 reload？检查插件日志。 |
| `unknown field 'screen_detection_skipped'` | 旧 Shop 适配器漏接 Herdr 可选布尔字段。两端升级到含修复的提交，保留 bridge/配置，再 `/reload`；不要删除身份校验。 |
| `Another Shop core owns bridge` | 命令用了错误 checkout。使用 `bridge.core_root`，不是哪个副本最后执行就让谁接管。 |
| `Existing bridge: inspect and back up before switching cores; no automatic overwrite` | 对已有安装用了首次配置命令。保留 bridge 或规划显式切换。 |
| 配置候选缺失/过期 | 在原 Architect `/reload` 或保存 `/shop-config`；先检查前面的身份/配置错误。不手写候选，不绕过时效校验。 |
| 收工报 `background_state_unknown` | 安全能力缺口，不是强制关闭或删除登记的许可；检查前台/后台工作与恢复计划。 |
