import { selectAction, t } from "./i18n.js";
import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";
import { getSupportedThinkingLevels, clampThinkingLevel } from "@earendil-works/pi-ai";
import { Key, matchesKey, SettingsList, type Component, type SettingsListTheme } from "@earendil-works/pi-tui";
import { panelFrame, type FrameTheme } from "./ui-frame.js";
import { selectModel } from "./model-picker.js";
import { readBridge } from "./bridge.js";
import { configCall, configContext, configRequest, SESSION_CONFIG, SEATS, sessionSettings,
  type ConfigView, type Overrides, type Profiles, type Scope, type Seat, type Thinking,
  ConfigPublisher } from "./configuration.js";

const SCOPES: Scope[] = ["global", "project", "session"];
const scopeLabels = () => ({ global: t("Global"), project: t("Project"), session: t("Session") });
const seatLabels = () => ({ lead: t("Primary Lead"), "lead-2": t("Auxiliary Lead"),
  worker: t("Fast Worker (low cost)"), "worker-2": t("Steady Worker (reliable)") });
type Result = { type: "cancel" | "save" | "reset" | "scope" } | { type: "edit"; seat: Seat; field: "model" | "thinking" };

/** Uses Pi's SettingsList for navigation/scrolling; outer keys match Curator. */
export function settingsPanel(scope: Scope, target: string, draft: Profiles, view: ConfigView,
  done: (result: Result) => void, requestRender: () => void, availableScopes: Scope[], existingShop: boolean,
  listTheme: SettingsListTheme, selectedId?: string, theme: FrameTheme = { fg: (_color, text) => text }): Component {
  const layer = view.layers[scope];
  const rows = SEATS.flatMap(seat => (["model", "thinking"] as const).map(field => {
    const value = draft[seat][field];
    const changed = value !== layer.profiles[seat][field];
    const source = changed ? t("Draft") : layer.sources[seat][field] ?? t("Built-in");
    const sourceLabel = SCOPES.includes(source as Scope) ? scopeLabels()[source as Scope] : source;
    return { id: `${seat}:${field}`, label: `${seatLabels()[seat]} · ${field === "model" ? t("Model") : t("Thinking")}`,
      description: seat === "worker" ? t("Routine tasks, batch edits and fast execution; choose the model yourself.")
        : seat === "worker-2" ? t("Complex implementation, difficult fixes and critical changes; choose the model yourself.") : undefined,
      currentValue: `${value ?? (field === "model" ? t("Not configured") : t("Pi default"))} ← ${sourceLabel}` };
  }));
  // Submenu returns control to the command, which opens the draft model/effort picker.
  const list = new SettingsList(rows.map(row => ({ ...row, submenu: () => {
    const [seat, field] = row.id.split(":") as [Seat, "model" | "thinking"];
    done({ type: "edit", seat, field });
    return { render: () => [], invalidate() {} };
  } })), 8, { ...listTheme, hint: () => "" }, () => {}, () => done({ type: "cancel" }));
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
      const tabs = SCOPES.map(s => !availableScopes.includes(s) ? theme.fg("dim", `${scopeLabels()[s]}×`)
        : s === scope ? theme.fg("accent", `[${scopeLabels()[s]}]`) : scopeLabels()[s]).join("  ");
      return panelFrame(t("Shop settings"), [t("Scope: {0}", [tabs]), theme.fg("dim", target),
        theme.fg("muted", t("Architect: current Pi; change with /model and /thinking")), "",
        ...list.render(Math.max(4, width - 4)).filter(line => line.trim()), "",
        theme.fg("muted", existingShop ? t("Existing Shop snapshots stay unchanged; saves affect new Shops only") : t("Saves affect new Shops only; no panes are opened automatically")),
        theme.fg("dim", t("Tab scope · Enter edit · S preview save · R reset scope · Esc cancel"))], width, theme);
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
    if (!model) throw new Error(t("{0} model currently unavailable: {1}", [seatLabels()[seat], profile.model]));
    if (profile.thinking != null && !getSupportedThinkingLevels(model).includes(profile.thinking))
      throw new Error(t("{0} does not support {1}; select another thinking level", [seatLabels()[seat], profile.thinking]));
  }
}

