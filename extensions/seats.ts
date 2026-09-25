// Ephemeral seats: Architect aligns and writes SPEC/PLAN, then each downstream
// seat starts from a curated child session in its own Herdr pane, works, reports
// through a tool, and its pane closes. No resident members, no ticket JSON.
import { execFile } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readBridge } from "./bridge.js";
import { HANDOFF_MODES, handoffToChildSession, type HandoffMode, type HandoffResult } from "./seat-curation.js";

const run = promisify(execFile);
const PACKAGE = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const RUN_ENTRY = "shop-seat-run";

export type SeatRole = "lead" | "worker";
export type SeatRecord = {
  id: string; role: SeatRole; pane: string; parent_pane: string; parent_id: string;
  session: string; prompt: string; model: string; thinking?: string; handoff: Omit<HandoffResult, "file">; started_at: string;
};
export type SeatReport = { id: string; role: SeatRole; status: string; summary: string; evidence?: string; at: string };
type Profile = { model: string; thinking?: string | null };

const LEAD_INSTRUCTION = "Downstream reader: the Lead who dispatches PLAN.md to fast Workers. Keep exact: user " +
  "constraints and decisions, file paths, commands, errors and facts the plan depends on. Summarize rationale " +
  "briefly. Drop rejected or superseded alternatives, chit-chat and anything SPEC.md/PLAN.md already state " +
  "(they are attached in full).";
const WORKER_INSTRUCTION = "Downstream reader: a fast Worker executing only the focus task. Keep exact only what " +
  "that task needs: relevant file contents, paths, commands, SPEC constraints. Drop other tasks, dispatch and " +
  "review chatter, and the Lead's own planning.";

function atomicJson(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  writeFileSync(path + ".tmp", JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
  renameSync(path + ".tmp", path);
}

function readJson<T>(path: string): T | undefined {
  try { return JSON.parse(readFileSync(path, "utf8")) as T; } catch { return undefined; }
}

export function seatRecords(runDir: string): SeatRecord[] {
  const dir = join(runDir, "seats");
  if (!existsSync(dir)) return [];
  return readdirSync(dir).filter(name => name.endsWith(".json"))
    .map(name => readJson<SeatRecord>(join(dir, name))).filter((seat): seat is SeatRecord => !!seat);
}

export function seatReport(runDir: string, id: string): SeatReport | undefined {
  return readJson<SeatReport>(join(runDir, "reports", id + ".json"));
}

function herdrBin(): string {
  return process.env.SHOP_HERDR_BIN || "herdr";
}

async function herdr(...args: string[]): Promise<any> {
  const { stdout } = await run(herdrBin(), args, { timeout: 180_000, maxBuffer: 4 << 20 });
  const parsed = stdout.trim() ? JSON.parse(stdout) : {};
  if (parsed.error) throw new Error(`herdr ${args.slice(0, 2).join(" ")}: ${JSON.stringify(parsed.error).slice(0, 500)}`);
  return parsed.result ?? parsed;
}

async function seatProfiles(): Promise<Record<string, Profile>> {
  const bridge = readBridge();
  if (!bridge) throw new Error("Shop bridge unconfigured; run core/plugin.py configure first");
  const { stdout } = await run("python3", [join(bridge.core_root, "core/settings.py"), "seats"], {
    timeout: 15_000, env: { ...process.env, SHOP_CONFIG_DIR: process.env.SHOP_CONFIG_DIR ?? bridge.config_dir },
  });
  return JSON.parse(stdout);
}

function handoffMode(): HandoffMode {
  const mode = (process.env.SHOP_HANDOFF_MODE || "auto") as HandoffMode;
  if (!HANDOFF_MODES.includes(mode)) throw new Error("SHOP_HANDOFF_MODE must be one of " + HANDOFF_MODES.join(", "));
  return mode;
}

function newRunId(): string {
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..*/, "").replace("T", "-");
  return `${stamp}-${randomBytes(3).toString("hex")}`;
}

