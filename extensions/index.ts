import { t } from "./i18n.js";
import { registerLanguageCommand } from "./language-ui.js";
import { registerResetCommand } from "./reset-ui.js";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { instructions, readMode, snapshotFacts, summarizeSnapshot, type Mode } from "./state.js";
import { readBridge, type Bridge } from "./bridge.js";
import { ConfigPublisher } from "./configuration.js";
import { editConfiguration } from "./settings-ui.js";
import { startShopTransport, stopShopTransport, reconcileShopTransport, sessionIdFor } from "./transport.js";
import { workbenchUI, submitPrepared, workbenchClient } from "./workbench-ui.js";

/** No generic interception, session replay, model calls, or automatic dispatch. */
export default function shopMode(pi: ExtensionAPI) {
  if (process.env.HERDR_ENV !== "1") return;
  registerLanguageCommand(pi);
  registerResetCommand(pi);
  const configPublisher = new ConfigPublisher(pi);
  pi.registerCommand("shop-ui", { description: "Shop workbench / 工作台：status, handoffs, development, delivery, configuration", handler: async (_args, ctx) => {
    try { await workbenchUI(pi, ctx); } catch (error) { ctx.ui.notify(String(error), "error"); }
  } });
  pi.registerCommand("shop-config", { description: "Shop settings / 全局、目录、会话配置；existing Shops unchanged", handler: async (args, ctx) => {
    try {
      await configPublisher.start(ctx);
      await editConfiguration(pi, ctx, configPublisher, args.trim());
    } catch (error) { ctx.ui.notify(String(error), "error"); }
  } });
  pi.on("session_tree", async (_event, ctx) => {
    generation++; pending = undefined; liveCheck = undefined;
    await configPublisher.start(ctx);
    await stopShopTransport();
    await startShopTransport(pi, ctx);
  });
  let timer: ReturnType<typeof setInterval> | undefined;
  let everEnabled = false;
  let pending: { prompt: string; token: string; session: string; expires: number } | undefined;
  let generation = 0;
  let liveCheck: { token: string; expires: number; ready: boolean; reason?: string } | undefined;
  const preflight = async (ctx: ExtensionContext, bridge: Bridge, mode: Mode) => {
    const session = sessionIdFor(ctx, process.env), mine = generation;
    const result = await pi.exec("python3", [join(bridge.core_root, "core/shop.py"), "preflight", "--session-id", session], { timeout: 15000 });
    if (result.code !== 0 || result.killed) throw new Error((result.stderr || "Shop preflight failed").slice(0, 500));
    if (result.stdout.length > 65536) throw new Error("Oversized Shop preflight response");
    const proof = JSON.parse(result.stdout);
    if (generation !== mine || sessionIdFor(ctx, process.env) !== session
        || JSON.stringify(readBridge()) !== JSON.stringify(bridge)
        || readMode(process.env, bridge.state_dir, ctx.cwd, session).token !== mode.token)
      throw new Error("Shop/session changed during preflight; nothing delegated");
    if (proof.decision !== "ready") throw new Error(proof.reason || "Shop requires recovery");
    if (proof.shop_id !== mode.state?.shop_id || proof.run_id !== (mode.state?.run_id ?? null)
        || proof.session_id !== session || proof.state_revision !== mode.token
        || !Number.isFinite(proof.expires_at) || proof.expires_at <= Date.now() || proof.expires_at > Date.now() + 30000)
      throw new Error("Stale or mismatched Shop preflight");
    liveCheck = { token: mode.token, expires: proof.expires_at, ready: true };
    return proof;
  };
  // Cached live readiness is presentation only. /shop always rechecks before sending.
  let lastSnapshot = { attention: 0, warn: 0, schema: "shop.snapshot/v1" };
  const refresh = (ctx: ExtensionContext) => {
    const bridge = readBridge();
    const mode = bridge ? readMode(process.env, bridge.state_dir, ctx.cwd, sessionIdFor(ctx, process.env)) : { kind: "off" as const, token: "off" };
    if (mode.kind !== "off") everEnabled = true;
    const verified = liveCheck?.token === mode.token && liveCheck.expires > Date.now();
    if (ctx.hasUI) ctx.ui.setStatus("shop-workstation", mode.kind === "architect"
      ? verified && liveCheck?.ready
        ? t("SHOP ready · /shop delegate · {0}", [mode.state.run_id ?? "unbound"])
          + (lastSnapshot.attention ? ` · ⚠${lastSnapshot.attention}` : "")
        : verified ? t("SHOP blocked · inspect state") : t("SHOP checking · live status not verified")
      : mode.kind === "blocked" ? t("SHOP blocked · inspect state") : undefined);
    return { bridge, mode };
  };
  pi.on("session_start", (_event, ctx) => {
    void configPublisher.start(ctx);
    if (timer) clearInterval(timer);
    everEnabled = false;
    generation++; pending = undefined; liveCheck = undefined;
    let checking = false;
    let ticks = 0;
    let reconciling = false;
    const tick = () => {
      if (++ticks % 5 === 0 && !reconciling) {
        reconciling = true;
        void reconcileShopTransport(pi, ctx).catch(() => {}).finally(() => { reconciling = false; });
      }
      try {
        const { bridge, mode } = refresh(ctx);
        if (bridge && mode.kind === "architect" && !checking && ticks % 5 === 1) {
          checking = true;
          const mine = generation;
          void preflight(ctx, bridge, mode).catch(error => {
            if (generation === mine) liveCheck = { token: mode.token, expires: Date.now() + 5000, ready: false, reason: String(error) };
          }).finally(() => { checking = false; });
        }
      } catch { if (ctx.hasUI) ctx.ui.setStatus("shop-workstation", t("SHOP bridge error")); }
    };
    tick();
    timer = setInterval(tick, 1000);
    timer.unref();
    // Built-in transport: connect once per session. The factory never starts
    // resources, and a missing/incomplete identity simply leaves it off.
    void startShopTransport(pi, ctx).catch((error) => {
      if (ctx.hasUI) ctx.ui.setStatus("shop-transport", t("Transport error: {0}", [String(error).slice(0, 120)]));
    });
  });
  pi.on("session_shutdown", (_event, ctx) => {
    configPublisher.stop();
    if (timer) clearInterval(timer);
    timer = undefined;
    generation++; pending = undefined; liveCheck = undefined;
    void stopShopTransport().catch(() => { /* broker exits on its own idle timer */ });
    if (ctx.hasUI) {
      ctx.ui.setStatus("shop-workstation", undefined);
      ctx.ui.setStatus("shop-transport", undefined);
    }
  });
  pi.registerCommand("shop", { description: "Delegate this request to Shop / 仅本次委托；ordinary messages stay local", handler: async (args, ctx) => {
    if (!args.trim()) { ctx.ui.notify(t("Usage: /shop analyze the workflow. Ordinary messages do not dispatch."), "info"); return; }
    if (!ctx.isIdle() || pending) { ctx.ui.notify(t("Current turn unfinished; wait or interrupt before /shop. Nothing dispatched."), "warning"); return; }
    const { bridge, mode } = refresh(ctx);
    if (!bridge || mode.kind !== "architect") {
      ctx.ui.notify(t("Open Shop from the primary Pi with Ctrl+B → U first; repair invalid registration. No other Shops are created automatically."), "warning"); return;
    }
    const prompt = `[Explicit /shop request ${randomUUID()}]\n${args.trim()}`;
    const request = { prompt, token: mode.token, session: sessionIdFor(ctx, process.env), expires: 0 };
    pending = request;
    try {
      const proof = await preflight(ctx, bridge, mode);
      if (pending !== request || !ctx.isIdle()) throw new Error("Session/turn changed; nothing delegated");
      request.expires = proof.expires_at;
      pi.sendUserMessage(prompt);
    } catch (error) {
      if (pending === request) pending = undefined;
      liveCheck = { token: mode.token, expires: Date.now() + 5000, ready: false, reason: String(error) };
      ctx.ui.notify(String(error), "warning");
      refresh(ctx);
    }
  } });
  pi.on("before_agent_start", (event, ctx) => {
    const request = pending;
    pending = undefined; // One request only; never a sticky mode or automatic follow-up delegation.
    const { bridge, mode } = refresh(ctx);
    const explicit = request?.prompt === event.prompt;
    if (explicit && (mode.kind !== "architect" || mode.token !== request.token
        || request.session !== sessionIdFor(ctx, process.env) || request.expires <= Date.now()))
      return { systemPrompt: event.systemPrompt + "\n本次 /shop 的工位登记已变化。只报告未派工，不执行任务或自动恢复/新建工位。" };
    let role = "";
    if (explicit && mode.kind === "architect" && bridge) {
      const override = join(bridge.config_dir, "roles/architect.md");
      role = readFileSync(existsSync(override) ? override : join(bridge.core_root, "roles/architect.md"), "utf8")
        .replaceAll("{{WORKFLOW}}", join(bridge.core_root, "docs/en/WORKFLOW.md"));
      role += `\nPackage CLI paths: ${join(bridge.core_root, "bin/herdr-shop")} ; ${join(bridge.core_root, "bin/shop-run")}. Do not use legacy global wrappers.`;
    }
    const extra = instructions(mode, role, everEnabled, explicit);
    if (extra) return { systemPrompt: event.systemPrompt + "\n\n" + extra };
  });

  const call = async (args: string[], signal?: AbortSignal, ctx?: ExtensionContext) => {
    const bridge = readBridge();
    if (!bridge) throw new Error("Shop bridge unconfigured; run core/plugin.py configure first");
    const session = ctx && sessionIdFor(ctx, process.env);
    const result = await pi.exec("python3", [join(bridge.core_root, "core/shop.py"), ...args], { signal, timeout: 180000 });
    if (ctx && sessionIdFor(ctx, process.env) !== session) throw new Error("Pi session changed during preparation; inspect recorded state, do not resend");
    if (result.code !== 0 || result.killed) throw new Error((result.stderr || result.stdout || "Shop command interrupted").slice(0, 12000));
    if (ctx) {
      const prepared = JSON.parse(result.stdout) as Record<string, unknown>;
      const delivered = await submitPrepared(pi, ctx, prepared);
      return { content: [{ type: "text" as const, text: JSON.stringify(delivered) }], details: {} };
    }
    const text = result.stdout.length > 20000 ? result.stdout.slice(0, 20000) + "\n[Truncated; inspect state/report files for full output]" : result.stdout;
    return { content: [{ type: "text" as const, text }], details: {} };
  };
  pi.registerCommand("shop-status", { description: "Show current Shop status / 查看状态", handler: async (_args, ctx) => {
    const result = await call(["status"]);
    const facts = snapshotFacts(result.content[0].text);
    lastSnapshot = { attention: facts.attention, warn: facts.warn, schema: facts.schema ?? "shop.snapshot/v1" };
    refresh(ctx);
    if (ctx.hasUI) ctx.ui.notify(summarizeSnapshot(result.content[0].text), "info");
  } });
  pi.registerTool({ name: "shop_message", label: "Shop message", description: "Light same-shop question, answer, progress or correction. No ticket created; transport journal retained. Target exact member name or unique role architect/lead/worker/auxiliary_lead. Submitted is not acknowledged; no automatic resend. Output capped at 20k characters.",
    parameters: Type.Object({ target: Type.String({ minLength: 1 }), text: Type.String({ minLength: 1, maxLength: 4000 }), allow_busy: Type.Optional(Type.Boolean()) }),
    execute: (_id, args, signal, _update, ctx) => call(["message", args.target, "--text", args.text, ...(args.allow_busy ? ["--allow-busy"] : [])], signal, ctx) });
  pi.registerTool({ name: "shop_status", label: "Shop status", description: "Read the bounded shop.snapshot/v1 view (desired vs observed facts, tickets, attention, unknowns); no repair. Compact summary, capped at 4k characters.", parameters: Type.Object({}),
    execute: async (_id, _args, signal) => {
      const result = await call(["status"], signal);
      const facts = snapshotFacts(result.content[0].text);
      lastSnapshot = { attention: facts.attention, warn: facts.warn, schema: facts.schema ?? "shop.snapshot/v1" };
      return { content: [{ type: "text" as const, text: summarizeSnapshot(result.content[0].text) }], details: facts };
    } });
  pi.registerTool({ name: "shop_patrol", label: "Shop patrol", description: "Primary Lead bounded patrol of latest auxiliary Sol/Workers. No automatic pause or scaling. Output capped at 20k characters.",
    parameters: Type.Object({ seconds: Type.Optional(Type.Integer({ minimum: 0, maximum: 120 })) }),
    execute: (_id, args, signal) => call(["patrol", "--seconds", String(args.seconds ?? 60)], signal) });
  pi.registerTool({ name: "shop_dispatch", label: "Shop dispatch", description: "Primary Lead explicitly dispatches ready ticket in bound run. Uncertain delivery must not be retried blindly.",
    parameters: Type.Object({ ticket: Type.String({ minLength: 1 }) }),
    execute: (_id, args, signal, _update, ctx) => call(["dispatch", args.ticket], signal, ctx) });
  pi.registerTool({ name: "shop_handoff", label: "Shop handoff receipt", description: "Exact recipient business receipt. Not ticket acceptance or transport delivery. Use /shop-ui to propose.",
    parameters: Type.Object({ id: Type.String(), transition: Type.Union([Type.Literal("accept"), Type.Literal("needs_context"), Type.Literal("reject"), Type.Literal("deliver"), Type.Literal("block")]), detail: Type.Optional(Type.String({ maxLength: 2000 })) }),
    execute: async (_id, args, _signal, _update, ctx) => {
      const result = await workbenchClient(pi, ctx)({ action: "handoff-transition", ...args });
      return { content: [{ type: "text" as const, text: JSON.stringify(result) }], details: {} };
    } });
}
