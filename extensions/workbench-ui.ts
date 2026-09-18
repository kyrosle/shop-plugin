import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { Thinking as ThinkingLevel } from "./configuration.js";
import { getSupportedThinkingLevels } from "@earendil-works/pi-ai";
import { matchesKey, truncateToWidth, wrapTextWithAnsi } from "@earendil-works/pi-tui";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { readBridge } from "./bridge.js";
import { findMember, readShopState, sendShopEnvelope, sessionIdFor, statePathFor, transportStatus } from "./transport.js";

type Json = Record<string, unknown>;
interface RequestRecord {
  id: string; kind: string; status: string;
  sender: { name: string }; recipient?: { name: string };
  payload: { profile?: { model: string; thinking?: ThinkingLevel | null }; [key: string]: unknown };
}
interface View {
  actor: string; snapshot: { shop: Json; members: Array<{ name: string; role: string; [key: string]: unknown }> };
  records: RequestRecord[]; handoffs: Json[]; models: Json;
}

export function workbenchClient(pi: ExtensionAPI, ctx: ExtensionContext) {
  const bridge = readBridge();
  const path = bridge && statePathFor(bridge, process.env);
  if (!bridge || !path) throw new Error("Shop bridge/Herdr context unavailable");
  const token = () => {
    const state = readShopState(path);
    const actor = state && findMember(state, process.env.HERDR_PANE_ID)?.member;
    if (!state || !actor) throw new Error("Shop membership unavailable");
    return { shop_id: state.shop_id, run_id: state.run_id, actor: actor.name, launch_id: actor.launch_id,
      session_id: sessionIdFor(ctx, process.env) };
  };
  const expected = token();
  return async <T = Json>(data: Json): Promise<T> => {
    if (JSON.stringify(token()) !== JSON.stringify(expected)) throw new Error("Shop/run/session changed; reopen /shop-ui");
    const result = await pi.exec("python3", [join(bridge.core_root, "core/workbench.py"), "--state", path,
      "--request", JSON.stringify({ ...data, expected })], { timeout: 90_000 });
    if (JSON.stringify(token()) !== JSON.stringify(expected)) throw new Error("Shop/run/session changed during operation; inspect recorded outcome, do not replay");
    if (result.code || result.killed) {
      let message = (result.stderr || result.stdout).slice(0, 4000);
      try {
        const error = JSON.parse(message) as { code: string; reason: string; next_action: string };
        message = `${error.code}: ${error.reason}\n下一步：${error.next_action}`;
      } catch { /* legacy/launcher error stays bounded */ }
      throw new Error(message);
    }
    if (result.stdout.length > 512_000) throw new Error("Workbench response exceeds display bound");
    return JSON.parse(result.stdout) as T;
  };
}

/** Bounded, read-only pager. No editor, model calls, files or repair actions. */
export async function showFacts(ctx: ExtensionContext, title: string, value: unknown): Promise<void> {
  const text = JSON.stringify(value, null, 2).slice(0, 64_000);
  await ctx.ui.custom<void>((tui, theme, _keys, done) => {
    let scroll = 0;
    let count = 0;
    return {
      render(width: number) {
        const rows = Math.max(3, Math.min(30, tui.terminal.rows - 5));
        const lines = text.split("\n").flatMap(line => wrapTextWithAnsi(line, Math.max(1, width - 2)));
        count = Math.max(0, lines.length - rows);
        scroll = Math.min(scroll, count);
        return [truncateToWidth(theme.fg("accent", title), width), ...lines.slice(scroll, scroll + rows),
          truncateToWidth(`↑↓ / PgUp PgDn · Esc 返回 · ${scroll + 1}/${lines.length}`, width)];
      },
      handleInput(data: string) {
        if (matchesKey(data, "escape") || matchesKey(data, "enter")) { done(); return; }
        if (matchesKey(data, "down")) scroll = Math.min(count, scroll + 1);
        if (matchesKey(data, "up")) scroll = Math.max(0, scroll - 1);
        if (matchesKey(data, "pageDown")) scroll = Math.min(count, scroll + 20);
        if (matchesKey(data, "pageUp")) scroll = Math.max(0, scroll - 20);
        tui.requestRender();
      },
      invalidate() {},
    };
  });
}