/** Open a pane next to `anchor`, start Pi on the child session with the brief pinned in its system prompt. */
async function spawnSeat(options: {
  runDir: string; id: string; role: SeatRole; anchor: string; direction: "right" | "down"; cwd: string;
  parentId: string; profile: Profile; handoff: HandoffResult; brief: string;
}): Promise<SeatRecord> {
  const { runDir, id, role, profile, handoff } = options;
  // System prompt survives the seat's own auto-compaction; session messages may not.
  const prompt = join(runDir, "prompts", id + ".md");
  mkdirSync(dirname(prompt), { recursive: true, mode: 0o700 });
  writeFileSync(prompt, readFileSync(join(PACKAGE, "roles", `seat-${role}.md`), "utf8") + "\n\n" + options.brief + "\n",
    { mode: 0o600 });
  const parentPane = process.env.HERDR_PANE_ID;
  if (!parentPane) throw new Error("Not running inside a Herdr pane");
  const env = { SHOP_SEAT_ROLE: role, SHOP_SEAT_RUN: runDir, SHOP_SEAT_ID: id, SHOP_SEAT_PARENT_PANE: parentPane };
  const split = await herdr("pane", "split", options.anchor, "--direction", options.direction, "--cwd", options.cwd,
    ...Object.entries(env).flatMap(([key, value]) => ["--env", `${key}=${value}`]));
  const pane = split.pane?.pane_id;
  if (!pane) throw new Error("herdr pane split returned no pane");
  const seat: SeatRecord = {
    id, role, pane, parent_pane: parentPane, parent_id: options.parentId, session: handoff.file, prompt,
    model: profile.model, ...(profile.thinking ? { thinking: profile.thinking } : {}),
    handoff: (({ file: _file, ...rest }) => rest)(handoff), started_at: new Date().toISOString(),
  };
  atomicJson(join(runDir, "seats", id + ".json"), seat);
  const name = `seat-${runDir.split("/").pop()!.slice(-6)}-${id}`.toLowerCase().replace(/[^a-z0-9-]/g, "-");
  await herdr("agent", "start", name, "--kind", "pi", "--pane", pane, "--timeout", "120000", "--",
    "--session", handoff.file, "--model", profile.model, ...(profile.thinking ? ["--thinking", profile.thinking] : []),
    "--append-system-prompt", prompt);
  await herdr("agent", "prompt", pane, "Start now: follow the Shop brief in your system prompt.");
  return seat;
}

function latestRun(ctx: ExtensionContext): string | undefined {
  const entries = ctx.sessionManager.getEntries() as Array<{ type: string; customType?: string; data?: { dir?: string } }>;
  return entries.filter(entry => entry.type === "custom" && entry.customType === RUN_ENTRY).pop()?.data?.dir;
}

function specPrompt(runDir: string, goal: string): string {
  return [
    `[Shop /shop-spec] Write the run specification for this goal into ${runDir}. Do not implement anything.`,
    "", `Goal: ${goal}`, "",
    `${runDir}/SPEC.md: objective; constraints (quote the user's words where they decided something); non-goals; ` +
      "acceptance criteria that can be checked by reading files or running commands.",
    `${runDir}/PLAN.md: numbered tasks. Each task: goal, exact files/scope, acceptance check, dependencies, and whether ` +
      "it can run in parallel. Tasks must be executable by a fast model without further design.",
    "Keep both short. When written, tell the user to review them and run /shop-go.",
  ].join("\n");
}

function leadBrief(runDir: string): string {
  const read = (name: string) => readFileSync(join(runDir, name), "utf8");
  return [`# Shop run brief (Lead)`, `Run directory: ${runDir}`, "", "## SPEC.md", read("SPEC.md"), "", "## PLAN.md",
    read("PLAN.md")].join("\n");
}

