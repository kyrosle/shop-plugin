# Shop 设置：全局、项目与会话

[English](../en/MODELS.md) · [README](../../README.zh-CN.md)

`/shop-config` 选择 Shop 启动的席位所用的模型和 thinking 档位。Architect 不是 Shop 席位：它沿用你的 Pi 模型，用 `/model` 和 `/thinking` 调整。界面语言另行设置，见[语言](LANGUAGE.md)。

## 席位

| 席位 | 用途 |
| --- | --- |
| **Lead**（`lead`） | 一次 run 唯一的 Lead：拆分 PLAN、派发、审查、汇报 |
| **快速 Worker**（`worker`） | 默认的 Worker：日常任务、批量修改、快速执行 |
| **稳健 Worker**（`worker-2`） | Lead 以 `profile: "steady"` 启动的 Worker，用于复杂、有风险或关键的任务 |

名称表示预期用途，不保证价格或质量；模型由你自己选择。压缩大段交接内容的 curator 分析模型使用快速 Worker 的模型。

## 面板

在 Pi 中执行 `/shop-config`（需要 TUI）。它不会调用模型、启动席位或打开 pane。

- 默认打开 **会话** 作用域；Tab 在 全局 / 项目 / 会话 之间切换。未受信任的项目不能使用项目作用域。
- ↑↓ 选择字段；Enter 打开模型选择器（搜索、模糊匹配、固定 10 行）或 thinking 档位列表。选择只改变草稿。
- "继承父层"删除覆盖；"Pi 默认（显式）"写入 `thinking=null`。
- S 在确认前预览新旧覆盖；R 预览清空当前作用域；Esc 取消。保存只写当前作用域。
- 模型来自当前 Pi 已认证的模型列表；thinking 选项随模型能力变化。

保存后对**下一次** `/shop-go` 生效；已在运行的席位保持原模型。

## 层级与文件

| 作用域 | 存储位置 |
| --- | --- |
| 全局 | `~/.config/shop-workstation/settings.json`（或 `$SHOP_CONFIG_DIR`） |
| 受信任项目 | `<cwd>/.pi/shop.json` |
| 会话 | 当前 Pi 分支上的 `shop-settings` 自定义记录（不是 LLM 消息） |

优先级：**会话 > 项目 > 全局**。同一层内：**席位 > 角色 > defaults**，因此 `worker` 的设置也会作用于 `worker-2`，除非 `worker-2` 单独覆盖。面板只保存与父层不同的字段。

```json
{
  "version": 1,
  "models": {
    "defaults": { "thinking": "medium" },
    "lead": { "model": "provider/mid-model", "thinking": "high" },
    "worker": { "model": "provider/fast-model", "thinking": "low" },
    "seats": { "worker-2": { "model": "provider/strong-model", "thinking": "high" } }
  }
}
```

席位：`lead`、`worker`、`worker-2`。早期 alpha 中的 `lead-2` 会被读取并忽略，下次保存时自动去掉。thinking：`off / minimal / low / medium / high / xhigh / max / null`。

## 一次 run 如何使用设置

`/shop-go` 启动 Lead 时，Architect 一次性解析生效的设置（全局 + 项目 + 它的会话），写入 `<run>/profiles.json`。该 run 的 Lead 和所有 Worker 都使用这份文件，因此在 Architect 中设置的会话覆盖对整个 run 生效，run 进行中修改设置也不会影响它。

如果 Lead、快速 Worker 或稳健 Worker 没有配置模型，run 会带着明确提示拒绝启动。

## 旧版迁移

如果没有 `settings.json`，会读取同目录下旧的 `models.json`。`/shop-config migrate` 会先预览，再写入新的 `settings.json`，并保留 `models.json` 作为备份。

## 并发

设置文件上限 64 KiB。保存时使用配置锁，并对所有父层做内容哈希比对（CAS）。遇到并发修改会拒绝保存，并提示重新打开面板。文件以 0600 权限原子替换。
