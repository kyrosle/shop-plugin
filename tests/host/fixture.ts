/** Only loaded by the isolated host runner. Never packaged or loaded in users' Pi. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream, type AssistantMessage } from "@earendil-works/pi-ai";
import { existsSync, writeFileSync, appendFileSync, renameSync } from "node:fs";
import { join } from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import { once } from "node:events";

export default function fixture(pi: ExtensionAPI) {
  const root = process.env.SHOP_HOST_TEST_ROOT;
  if (!root || !existsSync(join(root, "owned-test.json"))
      || process.env.HERDR_SOCKET_PATH !== join(root, "herdr", "h.sock"))
    throw new Error("Host fixture requires its private owned test environment");
  pi.registerProvider("shop-host-fixture", {
    baseUrl: "http://127.0.0.1:9", apiKey: "local-fixture-not-a-credential", api: "openai-completions",
    models: Array.from({ length: 13 }, (_, i) => ({
      id: i === 0 ? "no-network" : `no-network-${String(i).padStart(2, "0")}`,
      name: i === 0 ? "Host fixture (no network)" : `Host fixture variant ${i}`,
      reasoning: false, input: ["text" as const], contextWindow: 32768, maxTokens: 256,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    })),

    streamSimple(model) {
      // Deterministic local response, never an HTTP request or a tool call.
      appendFileSync(join(root, "provider-calls.jsonl"), JSON.stringify({ pane: process.env.HERDR_PANE_ID, at: Date.now() }) + "\n");
      const stream = createAssistantMessageEventStream();
      const message: AssistantMessage = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
        content: [{ type: "text", text: "SHOP_HOST_FIXTURE_ACK" }], stopReason: "stop", timestamp: Date.now(),
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      queueMicrotask(() => { stream.push({ type: "done", reason: "stop", message }); stream.end(); });
      return stream;
    },
  });
  // Controlled negative test: a no-network child in a NEW POSIX session,
  // with no tty/stdio. Tree inspection must still find it while Pi is idle.
  let job: ChildProcess | undefined;
  const jobFile = () => join(root, "observations", process.env.HERDR_PANE_ID + ".job.json");
  const recordJob = (pid: number | undefined, running: boolean) => {
    writeFileSync(jobFile() + ".tmp", JSON.stringify({ pid, owner: process.pid, running }), { mode: 0o600 });
    renameSync(jobFile() + ".tmp", jobFile());
  };
  const stopJob = async () => {
    const child = job;
    if (child && child.exitCode === null && child.signalCode === null) {
      const stopped = once(child, "exit");
      child.kill("SIGTERM"); // Only our direct fixture child, never a discovered PID.
      await stopped;
    }
    if (child) recordJob(child.pid, false);
    job = undefined;
  };
  pi.registerCommand("shop-host-background-start", { description: "Isolated fixture only", handler: async () => {
    if (job) throw new Error("Fixture job already exists");
    job = spawn(process.execPath, ["-e", "setTimeout(() => {}, 20000)"], {
      detached: true, stdio: "ignore", env: { PATH: "/usr/bin:/bin" },
    });
    await once(job, "spawn");
    recordJob(job.pid, true);
  } });
  pi.registerCommand("shop-host-background-stop", { description: "Isolated fixture only", handler: stopJob });
  pi.on("session_shutdown", stopJob);
  pi.on("session_start", (_event, ctx) => {
    const pane = process.env.HERDR_PANE_ID;
    if (!pane || !/^[a-zA-Z0-9:._-]+$/.test(pane)) throw new Error("Missing host fixture pane identity");
    const observation = join(root, "observations", pane + ".json");
    writeFileSync(observation + ".tmp", JSON.stringify({
      pane, pid: process.pid, tab: process.env.HERDR_TAB_ID, socket: process.env.HERDR_SOCKET_PATH,
      session: ctx.sessionManager.getSessionId(), mode: ctx.mode, hasUI: ctx.hasUI,
      model: ctx.model && `${ctx.model.provider}/${ctx.model.id}`,
      commands: pi.getCommands().map(c => c.name), at: Date.now(),
    }), { mode: 0o600 });
    renameSync(observation + ".tmp", observation);
  });
}