export function registerSeats(pi: ExtensionAPI): void {
  const role = process.env.SHOP_SEAT_ROLE as SeatRole | undefined;
  const runDir = process.env.SHOP_SEAT_RUN;
  const seatId = process.env.SHOP_SEAT_ID;

  if (!role) {
    pi.registerCommand("shop-spec", { description: "Write SPEC.md + PLAN.md for a goal (ephemeral seats)", handler: async (args, ctx) => {
      if (!args.trim()) { ctx.ui.notify("Usage: /shop-spec <goal>", "info"); return; }
      if (!ctx.isIdle()) { ctx.ui.notify("Current turn unfinished; nothing started", "warning"); return; }
      const dir = join(ctx.cwd, ".shop", "seats", newRunId());
      mkdirSync(dir, { recursive: true, mode: 0o700 });
      pi.appendEntry(RUN_ENTRY, { dir });
      pi.sendUserMessage(specPrompt(dir, args.trim()));
    } });

    pi.registerCommand("shop-go", { description: "Curate context and start the Lead for the latest /shop-spec run", handler: async (args, ctx) => {
      const dir = args.trim() ? resolve(ctx.cwd, args.trim()) : latestRun(ctx);
      if (!dir || !existsSync(join(dir, "SPEC.md")) || !existsSync(join(dir, "PLAN.md"))) {
        ctx.ui.notify("No run with SPEC.md and PLAN.md; run /shop-spec first", "warning"); return;
      }
      if (!ctx.isIdle()) { ctx.ui.notify("Current turn unfinished; nothing started", "warning"); return; }
      if (seatRecords(dir).some(seat => seat.role === "lead")) { ctx.ui.notify("This run already has a Lead", "warning"); return; }
      const profiles = await seatProfiles();
      const lead = profiles.lead;
      if (ctx.hasUI && !await ctx.ui.confirm("Launch Lead?", `${dir}\nLead: ${lead.model} (${lead.thinking ?? "default"})\n` +
        `Curator analyzer: ${profiles.worker.model}`)) return;
      ctx.ui.setStatus("shop-seats", "Handing context to Lead…");
      try {
        const handoff = await handoffToChildSession(ctx, {
          focus: "Lead: decompose PLAN.md into Worker tasks, dispatch, review, report", instruction: LEAD_INSTRUCTION,
          analyzerModel: profiles.worker.model, receiverModel: lead.model, sessionDir: join(dir, "sessions"),
          name: "Shop Lead", mode: handoffMode(),
        });
        const seat = await spawnSeat({ runDir: dir, id: "lead", role: "lead", anchor: process.env.HERDR_PANE_ID!,
          direction: "right", cwd: ctx.cwd, parentId: "architect", profile: lead, handoff, brief: leadBrief(dir) });
        ctx.ui.notify(`Lead started in ${seat.pane} (context ${handoff.mode}: ${handoff.reason}). ` +
          "Keep talking here; its report will arrive as a message.", "info");
      } finally {
        ctx.ui.setStatus("shop-seats", undefined);
      }
    } });
    return;
  }

  if (!runDir || !seatId) throw new Error("Shop seat environment incomplete");
  let reported = false;

  pi.registerTool({ name: "shop_report", label: "Shop report",
    description: "Report this seat's final result exactly once, then stop. Lead reports go to the Architect; Worker reports go to the Lead.",
    parameters: Type.Object({
      status: Type.Union([Type.Literal("completed"), Type.Literal("blocked"), Type.Literal("failed")]),
      summary: Type.String({ minLength: 1, maxLength: 8000 }),
      evidence: Type.Optional(Type.String({ maxLength: 8000 })),
    }),
    execute: async (_id, args) => {
      if (reported) throw new Error("Already reported; stop now");
      const report: SeatReport = { id: seatId, role, status: args.status, summary: args.summary,
        ...(args.evidence ? { evidence: args.evidence } : {}), at: new Date().toISOString() };
      atomicJson(join(runDir, "reports", seatId + ".json"), report);
      reported = true;
      if (role === "lead") {
        await herdr("agent", "prompt", process.env.SHOP_SEAT_PARENT_PANE!, [
          `[Shop] Lead report for ${runDir}: ${args.status}.`, args.summary.slice(0, 3000), "",
          `Verify against ${runDir}/SPEC.md acceptance criteria (reports are in ${runDir}/reports) and give the user the final result.`,
        ].join("\n"));
      }
      return { content: [{ type: "text" as const, text: "Reported. Stop now; this seat will close." }], details: report };
    } });

  pi.on("agent_end", async () => {
    // The seat is disposable: once its report is durable, close its own pane.
    if (reported && process.env.HERDR_PANE_ID) setTimeout(() => { void herdr("pane", "close", process.env.HERDR_PANE_ID!).catch(() => {}); }, 1500);
  });

  if (role !== "lead") return;

  pi.registerTool({ name: "shop_spawn_worker", label: "Shop spawn worker",
    description: "Start one Worker in a new pane from a context curated for this task. Returns immediately; collect results with shop_wait_workers.",
    parameters: Type.Object({
      id: Type.String({ pattern: "^[A-Za-z0-9_-]{1,32}$" }),
      task: Type.String({ minLength: 1, maxLength: 12000 }),
    }),
    execute: async (_id, args, signal, _update, ctx) => {
      if (existsSync(join(runDir, "seats", args.id + ".json")) || args.id === "lead") throw new Error("Seat id already used: " + args.id);
      const profiles = await seatProfiles();
      const handoff = await handoffToChildSession(ctx, {
        focus: `Worker task ${args.id}: ${args.task.slice(0, 600)}`, instruction: WORKER_INSTRUCTION,
        analyzerModel: profiles.worker.model, receiverModel: profiles.worker.model, sessionDir: join(runDir, "sessions"),
        name: `Shop Worker ${args.id}`, mode: handoffMode(), signal,
      });
      const seat = await spawnSeat({ runDir, id: args.id, role: "worker", anchor: process.env.HERDR_PANE_ID!,
        direction: "down", cwd: ctx.cwd, parentId: seatId, profile: profiles.worker, handoff,
        brief: `# Shop task ${args.id} (Worker)\nRun directory: ${runDir}\n\n${args.task}\n\nFinish with shop_report exactly once.` });
      return { content: [{ type: "text" as const, text: JSON.stringify({ id: args.id, pane: seat.pane, context: handoff.mode }) }],
        details: seat };
    } });

  pi.registerTool({ name: "shop_wait_workers", label: "Shop wait workers",
    description: "Block (without model calls) until every Worker you spawned has reported, one disappears, or the timeout passes. Returns their reports.",
    parameters: Type.Object({ timeout_seconds: Type.Optional(Type.Integer({ minimum: 10, maximum: 1800 })) }),
    execute: async (_id, args, signal) => {
      const deadline = Date.now() + (args.timeout_seconds ?? 900) * 1000;
      let lastLiveness = 0;
      const lost = new Set<string>();
      while (true) {
        const mine = seatRecords(runDir).filter(seat => seat.role === "worker" && seat.parent_id === seatId);
        const pending = mine.filter(seat => !seatReport(runDir, seat.id) && !lost.has(seat.id));
        if (!pending.length || Date.now() > deadline || signal?.aborted) {
          const result = mine.map(seat => seatReport(runDir, seat.id) ??
            { id: seat.id, status: lost.has(seat.id) ? "lost" : "pending", summary: lost.has(seat.id) ?
              "Worker pane closed without a report" : "No report before timeout" });
          return { content: [{ type: "text" as const, text: JSON.stringify(result).slice(0, 20000) }], details: result };
        }
        if (Date.now() - lastLiveness > 10_000) {
          lastLiveness = Date.now();
          for (const seat of pending) {
            try { await herdr("pane", "get", seat.pane); } catch { if (!seatReport(runDir, seat.id)) lost.add(seat.id); }
          }
        }
        await new Promise(resolveWait => setTimeout(resolveWait, 2000));
      }
    } });
}
