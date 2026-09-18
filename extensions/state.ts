import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";

export type Mode = { kind: "off" | "architect" | "blocked"; token: string; state?: any; error?: string };
export function readMode(env: NodeJS.ProcessEnv, root: string, cwd: string): Mode {
  if (env.HERDR_ENV !== "1" || !env.HERDR_PANE_ID || !env.HERDR_TAB_ID || !env.HERDR_SOCKET_PATH)
    return { kind: "off", token: "off" };
  const key = createHash("sha256").update(`${env.HERDR_SOCKET_PATH}:${env.HERDR_TAB_ID}`).digest("hex").slice(0, 12);
  const file = join(root, "runtime", `${key}.json`);
  try {
    if (statSync(file).size > 65536) throw new Error("Oversized shop state");
    const state = JSON.parse(readFileSync(file, "utf8"));
    if (!state.architect || typeof state.architect.pane !== "string") throw new Error("Invalid Architect registration");
    if (state.architect.pane !== env.HERDR_PANE_ID) return { kind: "off", token: "off" };
    const token = JSON.stringify([state.shop_id, state.phase, state.run_id, state.lead?.pane, state.lead?.name]);
    if (state.tab !== env.HERDR_TAB_ID || state.cwd !== cwd)
      return { kind: "blocked", token, state, error: "Workstation tab/cwd mismatch" };
    if (state.phase !== "ready" || !state.shop_id || !state.lead?.name)
      return { kind: "blocked", token, state, error: `Shop phase: ${state.phase ?? "invalid"}` };
    return { kind: "architect", token, state };
  } catch (error: any) {
    if (error.code === "ENOENT") return { kind: "off", token: "off" };
    return { kind: "blocked", token: "invalid", error: String(error.message ?? error) };
  }
}

export function instructions(mode: Mode, role: string, previouslyEnabled: boolean, explicit = false): string | undefined {
  if (!explicit) {
    if (mode.kind === "off" && !previouslyEnabled) return undefined;
    return "本次未使用 /shop：当前为普通单 agent 模式，直接按用户要求解释、读代码、修改和测试，不自动转交Lead/Worker、不自动创建run或其他项目工位。旧对话中的强制Architect分工不适用于本次。已有Shop任务不因本次消息自动取消或转派；涉及同一文件的修改先核实在途写入者，避免冲突。普通消息不自动转发为旧任务补充。";
  }
  if (mode.kind === "off") return previouslyEnabled
    ? "Herdr 工位已关闭。当前恢复普通单 agent 模式；旧对话中的 Architect 工位限制不再因该工位而生效。仍遵守用户当前要求，不恢复或继续旧工单。"
    : undefined;
  if (mode.kind === "blocked") return `Herdr 工位状态异常：${mode.error}。不要默默退回单 agent 实现。只诊断/报告工位问题，不改业务代码、不派新任务，等待修复。`;
  return `${role}\n\n本次由用户 /shop 显式委托，采用 Architect 模式；仅限本次请求。\n主仓库：${mode.state.cwd}\n主 Lead：${mode.state.lead.name}\n绑定 run：${mode.state.run_id ?? "未绑定；目标明确的新任务由你创建run并绑定，不要求用户念派工指令"}\n仅本次/shop任务写最小SPEC/PLAN后交主Lead。U只准备窗口，不改变普通消息的直接执行模式。不得自动接管其他Pi或开跨项目工位。无绑定且新任务明确时创建并绑定run；已有绑定沿用，存在归属冲突才询问。你不直接改业务代码，不越过主Lead派Worker。默认通知主Lead时wait=true、timeout=600000毫秒；派后继续等待，直到读到SUMMARY/REVIEW并汇总，不能只回复已派活就结束回合。超时后get检查，working继续agent wait而不重复prompt；blocked/故障则明确报告。只有用户说后台跑/不用等才直接返回，不承诺自动唤醒。Lead忙碌/故障时检查状态，不重复派送也不默默自己接手。先核对herdr-shop status，按角色提示中的 WORKFLOW 文件交接。`;
}

