import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";
import { getSettingsListTheme } from "@earendil-works/pi-coding-agent";
import { getSupportedThinkingLevels, clampThinkingLevel } from "@earendil-works/pi-ai";
import { Key, matchesKey, SettingsList, truncateToWidth, type Component, type SettingsListTheme } from "@earendil-works/pi-tui";
import { readBridge } from "./bridge.js";
import { configCall, configContext, configRequest, SESSION_CONFIG, SEATS, sessionSettings,
  type ConfigView, type Overrides, type Profiles, type Scope, type Seat, type Thinking,
  ConfigPublisher } from "./configuration.js";

const SCOPES: Scope[] = ["global", "project", "session"];
const SCOPE_LABEL = { global: "全局", project: "目录", session: "会话" };
const SEAT_LABEL = { lead: "主 Lead", "lead-2": "辅助 Lead", worker: "Worker 1", "worker-2": "Worker 2" };
type Result = { type: "cancel" | "save" | "reset" | "scope" } | { type: "edit"; seat: Seat; field: "model" | "thinking" };

/** Uses Pi's SettingsList for navigation/scrolling; outer keys match Curator. */
export function settingsPanel(scope: Scope, target: string, draft: Profiles, view: ConfigView,
  done: (result: Result) => void, requestRender: () => void, availableScopes: Scope[], existingShop: boolean,
  listTheme: SettingsListTheme, selectedId?: string): Component {
  const layer = view.layers[scope];
  const rows = SEATS.flatMap(seat => (["model", "thinking"] as const).map(field => {
    const value = draft[seat][field];
    const changed = value !== layer.profiles[seat][field];
    const source = changed ? "草稿" : layer.sources[seat][field] ?? "内置";
    return { id: `${seat}:${field}`, label: `${SEAT_LABEL[seat]} · ${field === "model" ? "模型" : "思考"}`,
      currentValue: `${value ?? (field === "model" ? "未配置" : "Pi 默认")} ← ${source}` };
  }));
  // Submenu returns control to the command, which opens Pi's model/effort selector.
  const list = new SettingsList(rows.map(row => ({ ...row, submenu: () => {
    const [seat, field] = row.id.split(":") as [Seat, "model" | "thinking"];
    done({ type: "edit", seat, field });
    return { render: () => [], invalidate() {} };
  } })), 8, listTheme, () => {}, () => done({ type: "cancel" }));
  if (selectedId) list.selectItem(selectedId);
  return {
    invalidate() { list.invalidate(); },
    handleInput(data) {
      if (matchesKey(data, Key.tab)) done({ type: "scope" });
      else if (data.toLowerCase() === "s") done({ type: "save" });
      else if (data.toLowerCase() === "r") done({ type: "reset" });
      else if (matchesKey(data, Key.escape) || data.toLowerCase() === "q") done({ type: "cancel" });
      else list.handleInput(data);
      requestRender();
    },
    render(width) {
      const tabs = SCOPES.map(s => !availableScopes.includes(s) ? `${SCOPE_LABEL[s]}×` : s === scope ? `[${SCOPE_LABEL[s]}]` : SCOPE_LABEL[s]).join("  ");
      return ["Shop 设置 · " + tabs, target,
        "Architect：跟随当前 Pi；使用 /model、/thinking 修改", ...list.render(width),
        existingShop ? "已有工位快照不变；保存只影响下次新建工位" : "保存只影响下次新建工位；不会自动开窗",
        "Tab 切层 · Enter 修改 · S 预览保存 · R 重置本层 · Esc 取消"]
        .map(line => truncateToWidth(line, Math.max(0, width)));
    },
  };
}

function inherited(draft: Profiles, parent: Profiles, seat: Seat, field: "model" | "thinking") {
  delete draft[seat][field];
  const value = parent[seat][field];
  if (value !== undefined) Object.assign(draft[seat], { [field]: value });
}

export function validateModels(ctx: ExtensionCommandContext, profiles: Profiles): void {
  const available = ctx.modelRegistry.getAvailable();
  for (const seat of SEATS) {
    const profile = profiles[seat];
    if (!profile.model) continue; // Partial layers may be saved; setup requires all models.
    const model = available.find(m => `${m.provider}/${m.id}` === profile.model);
    if (!model) throw new Error(`${SEAT_LABEL[seat]} 模型当前不可用：${profile.model}`);
    if (profile.thinking != null && !getSupportedThinkingLevels(model).includes(profile.thinking))
      throw new Error(`${SEAT_LABEL[seat]} 不支持 ${profile.thinking}，请重新选择档位`);
  }
}

