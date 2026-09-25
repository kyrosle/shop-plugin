# 真实宿主回归跑道

[English](../en/TESTING.md)

Python/Bun 离线测试仍然必要，但不能证明 Herdr 的真实返回结构、Pi TUI 与插件生命周期能协同工作。`tests/host/run.py` 提供显式启用、可重复执行的 **真实 Herdr + 真实 Pi** 冒烟门禁。目前还不是完整的工单交付端到端测试。

## 从源码 checkout 运行

前提：macOS；PATH 中已安装 Herdr **>= 0.9.0**（实测 0.9.1）、Pi、Node、Python 3；仓库已执行 `npm ci --ignore-scripts`。跑道不自动安装或升级宿主。Herdr 兼容性由适配器的协议/schema 探测决定；Pi 实际版本写入报告，由真实测试检查兼容性。

```sh
npm run test:host                       # 仅计划/预检，不启动进程
npm run test:host -- --run              # 全部场景，启动隔离的真实进程
npm run test:host -- --run --scenario startup
npm run test:host -- --run --scenario reset
npm run test:host -- --run --scenario lifecycle
```

只有 `--run` 授权启动。缺少前提条件算错误，不静默跳过。`startup`、`reset` 仅用于定位，**不代表发布验收**。必须默认 `all` 通过，才能声称宿主生命周期已验证。

## 隔离与权限边界

每次创建全新的私有 `/tmp/shop-host-*` 目录（macOS 规范化后为 `/private/tmp`），分别容纳：

- 独立 Herdr 配置、Unix socket、headless 服务、插件注册表与 PTY；
- 独立 HOME、XDG 目录、Pi agent 目录、空认证文件和成员会话；
- 独立 Shop bridge、模型/语言配置与状态；
- 临时 Git 项目、测试扩展和日志。

子进程环境使用白名单，不复制用户密钥、代理、agent 上下文、shell 启动覆盖或现有 Shop/Herdr 定位信息。无网络测试 Provider 提供十三个模型目录项，供真实 UI 滚动和搜索；其响应实现不请求远端。本组场景要求 **Provider 调用为零**，连测试 Provider 也不能被误触发。原生推理工具及无关扩展、技能、上下文文件均禁用。

启动前要求私有 socket 不存在、服务明确未运行；新服务的 workspace/plugin 列表必须为空。不接管已有服务，不查询其他 tab。故障场景先执行一次**真实 pane 改名**并保存原始成功响应，再故意破坏该次响应，复现“写入已成功、适配器解析失败”。不伪造宿主成功，也不放宽收工安全检查。

这是配置/进程隔离，**不是操作系统安全沙箱**；只运行可信 checkout。不会复制或修改用户安装、模型凭据、现有工位登记或业务仓库。

## 覆盖范围与限制

| 阶段 | 核验内容 |
| --- | --- |
| 启动 | 原生插件 link；真实 Pi TUI 加载 Shop 命令；私有端点与测试模型 |
| 配置 | 取消不改配置；作用域切换；十三项模型列表的定高滚动和搜索；确认保存；实际 Lead 按保存的模型启动 |
| Architect 保留 | Shop 保存不改 Pi 默认设置；reload 保留 Architect 会话和当前模型 |
| 语言/reload | 原生 slash 命令写入私有语言配置；观察真实 reload 生命周期 |
| 重置 | 真实写入后注入坏响应产生 partial 登记；`/shop-reset` 取消保留原始字节，确认归档原始字节且保留 pane/配置 |
| 开工 | 原生 Herdr 插件动作启动真实 Lead/Worker Pi；登记与现场身份匹配，各自会话独立 |
| 拒绝收工 | 启动真实、有时限、独立 POSIX 会话且无 tty 的子进程；Pi 即使 idle 也必须被阻止收工，登记与所有成员保留 |
| 成功收工 | 仅停止测试子进程后，要求真实 ready 计划、执行 pane/进程退出、Architect 保留、回执正确；重复收工不改变登记、回执及布局 |
| 清理 | 只终止本次直接创建的测试服务，核验观测到的测试 Pi 已退出，保留日志 |