export async function editConfiguration(pi: ExtensionAPI, ctx: ExtensionCommandContext,
  publisher: ConfigPublisher, args = ""): Promise<void> {
  if (!ctx.hasUI || ctx.mode !== "tui") { ctx.ui.notify(t("/shop-config requires Pi TUI"), "warning"); return; }
  if (!ctx.isIdle()) { ctx.ui.notify(t("Wait for the current turn to finish before configuring Shop. Nothing saved."), "warning"); return; }
  if (args && args !== "migrate") { ctx.ui.notify(t("Usage: /shop-config [migrate]"), "info"); return; }
  const bridge = readBridge();
  if (!bridge) throw new Error(t("Shop bridge unconfigured; run core/plugin.py configure first"));
  const context = configContext(bridge, ctx);
  const capturedId = ctx.sessionManager.getSessionId();
  const session = sessionSettings(ctx.sessionManager.getBranch(), context.root);
  const signal = publisher.signal;
  const guard = () => {
    if (signal.aborted || ctx.sessionManager.getSessionId() !== capturedId || !ctx.isIdle())
      throw new Error(t("Session/turn changed; nothing saved. Reopen /shop-config"));
    if (JSON.stringify(readBridge()) !== JSON.stringify(bridge)) throw new Error(t("Shop bridge changed; nothing saved. Reopen the panel"));
    const current = configContext(bridge, ctx);
    if (JSON.stringify(current) !== JSON.stringify(context)
        || sessionSettings(ctx.sessionManager.getBranch(), current.root).marker !== session.marker)
      throw new Error(t("Project, trust, branch settings or Shop changed; nothing saved. Reopen /shop-config"));
  };
  const base = configRequest(context, session.overrides);
  const view = await configCall<ConfigView>(pi, bridge, { ...base, action: "load" });
  guard();
  if (args === "migrate") {
    const request = { ...base, action: "migrate", revisions: view.revisions };
    const preview = await configCall<{ proposed: unknown; backup: string }>(pi, bridge, request);
    guard();
    if (!await ctx.ui.confirm(t("Migrate legacy models.json?"), t("{0}\nOriginal file retained: {1}\nExisting Shops stay unchanged.", [JSON.stringify(preview.proposed, null, 2), preview.backup]), { signal })) return;
    guard();
    await configCall(pi, bridge, { ...request, apply: true });
    ctx.ui.notify(t("Migrated to settings.json; legacy models.json retained, no further dual writes."), "info");
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
      result = await ctx.ui.custom<Result | undefined>((tui, theme, _keys, done) => {
        let settled = false;
        const finish = (value: Result) => { if (!settled) { settled = true; detachAbort?.(); done(value); } };
        const abort = () => finish({ type: "cancel" });
        detachAbort = () => signal.removeEventListener("abort", abort);
        signal.addEventListener("abort", abort, { once: true });
        if (signal.aborted) abort();
        return settingsPanel(scope, target + (view.legacy ? " · legacy：/shop-config migrate" : ""), draft, view,
          finish, () => tui.requestRender(), scopes, context.existingShop, {
            label: (text, selected) => theme.fg(selected ? "accent" : "text", text),
            value: (text, selected) => theme.fg(selected ? "accent" : "muted", text),
            description: text => theme.fg("muted", text), hint: text => theme.fg("dim", text), cursor: "› ",
          }, selectedIds[scope], theme);
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
        const models = ctx.modelRegistry.getAvailable().map(m => `${m.provider}/${m.id}`);
        const choice = await selectModel(ctx, { title: seatLabels()[seat] + t(" model"),
          models: ctx.modelRegistry.getAvailable(), current: draft[seat].model, parent: parent[seat].model }, signal);
        guard();
        if (choice === "inherit") inherited(draft, parent, seat, field);
        else if (choice?.startsWith("model:") && models.includes(choice.slice(6))) {
          const model = ctx.modelRegistry.getAvailable().find(m => `${m.provider}/${m.id}` === choice.slice(6));
          if (!model) throw new Error(t("{0} model currently unavailable: {1}", [seatLabels()[seat], choice.slice(6)]));
          draft[seat].model = choice.slice(6);
          if (draft[seat].thinking != null) draft[seat].thinking = clampThinkingLevel(model, draft[seat].thinking!);
        }
      } else {
        const model = ctx.modelRegistry.getAvailable().find(m => `${m.provider}/${m.id}` === draft[seat].model);
        const levels = model ? getSupportedThinkingLevels(model) : [];
        const inherit = t("Inherit parent: {0}", [parent[seat].thinking ?? t("Pi default")]);
        const choice = await selectAction(ctx, seatLabels()[seat] + t(" thinking level"),
          [["inherit", inherit], ["default", t("Pi default (explicit)")], ...levels.map(id => [id, id] as const)], { signal });
        guard();
        if (choice === "inherit") inherited(draft, parent, seat, field);
        else if (choice === "default") draft[seat].thinking = null;
        else if (choice && levels.includes(choice as Thinking)) draft[seat].thinking = choice as Thinking;
      }
      continue;
    }
    const reset = result.type === "reset";
    if (!reset) validateModels(ctx, draft);
    const request = { ...base, action: "prepare", scope, revisions: view.revisions, draft, reset };
    const prepared = await configCall<{ overrides: Overrides }>(pi, bridge, request);
    guard();
    if (!await ctx.ui.confirm(reset ? t("Clear model overrides in this scope?") : t("Save Shop configuration?"),
      t("{0} · {1}\nOld overrides: {2}\nNew overrides: {3}\nAffects new Shops only; existing Shops stay unchanged.", [scopeLabels()[scope], target, JSON.stringify(view.layers[scope].overrides), JSON.stringify(prepared.overrides)]), { signal })) continue;
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
    ctx.ui.notify(t("{0} settings saved; applies to new Shops. Existing Shops unchanged.", [scopeLabels()[scope]]), "info");
    await publisher.start(ctx);
    return;
  }
}
