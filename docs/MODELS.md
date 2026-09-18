# Shop 配置：全局、目录、会话

本文介绍 `/shop-config` 的配置层、继承、保存与迁移。当前空闲席位的显式配置申请见[工作台指南](WORKBENCH.md)。

## Pi 设置入口

在 Herdr 内的 Pi 执行 `/shop-config`。需要已配置 bridge 和 Pi TUI；不调用模型、不派工、不自动开窗。

- 默认打开 **会话** 层；Tab 切换全局 / 目录 / 会话。
- ↑↓ 选择字段，Enter 打开模型或思考档位列表。
- 模型来自当前 Pi 已认证可用列表；思考选项跟随模型能力。
- “继承父层”清除覆盖；“Pi 默认（显式）”写 thinking=null，覆盖父层显式档位。
- S 预览旧/新覆盖，确认保存；R 预览清除本层模型组，确认重置；Esc 取消。
- 切层保留草稿，但一次只保存当前层；未保存的其他层不会写入。
- 显示每个字段来源、目标路径/session，以及下次新建工位生效提示。
- 未信任当前目录时目录层禁用。非 Architect 席位可以编辑设置，但其 session 配置不会修改当前 Shop。

Architect 保留原 Pi 模型和会话；用 Pi 原生 `/model`、`/thinking` 选择。
保存 `/shop-config` 不远程热切换成员、不重启 Pi，也不修改 Pi 全局默认值。
第一版只开放模型组；未来协作、巡检等配置可以增加独立分组，不开放绕过身份/停写检查的开关。

## 配置层与文件

| 层 | 存储 |
| --- | --- |
| 全局 | `<bridge.config_dir>/settings.json` |
| 受信任目录 | `<Architect 项目根>/.pi/shop.json` |
| 会话 | Pi 当前分支的 `shop-settings` custom entry |
| 执行快照 | Shop runtime 登记的 `model_profiles`，不是动态继承层 |

无工位时项目根是当前 Pi cwd；有登记时使用登记 cwd，不随 Worker worktree 改变。
Pi 分发版使用其公共 CONFIG_DIR_NAME 替代 `.pi`。当前 Pi cwd 与登记根不同时，不借用另一目录的信任决定。
会话记录附带 project_root；不同根不沿用，不扫描会话 JSONL。custom entry 不进入 LLM message。

优先级：**会话 > 目录 > 全局 > 内置默认**，按字段覆盖。
每层内部先按 **席位 > 角色 > defaults** 展开；所以会话层的 worker 角色配置能覆盖全局层 worker-2 的同字段。
UI 只保存与父层不同的席位字段；未覆盖字段继续继承。保存/重置保留模型组之外的高级字段。
内置不指定模型品牌；未指定 thinking 时由 Pi 决定。

文件格式（替换 provider/model 占位符）：

```json
{
  "version": 1,
  "models": {
    "defaults": { "thinking": "medium" },
    "lead": { "model": "provider/model-a", "thinking": "high" },
    "worker": { "model": "provider/model-b" },
    "seats": {
      "lead-2": { "model": "provider/model-c", "thinking": "high" },
      "worker-2": { "model": "provider/model-d", "thinking": "low" }
    }
  }
}
```

可用席位：lead、lead-2、worker、worker-2。模型字段最多 512 字符。
Thinking：off / minimal / low / medium / high / xhigh / max / null。
推荐分开写 model 与 thinking，避免在模型 ID 后附加 thinking 简写造成继承歧义。
可以先保存不完整配置；**开工时四个席位都必须解析到模型**，包括尚未启动的辅助席位。
Python 校验配置语法；UI 另校验当前模型可用性与档位。其他 Pi 的 credentials/catalog 可能不同，实际启动仍由接收 Pi 验证。

## 保存不等于修改正在运行的成员

```text
内置默认 → 全局 → 受信任目录 → Architect 会话
                                 ↓ 新建工位
                         固定 model_profiles
                                 ↓ 启动成员
                         launch_profile
```

- 全局/目录/会话保存只影响后续新建工位。
- 扩员、恢复继续使用当前工位的固定快照；修改文件不改变在途成员。
- `launch_profile` 是请求启动参数，不是实时模型观测；手动在 Pi 切模型不会回写它。
- `/new`、`/resume`、`/tree`、reload 不会修改现有工位或重启任何成员。
- 单独的 `/shop-ui` → 空闲席位配置可发起明确申请：目标 Pi 用户再确认，公共 API 应用后可选择仅会话生效，或更新该 Shop 对应席位快照；不改配置层、不覆写历史启动参数。详见 [工作台边界](WORKBENCH.md#单席位模型申请)。
- Architect 不接受此申请，继续使用原生 `/model`、`/thinking`。

## 快捷键如何取得会话配置

Pi 扩展发布有限候选记录到 `<state_dir>/config-candidates/`，每 5 秒刷新，仅含配置与实例元数据，不含聊天、凭据或完整会话路径。
记录绑定 socket/tab/pane/terminal、session、PID、扩展实例、项目根、信任和会话覆盖。

Herdr 开工读取原 Architect 对应记录，核对实例、进程存在和 20 秒时效，重新读取磁盘层，再固定解析结果与来源。
没有有效记录时明确拒绝，不默默只用全局值。先在原 Architect `/reload` 或 `/shop-config`，再按开工快捷键。
关闭/reload/session 切换清除本实例候选；进程崩溃后旧记录不能依靠存活时间或同名实例自动接管。
这是本机同用户协作协议，不是恶意代码安全边界，也不能原子冻结另一个 pane 的人工操作。

## 旧配置迁移

旧 `<config_dir>/models.json` 仍可在没有 settings.json 时作为只读兼容来源，面板显示 legacy。
执行 `/shop-config migrate`，审查导入预览后确认：

- 只创建新的 settings.json，不覆盖已有新版文件。
- 原 models.json 保留为备份，迁移后不再读取或双写它。
- 不修改当前工位、票据、会话、worktree。
- 有 legacy 时直接保存全局层会提示先迁移；会话/目录层仍可按显式覆盖使用。

旧登记如果没有 model_profiles，在下一次显式启动/恢复时固定全局兼容配置；不会伪称读取到了旧 Architect session 的配置。

## 显式文件入口

保留仅供 **新** 工位的 CLI 入口，明确绕过目录/会话配置：

```sh
/absolute/package/bin/herdr-shop setup --models-file /absolute/models.json --dry-run
/absolute/package/bin/herdr-shop setup --models-file /absolute/models.json
```

文件是旧式模型组格式（见 `config/models.example.json`），不是外层带 version/models 的 settings.json。
不与默认配置合并。现有工位和非 setup 命令拒绝 --models-file。

## 并发与验收边界

配置 <=64 KiB；同一 bridge/config_dir 下的参与者使用配置锁和文件内容哈希 CAS（含父层）。
并发修改时拒绝保存并要求重开；不会无声最后写入者覆盖。配置文件 0600 原子替换。
预览/会话保存只可能创建全局配置锁，不往业务 worktree 写锁文件。
原始 shell/外部编辑器不参与该锁；不是 OS 权限沙箱。

自动测试覆盖三层优先级、继承/重置、CAS、迁移、UI/分支生命周期、候选身份和跨语言消费。
真实 Pi/Herdr 快捷键、Provider 权限及手动 session 切换竞态仍需独立受控现场验收。