export async function submitPrepared(pi: ExtensionAPI, ctx: ExtensionContext, prepared: Json): Promise<Json> {
  const envelope = prepared.transport_request as Json | undefined;
  if (!envelope) return prepared;
  try {
    const outcome = await sendShopEnvelope(pi, ctx, envelope, prepared.transport_session as string | undefined);
    return { ...prepared, transport_request: undefined, transport: outcome,
      note: "Transport receipt is not business acceptance. No automatic resend." };
  } catch (error) {
    return { ...prepared, transport_request: undefined, transport: { outcome: "unknown", detail: String(error) },
      note: "Prepared record retained. Inspect before retry; no fallback or automatic resend." };
  }
}

export async function workbenchUI(pi: ExtensionAPI, ctx: ExtensionContext): Promise<void> {
  if (!ctx.hasUI) throw new Error("/shop-ui requires interactive Pi UI");
  const call = workbenchClient(pi, ctx);
  const chooseMember = async (view: View) => ctx.ui.select("选择精确接收工位", view.snapshot.members
    .filter(m => m.name !== view.actor).map(m => m.name));
  const input = (label: string) => ctx.ui.input(label);
  while (true) {
    const view = await call<View>({ action: "view" });
    const choice = await ctx.ui.select(`Shop · ${view.snapshot.shop.run_id ?? "unbound"} · ${view.actor}`, [
      "状态与身份", "接手请求", "待办与回执", "开发准备", "交付与集成", "空闲席位配置", "任务干预", "诊断与脱敏导出", "退出",
    ]);
    if (!choice || choice === "退出") return;
    try {
      if (choice === "状态与身份") {
        await showFacts(ctx, "只读快照 · 配置不是运行时观测", { ...view, local_transport: transportStatus() });
      } else if (choice === "接手请求") {
        const recipient = await chooseMember(view); if (!recipient) continue;
        const objective = await input("目标（必填）"); if (!objective) continue;
        const scope = await input("范围/限制（必填）"); if (!scope) continue;
        const acceptance = await input("验收标准（必填）"); if (!acceptance) continue;
        const evidence = await input("证据路径（可空）"); if (evidence === undefined) continue;
        if (!await ctx.ui.confirm("提交接手请求？", `${recipient}\n${objective}\n仅 proposed，不代表对方接受或任务完成。`)) continue;
        await showFacts(ctx, "接手请求结果", await submitPrepared(pi, ctx,
          await call({ action: "handoff-propose", recipient, objective, scope, acceptance, evidence })));
      } else if (choice === "待办与回执") {
        const records = view.records.filter(r => r.recipient?.name === view.actor);
        const selected = await ctx.ui.select("待办（业务接受 ≠ 传输送达）", ["查看全部回执", ...records.map(r => `${r.id} · ${r.kind} · ${r.status}`)]);
        if (!selected) continue;
        if (selected === "查看全部回执") { await showFacts(ctx, "回执", { records: view.records, handoffs: view.handoffs }); continue; }
        const record = records.find(r => selected.startsWith(r.id))!;
        await showFacts(ctx, "请求内容", record);
        if (record.kind === "handoff") {
          const transition = await ctx.ui.select("明确业务决定", ["accept", "needs_context", "reject", "deliver", "block"]);
          if (!transition) continue;
          const detail = await input("说明/交付证据"); if (detail === undefined) continue;
          if (!await ctx.ui.confirm("确认业务回执？", `${record.id}\n${transition}\n不会自动 accept 工单。`)) continue;
          await showFacts(ctx, "回执", await call({ action: "handoff-transition", id: record.id, transition, detail }));
        } else if (record.kind === "profile") {
          if (["applying", "unknown"].includes(record.status)) {
            if (!ctx.isIdle() || !ctx.model) throw new Error("Idle Pi with observable model required for reconciliation");
            const observed = { model: `${ctx.model.provider}/${ctx.model.id}`, thinking: pi.getThinkingLevel() };
            if (!await ctx.ui.confirm("确认当前运行时配置并解除派工阻塞？", JSON.stringify(observed))) continue;
            if (!ctx.isIdle()) throw new Error("Pi became busy; no reconciliation");
            await showFacts(ctx, "人工核对回执", await call({ action: "profile-resolve", id: record.id,
              session_id: sessionIdFor(ctx, process.env), observed }));
            continue;
          }
          const profile = record.payload.profile!;
          const model = ctx.modelRegistry.getAvailable().find(m => `${m.provider}/${m.id}` === profile.model);
          if (!model || (profile.thinking != null && !getSupportedThinkingLevels(model).includes(profile.thinking))) {
            throw new Error("Receiver model/provider/thinking unavailable; request not applied");
          }
          if (!ctx.isIdle()) throw new Error("Current Pi busy; no model change");
          if (!await ctx.ui.confirm("在当前空闲 Pi 应用配置？", JSON.stringify(record.payload))) continue;
          if (!ctx.isIdle()) throw new Error("Pi became busy; reopen later");
          const session_id = sessionIdFor(ctx, process.env);
          await call({ action: "profile-claim", id: record.id, session_id });
          let status = "unknown";
          let detail = "";
          try {
            if (!ctx.isIdle()) throw new Error("Pi became busy after claim; inspect request before retry");
            if (!await pi.setModel(model)) { status = "rejected"; detail = "Pi rejected model selection"; }
            else {
              if (!ctx.isIdle()) throw new Error("Pi became busy during model application; outcome unknown");
              if (profile.thinking != null) pi.setThinkingLevel(profile.thinking);
              if (profile.thinking != null && pi.getThinkingLevel() !== profile.thinking) throw new Error("Pi thinking differs from request");
              status = "applied";
            }
          } catch (error) { detail = String(error); }
          await showFacts(ctx, "配置回执", await call({ action: "profile-result", id: record.id, session_id, status, detail }));
        } else if (["scope-change", "pause", "cancel"].includes(record.kind)) {
          if (record.kind === "scope-change") {
            if (await ctx.ui.confirm("接受需求修订？", "启用新 revision。旧 ready 工单必须显式 revise；assigned 工单不改写。")) {
              await showFacts(ctx, "需求回执", await call({ action: "intervention-acknowledge", id: record.id }));
            }
          } else {
            const operation = await ctx.ui.select("主 Lead 干预操作", ["确认收到（不停止）", "执行已确认请求"]);
            if (operation === "确认收到（不停止）") {
              await showFacts(ctx, "干预回执", await call({ action: "intervention-acknowledge", id: record.id }));
            } else if (operation === "执行已确认请求") {
              if (record.kind === "cancel" && !await ctx.ui.confirm("确认旧写入者全部停止？", "必须检查前台与后台；idle 不构成后台停止证明。不确定请取消。")) continue;
              if (!await ctx.ui.confirm("执行干预？", record.kind === "pause"
                ? "仅向原执行者发送 Esc；不标记 stopped，不重新派工。"
                : "取消列出的当前 attempt；保留代码/checkpoint/result。未处理依赖会拒绝。")) continue;
              await showFacts(ctx, "干预执行结果", await call({ action: "intervention-execute", id: record.id,
                writers_stopped: record.kind === "cancel" }));
            }
          }
        }
      } else if (choice === "开发准备") {
        const path = await input("新 worktree 绝对路径（必须不存在）"); if (!path) continue;
        const branch = await input("新分支名称（必须不存在）"); if (!branch) continue;
        const base = await input("基线 commit/ref（空为 HEAD）"); if (base === undefined) continue;
        const plan = await call({ action: "development-preview", path, branch, base: base || "HEAD" });
        await showFacts(ctx, "开发预检 · 尚未修改 Git", plan);
        if (await ctx.ui.confirm("创建预览中的分支与 worktree？", `${path}\n${branch}\n不会提交、派工、覆盖或清理现有文件。`)) {
          await showFacts(ctx, "创建结果", await call({ action: "development-create", id: plan.id }));
        }
      } else if (choice === "交付与集成") {
        const report = await call({ action: "delivery" });
        await showFacts(ctx, "交付检查表", report);
        const operation = await ctx.ui.select("交付下一步", ["仅查看", "预览集成", "登记最终检查证据"]);
        if (!operation || operation === "仅查看") continue;
        if (operation === "登记最终检查证据") {
          const command = await input("已执行的检查命令/人工核验名称"); if (!command) continue;
          const code = await input("退出码（整数）"); if (code === undefined || !/^-?\d+$/.test(code)) continue;
          const evidence = await input("检查证据绝对路径（≤1 MiB）"); if (!evidence) continue;
          if (!await ctx.ui.confirm("登记人工声明的验证证据？", `HEAD ${report.target_commit}\n${command}\n插件不会运行命令；记录文件哈希，HEAD/文件变化则证据过期。`)) continue;
          await showFacts(ctx, "最终检查证据", await call({ action: "final-check", target_commit: report.target_commit,
            command, exit_code: Number(code), evidence }));
          continue;
        }
        const ticket = await input("可选：要集成的 accepted 工单 ID；空为仅查看"); if (!ticket) continue;
        const plan = await call({ action: "integration-preview", ticket });
        await showFacts(ctx, "集成计划 · ff-only · 未修改 Git", plan);
        if (!await ctx.ui.confirm("所有前台/后台写入者均已停止？", "idle 不是后台停止证明。必须亲自核实；不确定请取消。")) continue;
        if (!await ctx.ui.confirm("执行预览中的 Git fast-forward？", "将修改目标分支与文件。不会自动推送、关闭 run 或清理；失败保留现场。")) continue;
        await showFacts(ctx, "集成结果", await call({ action: "integrate", id: plan.id, writers_stopped: true }));
      } else if (choice === "空闲席位配置") {
        const recipient = await chooseMember(view); if (!recipient) continue;
        const model = await ctx.ui.select("选择请求模型（接收端再次验证）", ctx.modelRegistry.getAvailable().map(m => `${m.provider}/${m.id}`));
        if (!model) continue;
        const selected = ctx.modelRegistry.getAvailable().find(m => `${m.provider}/${m.id}` === model)!;
        const thinking = await ctx.ui.select("运行时必须选择明确思考档位", getSupportedThinkingLevels(selected)); if (!thinking) continue;
        const persist = await ctx.ui.confirm("同步到此 Shop 的后续启动配置？", "仅目标席位。否 = 仅当前会话；不修改全局/目录配置。");
        if (!await ctx.ui.confirm("请求目标 Pi 应用配置？", `${recipient}\n${model}\n${thinking}\n目标用户须在 /shop-ui 待办中确认。Architect 禁止。`)) continue;
        await showFacts(ctx, "配置请求（尚未应用）", await call({ action: "profile-request", recipient,
          profile: { model, thinking }, persist }));
      } else if (choice === "任务干预") {
        const kind = await ctx.ui.select("区分补充 / 需求变化 / 暂停 / 取消", ["supplement", "scope-change", "pause", "cancel"]);
        if (!kind) continue;
        const text = await input("补充内容或变更原因"); if (!text) continue;
        if (kind === "supplement") {
          const recipient = await chooseMember(view); if (!recipient) continue;
          if (!await ctx.ui.confirm("发送补充消息？", `${recipient}\n${text}\n忙碌时 steer；不创建任务或更改验收标准。`)) continue;
          await showFacts(ctx, "补充发送结果", await submitPrepared(pi, ctx,
            await call({ action: "note", recipient, text })));
          continue;
        }
        const tickets = await input("受影响工单 ID，逗号分隔（可空）"); if (tickets === undefined) continue;
        if (!await ctx.ui.confirm("创建干预请求？", `${kind}\n${text}\nrequested 不等于 stopped/cancelled。Lead 必须确认并执行授权操作。`)) continue;
        await showFacts(ctx, "干预请求", await call({ action: "intervention-request", kind, text,
          tickets: tickets.split(",").map(s => s.trim()).filter(Boolean) }));
      } else if (choice === "诊断与脱敏导出") {
        const report = await call({ action: "diagnostics" });
        await showFacts(ctx, "脱敏诊断（不含路径、ID、提示词、环境或原始错误）", report);
        if (!await ctx.ui.confirm("导出以上脱敏 JSON？", "仅写入本地 Shop diagnostics 目录；不上传。")) continue;
        const root = readBridge()!.state_dir;
        mkdirSync(join(root, "diagnostics"), { recursive: true, mode: 0o700 });
        const file = join(root, "diagnostics", `${randomUUID()}.json`);
        writeFileSync(file, JSON.stringify(report, null, 2), { mode: 0o600, flag: "wx" });
        ctx.ui.notify(file, "info");
      }
    } catch (error) { ctx.ui.notify(String(error), "error"); }
  }
}
