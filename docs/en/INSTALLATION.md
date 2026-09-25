# Installation

[简体中文](../zh-CN/INSTALLATION.md) · [README](../../README.md)

Shop is a single Pi package. There is no Herdr plugin and no bridge file.

## Prerequisites

- Herdr **≥ 0.9.0** (tested 0.9.1); Pi (tested 0.86.1); Node **≥ 22.19**; Python **≥ 3.9**; Git.
- Pi providers, credentials and models already configured. Shop ships no credentials or model choices.
- Only macOS has been tested. Linux is untested; Windows is unsupported.

Extensions run with your OS user permissions, not in a sandbox. Review the source before installing.

## Install

Pin a reviewed commit:

```sh
SHOP_REF='<full commit sha>'
pi install "git:github.com/kyrosle/shop-plugin@$SHOP_REF"
```

Pi clones the repository and runs `npm install`, which downloads the pinned `pi-context-curator` tarball over HTTPS (no SSH key needed). Then run `/reload` in Pi and check:

```text
/shop-config
```

The settings panel should open. Inside a Herdr pane, `/shop-go` and `/shop-spec` are also available; outside Herdr only `/shop-config` and `/shop-language` are registered, because seats open Herdr panes.

Installing never starts a seat and never changes Architect's model.

## Update and remove

```sh
pi install "git:github.com/kyrosle/shop-plugin@<new sha>"   # move to a new pinned commit
pi remove "git:github.com/kyrosle/shop-plugin"               # uninstall
```

Removing the package does not delete settings (`~/.config/shop-workstation/`) or run records (`<project>/.shop/seats/`).

## Upgrading from the workstation alpha

The earlier alpha installed a Herdr plugin (`shop.workstation`), a bridge and Ctrl+B → U shortcuts. None of them are used any more.

1. Close any Lead/Worker panes the old workstation opened.
2. Copy your model settings before removing the plugin, because they lived in the Herdr plugin's config directory:

   ```sh
   mkdir -p ~/.config/shop-workstation
   cp "$(herdr plugin config-dir shop.workstation)/settings.json" ~/.config/shop-workstation/settings.json
   ```

   The old auxiliary Lead (`lead-2`) is read and ignored. Save once in `/shop-config` to drop it from the file.
3. Remove the plugin and its shortcuts: `herdr plugin uninstall shop.workstation`, delete the `shop.workstation.open` / `shop.workstation.close` entries from Herdr's `config.toml`, then `herdr config check` and `herdr server reload-config`.
4. Update the Pi package to a commit with ephemeral seats (above) and `/reload`.
5. Optional cleanup, after you are sure you no longer need them: `~/.config/shop-workstation/bridge.json`, `~/.local/state/shop-workstation/` (old registrations, member sessions, evidence) and each project's old `.shop/runs/`.

## Settings and data locations

| What | Where |
| --- | --- |
| Global settings, language | `~/.config/shop-workstation/` or `SHOP_CONFIG_DIR` |
| Trusted-project settings | `<project>/.pi/shop.json` |
| Session settings | An entry on the current Pi branch |
| Run records and seat sessions | `<project>/.shop/seats/<run>/`; `/.shop/` is added to `.git/info/exclude` |