**收工证据：**Herdr protocol 22 仍缺后台列表。Shop 将原生身份/前台信息、本地 Unix socket 的内核对端 PID 与两次稳定 OS 元数据快照交叉核验。仅豁免核实后的直接 Pi 主进程；额外后代、同会话/终端/进程组中的进程仍阻塞。进程实例变化使计划失效，关闭后还须核实原 shell/Pi 已退出。不读取完整命令行参数或环境变量。`background-blocked-plan.json` 与 `shutdown-plan.json` 分别保留负向、正向证据；不得注入 `background_proven=True` 或绕过缺失证据刷绿。

证明范围仅为当前可观测的 **pane 内进程静止状态**，不是所有仓库写入者均已停止。完全 daemonize、重归属并脱离 pane 会话/终端/进程树的独立服务不在范围内；快照检查也不等于原子冻结进程。收工不会宣称这些外部服务已停止，不会结束 run，也不能替代 worktree 集成前的停写核验。远端/未核实对端、不可读或变化中的元数据仍拒绝关闭。本地快照补充目前仅在 macOS 启用；Linux 的 proc 可见性/命名空间及真实宿主跑道未经验证，继续保守返回 `background_state_unknown`。

当前原生 CLI 在登记移除后拒绝重复收工（非零退出），不会返回 `already_closed`。报告明确记录此结果；重复检查仅证明不发生额外修改，不证明成功回执。

绑定 run 的 broker 通信、派单 → 接手 → 交付、worktree 集成、真实模型协作质量、截图视觉对比目前**尚未覆盖**。这是宿主生命周期基础，不是完整模型协作闭环的验收证据。

### 离线生命周期与实际客户端检查

`tests/test_lifecycle.py` 使用真实核心、适配器与 candidate 文件逻辑，只替换原生响应及 OS 进程元数据，覆盖会话/租约/进程实例失效、成员移动/忙碌、未使用工位的孤立登记归档、私有原始字节备份、备份失败和原件变化。`test_setup_adapter.py` 也覆盖早期仅 Architect 失败后的安全归档；split/start 结果不明时仍保留 partial 证据。Workbench 业务测试替换了已核验归属门禁，不独立证明真实生命周期归属。

Bun 命令测试确认预检失败或会话变化时不发送模型请求。`transport_harness.test.ts` 将**实际 ShopTransportClient** 接入隔离的真实 broker，完成消息/回执交换，不运行 Pi、Herdr 或模型，可捕获原始 socket 测试发现不了的客户端漏发 hello。socket 派送/注入不等于业务接受。这些是离线检查，不是换会话、孤立登记清理或 Grok RPC 进程祖先链的真实宿主验收；后者须单独授权运行。

## 证据与清理

每次打印 `Artifacts: <私有目录>`。`report.json` 包含各阶段结果、版本、覆盖边界、Provider 调用次数及清理结果。另保留 `commands.jsonl`、`server.log`、成员观测、私有原生插件日志、重置归档和收工计划。证据含本机路径和终端文字，分享前先检查。

失败或中断仍尝试清理自有资源。不调用全局 `herdr server stop`，不广泛 `pkill`，不隐式 reset，不自动重试写操作。清理失败也算测试失败；人工处理前核对报告中的自有服务 PID、私有 socket 和剩余测试 Pi PID。SIGKILL/机器故障无法保证清理。证据默认保留；检查后仅删除该次打印的精确目录。

## 自动化

`npm test` 包含跑道的离线安全回归，仍不启动真实宿主。独立 **Real host gate** GitHub 工作流采用手动触发（`workflow_dispatch`），要求带 `shop-host-tests` 标签的专用 self-hosted macOS runner，且提前安装宿主二进制。执行默认完整门禁，透传退出码；即使失败，也只上传该次运行证据。使用可丢弃的可信 runner，不在其中运行不可信 PR 代码。

普通 push/PR CI 仍是离线测试。真实宿主工作流未配置或未执行必须标记 **NOT_RUN**，不算通过。完整真实宿主验收必须同时通过后台拒绝与空闲关闭场景，不能只看离线 CI 全绿。