export interface SnapshotFacts {
  schema?: string;
  phase?: string;
  runId?: string;
  members: number;
  attention: number;
  warn: number;
  unknowns: number;
  parseError?: string;
}

/** Read bounded facts out of a shop.snapshot/v1 document without raw dumps. */
export function snapshotFacts(text: string): SnapshotFacts {
  try {
    const document = JSON.parse(text);
    const attention = Array.isArray(document.attention) ? document.attention : [];
    return {
      schema: document.schema,
      phase: document.shop?.phase,
      runId: document.shop?.run_id ?? undefined,
      members: Array.isArray(document.members) ? document.members.length : 0,
      attention: attention.length,
      warn: attention.filter((entry: any) => entry?.severity === "warn").length,
      unknowns: Array.isArray(document.unknowns) ? document.unknowns.length : 0,
    };
  } catch (error: any) {
    return { members: 0, attention: 0, warn: 0, unknowns: 0, parseError: String(error?.message ?? error) };
  }
}

/**
 * Compact human view of one snapshot. The producer is already bounded and
 * redacted, so this only shortens it for a notification/tool result.
 */
export function summarizeSnapshot(text: string, limit = 4000): string {
  const facts = snapshotFacts(text);
  if (facts.parseError) {
    return `Shop snapshot unreadable (${facts.parseError.slice(0, 200)}); raw prefix: ${text.slice(0, 400)}`;
  }
  let document: any;
  try { document = JSON.parse(text); } catch { document = {}; }
  const lines: string[] = [];
  lines.push(`${facts.schema ?? "shop.snapshot/?"} · ${facts.phase ?? "unknown phase"} · run ${facts.runId ?? "unbound"}`
    + ` · members ${facts.members} · attention ${facts.attention} (warn ${facts.warn}) · unknowns ${facts.unknowns}`);
  const events = document.events ?? {};
  lines.push(`events: ${events.coverage ?? "unknown"} · revision ${events.revision ?? "-"}`
    + ` · facts ${events.fact_count ?? 0}` + (events.reason ? ` · ${events.reason}` : ""));
  const transport = document.transport ?? {};
  lines.push(`transport: ${transport.implemented ? "reported" : "unavailable"}`
    + ` · pending ${transport.pending ?? 0} · unknown ${transport.unknown ?? 0}`
    + ` · broker ${transport.broker?.live ? "live" : "not running"}`);
  const handoff = document.handoff ?? {};
  lines.push(`handoff: ${handoff.implemented ? "reported" : "unavailable"}`
    + ` · open ${(handoff.open ?? []).length}`);
  for (const member of document.members ?? []) {
    const observed = member.observed ?? {};
    lines.push(`  ${String(member.role ?? "?").padEnd(14)} ${String(observed.status ?? "?").padEnd(11)} `
      + `${String(member.name ?? "?").padEnd(22)} ${member.pane ?? ""} [${observed.source ?? "none"}]`);
    for (const ticket of member.tickets ?? []) {
      lines.push(`    ${ticket.ticket_id} a${ticket.attempt} ${ticket.status}`
        + ` [${ticket.responsibility?.state ?? "?"}] ${String(ticket.objective ?? "").slice(0, 60)}`);
    }
  }
  for (const entry of (document.attention ?? []).slice(0, 8)) {
    lines.push(`  [${entry.severity}] ${entry.code} ${entry.target ?? ""} — ${entry.detail ?? ""}`);
  }
  if ((document.unknowns ?? []).length) lines.push(`unknowns: ${document.unknowns.slice(0, 4).join("; ")}`);
  const links = (document.links ?? []).filter((link: any) => link.focus).slice(0, 4);
  if (links.length) {
    lines.push("locations (read-only focus commands):");
    for (const link of links) lines.push(`  ${link.label}: ${link.focus.command.join(" ")} && ${link.focus.then.join(" ")}`);
  }
  return lines.join("\n").slice(0, limit);
}
