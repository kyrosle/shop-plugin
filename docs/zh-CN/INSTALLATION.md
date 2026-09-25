# 安装

[English](../en/INSTALLATION.md) · [README](../../README.zh-CN.md)

Shop 是一个单独的 Pi 包。没有 Herdr 插件，也没有 bridge 文件。

## 前提

- Herdr **≥ 0.9.0**（实测 0.9.1）；Pi（实测 0.86.1）；Node **≥ 22.19**；Python **≥ 3.9**；Git。
- 已配置好 Pi 的 provider、凭据和模型。Shop 不附带任何凭据或模型选择。
- 目前只在 macOS 上测试过。Linux 未测试；不支持 Windows。

扩展以你的系统用户权限运行，没有沙箱。安装前请先审阅源码。

## 安装

锁定到一个审阅过的提交：

```sh
SHOP_REF='<完整 commit sha>'
pi install "git:github.com/kyrosle/shop-plugin@$SHOP_REF"
```

Pi 会克隆仓库并运行 `npm install`，通过 HTTPS 下载锁定版本的 `pi-context-curator` 压缩包（不需要 SSH key）。然后在 Pi 中执行 `/reload`，并检查：

```text
/shop-config
```

应当打开设置面板。在 Herdr pane 中还能看到 `/shop-go` 和 `/shop-spec`；在 Herdr 之外只注册 `/shop-config` 和 `/shop-language`，因为席位需要打开 Herdr pane。

安装不会启动任何席位，也不会改动 Architect 的模型。

## 更新与卸载

```sh
pi install "git:github.com/kyrosle/shop-plugin@<新的 sha>"   # 切换到新的锁定提交
pi remove "git:github.com/kyrosle/shop-plugin"               # 卸载
```

卸载不会删除设置（`~/.config/shop-workstation/`）或 run 记录（`<项目>/.shop/seats/`）。

## 从工作台 alpha 升级

早期 alpha 安装了 Herdr 插件（`shop.workstation`）、bridge 和 Ctrl+B → U 快捷键，现在都不再使用。

1. 关闭旧工作台打开的 Lead/Worker pane。
2. 在卸载插件之前先复制模型设置，因为它们存放在 Herdr 插件的配置目录里：

   ```sh
   mkdir -p ~/.config/shop-workstation
   cp "$(herdr plugin config-dir shop.workstation)/settings.json" ~/.config/shop-workstation/settings.json
   ```

   旧的辅助 Lead（`lead-2`）会被读取并忽略；在 `/shop-config` 里保存一次即可把它从文件中去掉。
3. 卸载插件和快捷键：`herdr plugin uninstall shop.workstation`，从 Herdr 的 `config.toml` 删除 `shop.workstation.open` / `shop.workstation.close` 两项，再执行 `herdr config check` 和 `herdr server reload-config`。
4. 按上文把 Pi 包更新到带一次性席位的提交，然后 `/reload`。
5. 可选清理（确认不再需要后）：`~/.config/shop-workstation/bridge.json`、`~/.local/state/shop-workstation/`（旧登记、成员 session、证据），以及各项目中旧的 `.shop/runs/`。

## 设置与数据位置

| 内容 | 位置 |
| --- | --- |
| 全局设置、语言 | `~/.config/shop-workstation/` 或 `SHOP_CONFIG_DIR` |
| 受信任项目设置 | `<项目>/.pi/shop.json` |
| 会话设置 | 当前 Pi 分支上的一条记录 |
| run 记录与席位 session | `<项目>/.shop/seats/<run>/`；`/.shop/` 会加入 `.git/info/exclude` |