export async function editConfiguration(pi: ExtensionAPI, ctx: ExtensionCommandContext,
  publisher: ConfigPublisher, args = ""): Promise<void> {
  if (!ctx.hasUI || ctx.mode !== "tui") { ctx.ui.notify("/shop-config 需要 Pi TUI", "warning"); return; }
  if (!ctx.isIdle()) { ctx.ui.notify("等待当前回合结束，再配置 Shop。未保存。", "warning"); return; }
  if (args && args !== "migrate") { ctx.ui.notify("用法：/shop-config [migrate]", "info"); return; }
  const bridge = readBridge();
  if (!bridge) throw new Error("Shop bridge 未配置；先运行 core/plugin.py configure");
  const context = configContext(bridge, ctx);
  const capturedId = ctx.sessionManager.getSessionId();
  const session = sessionSettings(ctx.sessionManager.getBranch(), context.root);
  const signal = publisher.signal;
  const guard = () => {
    if (signal.aborted || ctx.sessionManager.getSessionId() !== capturedId || !ctx.isIdle())
      throw new Error("会话/回合已变化；未保存，请重新打开 /shop-config");
    if (JSON.stringify(readBridge()) !== JSON.stringify(bridge)) throw new Error("Shop bridge 已变化；未保存，请重新打开");
    const current = configContext(bridge, ctx);
    if (JSON.stringify(current) !== JSON.stringify(context)
        || sessionSettings(ctx.sessionManager.getBranch(), current.root).marker !== session.marker)
      throw new Error("项目、信任、分支设置或工位已变化；未保存，请重新打开 /shop-config");
  };
  const base = configRequest(context, session.overrides);
  const view = await configCall<ConfigView>(pi, bridge, { ...base, action: "load" });
  guard();
  if (args === "migrate") {
    const request = { ...base, action: "migrate", revisions: view.revisions };
    const preview = await configCall<{ proposed: unknown; backup: string }>(pi, bridge, request);
    guard();
    if (!await ctx.ui.confirm("迁移旧 models.json？", `${JSON.stringify(preview.proposed, null, 2)}\n旧文件保留：${preview.backup}\n现有工位不变。`, { signal })) return;
    guard();
    await configCall(pi, bridge, { ...request, apply: true });
    ctx.ui.notify("已迁移到 settings.json；旧 models.json 保留，不再双写。", "info");
    await publisher.start(ctx);
    return;
  }
  const scopes = SCOPES.filter(s => s !== "project" || context.trusted);
  const drafts = Object.fromEntries(SCOPES.map(s => [s, structuredClone(view.layers[s].profiles)])) as Record<Scope, Profiles>;
  let scope: Scope = "session";
  const selectedIds: Partial<Record<Scope, string>> = {};
  while (!signal.aborted) {
    guard();
    const draft = drafts[scope];
    const target = scope === "session" ? `session:${capturedId}` : view.paths[scope];
    let detachAbort: (() => void) | undefined;
    let result: Result | undefined;
    try {
      result = await ctx.ui.custom<Result | undefined>((tui, _theme, _keys, done) => {
        let settled = false;
        const finish = (value: Result) => { if (!settled) { settled = true; detachAbort?.(); done(value); } };
        const abort = () => finish({ type: "cancel" });
        detachAbort = () => signal.removeEventListener("abort", abort);
        signal.addEventListener("abort", abort, { once: true });
        if (signal.aborted) abort();
        return settingsPanel(scope, target + (view.legacy ? " · legacy：/shop-config migrate" : ""), draft, view,
          finish, () => tui.requestRender(), scopes, context.existingShop, getSettingsListTheme(), selectedIds[scope]);
      }, { overlay: true, overlayOptions: { width: "85%", maxHeight: "95%", anchor: "center" } });
    } finally { detachAbort?.(); }
    if (!result || result.type === "cancel" || signal.aborted) return;
    guard();
    if (result.type === "scope") { scope = scopes[(scopes.indexOf(scope) + 1) % scopes.length]; continue; }
    if (result.type === "edit") {
      const { seat, field } = result;
      selectedIds[scope] = `${seat}:${field}`;
      const parent = view.layers[scope].parent;
      if (field === "model") {
        const models = ctx.modelRegistry.getAvailable().map(m => `${m.provider}/${m.id}`).sort();
        const inherit = `继承父层：${parent[seat].model ?? "未配置"}`;
        const choice = await ctx.ui.select(SEAT_LABEL[seat] + " 模型", [inherit, ...models], { signal });
        guard();
        if (choice === inherit) inherited(draft, parent, seat, field);
        else if (choice && models.includes(choice)) {
          draft[seat].model = choice;
          const model = ctx.modelRegistry.getAvailable().find(m => `${m.provider}/${m.id}` === choice)!;
          if (draft[seat].thinking != null) draft[seat].thinking = clampThinkingLevel(model, draft[seat].thinking!);
        }
      } else {
        const model = ctx.modelRegistry.getAvailable().find(m => `${m.provider}/${m.id}` === draft[seat].model);
        const levels = model ? getSupportedThinkingLevels(model) : [];
        const inherit = `继承父层：${parent[seat].thinking ?? "Pi 默认"}`;
        const choice = await ctx.ui.select(SEAT_LABEL[seat] + " 思考档位", [inherit, "Pi 默认（显式）", ...levels], { signal });
        guard();
        if (choice === inherit) inherited(draft, parent, seat, field);
        else if (choice === "Pi 默认（显式）") draft[seat].thinking = null;
        else if (choice && levels.includes(choice as Thinking)) draft[seat].thinking = choice as Thinking;
      }
      continue;
    }
    const reset = result.type === "reset";
    if (!reset) validateModels(ctx, draft);
    const request = { ...base, action: "prepare", scope, revisions: view.revisions, draft, reset };
    const prepared = await configCall<{ overrides: Overrides }>(pi, bridge, request);
    guard();
    if (!await ctx.ui.confirm(reset ? "清除本层模型覆盖？" : "保存 Shop 配置？",
      `${SCOPE_LABEL[scope]} · ${target}\n旧覆盖：${JSON.stringify(view.layers[scope].overrides)}\n新覆盖：${JSON.stringify(prepared.overrides)}\n仅下次新建工位生效；现有工位不变。`, { signal })) continue;
    guard();
    if (!reset) validateModels(ctx, draft);
    if (scope === "session") {
      // Recheck file generations after confirmation, then only append metadata.
      const verified = await configCall<{ overrides: Overrides }>(pi, bridge, request);
      guard();
      pi.appendEntry(SESSION_CONFIG, { version: 1, project_root: context.root, overrides: verified.overrides });
    } else {
      await configCall(pi, bridge, { ...request, action: "save" });
    }
    ctx.ui.notify(`${SCOPE_LABEL[scope]}配置已保存；下次新建工位生效，现有工位未修改。`, "info");
    await publisher.start(ctx);
    return;
  }
}
