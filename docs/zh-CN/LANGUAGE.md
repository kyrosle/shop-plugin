# 语言

[English](../en/LANGUAGE.md) · [中文首页](../../README.zh-CN.md)

Shop 支持英文 `en` 与简体中文 `zh-CN`。

## 选择语言

在 Pi 中：

```text
/shop-language
/shop-language en
/shop-language zh-CN
/shop-language auto
```

不带参数打开中英选择器，Esc 取消不写入；明确参数则保存偏好。
命令描述采用静态中英并列；面板、通知使用选定语言。宿主自带按钮和快捷键提示仍由 Pi/Herdr 控制。

无需启动 Pi，也可用 CLI：

```sh
python3 <package>/core/language.py            # 以 JSON 显示保存值与实际语言
python3 <package>/core/language.py zh-CN      # 保存偏好
```

CLI 不查询 Herdr、不启动席位，也不创建 run 状态。

解析顺序：

1. 明确保存的 `en` 或 `zh-CN`。
2. `auto` 依次取首个非空 `LC_ALL`、`LC_MESSAGES`、`LANG`。
3. `zh` 前缀选择简体中文，包括 zh_CN/zh-TW 变体；其他值（含 C、POSIX、不支持的语言）回退英文。

## 存储与边界

个人偏好位于 `~/.config/shop-workstation/language.json`（或 `$SHOP_CONFIG_DIR/language.json`）。

```json
{"version": 1, "language": "auto"}
```

它不是项目/会话模型配置层，不会修改 settings.json、run 的模型配置或运行中的席位。
Pi 与 CLI 共用此文件。写入使用独立锁、内容哈希校验和 0600 原子替换。并发修改会拒绝，而非静默覆盖；先检查并重新打开，不自动重试。
偏好损坏/超大时，显示按系统语言回退；显式读取/保存报告错误，不覆盖坏文件。缺少翻译词条时回退英文。
已经打开的对话框保留其显示选项；切语言后重新打开面板。其他 Pi 在后续显示操作中读取偏好，不需要重启或调用模型。

## 不随语言变化的内容

- 命令/参数、操作 ID、JSON 字段、schema、状态值、错误码、路径、身份。
- 用户输入、Provider/model ID、原始异常、历史证据、JSON 事实视图。可在原始异常外补充本地化解释。
- Agent 回复语言遵循用户要求；界面语言不授权翻译任务内容、源码或证据。

菜单返回稳定操作 ID，不把翻译后的文字当业务操作。两种语言的确认保持相同安全边界，取消始终不执行。

## 文档与验证

[英文首页](../../README.md)与[中文首页](../../README.zh-CN.md)链接 `docs/en/`、`docs/zh-CN/` 成对指南。
席位的角色提示是英文的，不随显示语言变化。

离线测试覆盖词条/占位符一致、回退、Python/TypeScript 共用偏好、并发拒绝、窄屏、文档链接和打包内容。真实宿主测试会在真实 Pi 中保存并切换语言。
