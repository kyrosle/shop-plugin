import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { join } from "node:path";
import { readBridge } from "./bridge.js";
import { t } from "./i18n.js";

type ResetPlan = {
  schema: number; decision: "empty" | "blocked" | "ready" | "archived"; reason?: string;
  token?: string; tab: string; pane: string; original_error?: string;
  recorded_name?: string; observed_name?: string | null; terminal_id?: string; archive?: string;
};
const reasons = () => ({
  invalid_registration: t("Registration does not match this Architect/tab; inspect identity before recovery."),
  not_early_setup: t("Only early failed setup without members or a bound run can be reset. Use safe shutdown/recovery for other Shops."),
  unknown_setup_stage: t("Setup may already have created panes. Inspect live panes and use recovery; reset refused."),
  binding_conflict: t("Project binding evidence conflicts with reset. Inspect task bindings; nothing removed."),
  identity_changed: t("Architect pane, terminal, name or project changed. Reset cannot adopt a different agent."),
  extra_panes: t("Other panes exist in this tab. Reset will not close them; inspect recovery."),
  other_members: t("Other Shop members are still present, possibly in another tab. Reset refused."),
});

/** User command only: no reset tool, force flag, model call or automatic reopen. */
export function registerResetCommand(pi: ExtensionAPI): void {
  let generation = 0;
  let busy = false;
  const invalidate = () => { generation++; };
  pi.on("session_start", invalidate);
  pi.on("session_shutdown", invalidate);
  pi.on("session_tree", invalidate);
  pi.registerCommand("shop-reset", {
    description: "Reset failed setup / 预览并确认归档当前 tab 的早期失败登记；保留 Pi 和配置",
    handler: async (args, ctx) => {
      if (args.trim()) { ctx.ui.notify(t("Usage: /shop-reset (interactive preview and confirmation only)"), "warning"); return; }
      if (!ctx.hasUI || ctx.mode !== "tui") { ctx.ui.notify(t("/shop-reset requires Pi TUI"), "warning"); return; }
      if (!ctx.isIdle() || busy) { ctx.ui.notify(t("Wait for the current turn or reset command to finish. Nothing reset."), "warning"); return; }
      busy = true;
      try {
        const bridge = readBridge();
        if (!bridge) throw new Error(t("Shop bridge unconfigured; run core/plugin.py configure first"));
        const session = ctx.sessionManager.getSessionId(), epoch = generation, cwd = ctx.cwd;
        const context = () => JSON.stringify([process.env.HERDR_SOCKET_PATH, process.env.HERDR_TAB_ID, process.env.HERDR_PANE_ID]);
        const identity = context();
        const guard = () => {
          if (generation !== epoch || ctx.sessionManager.getSessionId() !== session || ctx.cwd !== cwd
              || !ctx.isIdle() || context() !== identity || JSON.stringify(readBridge()) !== JSON.stringify(bridge))
            throw new Error(t("Session, pane or bridge changed. Stop and inspect reset state; do not retry automatically."));
        };
        const call = async (request: Record<string, unknown>): Promise<ResetPlan> => {
          guard();
          const result = await pi.exec("python3", [join(bridge.core_root, "core/reset.py"), "--request",
            JSON.stringify({ ...request, expected_state_dir: bridge.state_dir, expected_config_dir: bridge.config_dir })], { timeout: 15000 });
          guard();
          if (result.code !== 0 || result.killed)
            throw new Error(t("Reset failed or outcome unknown. Inspect before retry.") + "\n" + (result.stderr || result.stdout).slice(0, 4000));
          if (Buffer.byteLength(result.stdout) > 65536) throw new Error(t("Invalid reset response; inspect state before retry."));
          const data = JSON.parse(result.stdout) as ResetPlan;
          if (data.schema !== 1 || !["empty", "blocked", "ready", "archived"].includes(data.decision)
              || data.tab !== process.env.HERDR_TAB_ID || data.pane !== process.env.HERDR_PANE_ID)
            throw new Error(t("Invalid reset response; inspect state before retry."));
          return data;
        };
        const plan = await call({ action: "preview" });
        if (plan.decision === "empty") { ctx.ui.notify(t("No Shop registration in this tab. Nothing to reset."), "info"); return; }
        if (plan.decision === "blocked") {
          ctx.ui.notify((reasons()[plan.reason as keyof ReturnType<typeof reasons>] ?? t("Reset blocked; inspect registration and live panes."))
            + (plan.original_error ? "\n" + t("Original setup error: {0}", [plan.original_error]) : ""), "warning");
          return;
        }
        if (plan.decision !== "ready" || !/^[a-f0-9]{64}$/.test(plan.token ?? ""))
          throw new Error(t("Invalid reset response; inspect state before retry."));
        const details = [t("Tab: {0} · Keep Pi pane: {1}", [plan.tab, plan.pane]),
          t("Registered name: {0} → Current name: {1}", [plan.recorded_name ?? "?", plan.observed_name ?? t("Unnamed")]),
          t("Original setup error: {0}", [(plan.original_error ?? "?").replace(/[\r\n\t]/g, " ").slice(0, 240)
            + ((plan.original_error?.length ?? 0) > 240 ? "…" : "")]),
          t("Archive this early failed registration only. Close 0 panes; start 0 members. Models, tickets, worktrees and other tabs stay unchanged."),
          t("A missing display name may be acknowledged only when the original pane and terminal still match. No automatic setup follows.")].join("\n\n");
        if (!await ctx.ui.confirm(t("Archive failed setup in this tab?"), details)) return;
        guard();
        const result = await call({ action: "apply", confirmed: true, token: plan.token });
        if (result.decision !== "archived" || !result.archive) throw new Error(t("Invalid reset response; inspect state before retry."));
        ctx.ui.notify(t("Failed registration archived: {0}. Pi and configuration preserved. Start Shop manually when ready.", [result.archive]), "info");
      } catch (error) { ctx.ui.notify(String(error), "error"); }
      finally { busy = false; }
    },
  });
}
