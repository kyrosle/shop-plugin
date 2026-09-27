import { expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeFileSync as write, mkdirSync as mkdir, existsSync } from "node:fs";
import { claimLaunch, deliverLeadReport, excludeShopDir, pendingLaunches, seatAgentName, startWhenShellReady } from "../extensions/seats.ts";

test("run records are excluded from git status once, via the repo-local exclude file", async () => {
  const repo = mkdtempSync(join(tmpdir(), "seats-exclude-"));
  execFileSync("git", ["init", "-q"], { cwd: repo });
  await excludeShopDir(repo);
  await excludeShopDir(repo);
  const exclude = readFileSync(join(repo, ".git/info/exclude"), "utf8");
  expect(exclude.split("\n").filter(line => line === "/.shop/").length).toBe(1);
  execFileSync("mkdir", ["-p", join(repo, ".shop/seats/r1")]);
  execFileSync("touch", [join(repo, ".shop/seats/r1/SPEC.md")]);
  expect(execFileSync("git", ["status", "--porcelain"], { cwd: repo, encoding: "utf8" })).toBe("");
});

test("outside a git repository the exclude step is a no-op", async () => {
  await excludeShopDir(mkdtempSync(join(tmpdir(), "seats-nogit-")));
});

test("seat agent names satisfy Herdr's rule even for long ids and stay unique", () => {
  const rule = /^[a-z][a-z0-9_-]{0,31}$/;
  const run = "/p/.shop/seats/20260925-120251-8A1475/";
  const long = seatAgentName(run, "task2_add_unique_words_and_more_text");
  expect(long).toMatch(rule);
  expect(seatAgentName(run, "lead")).toMatch(rule);
  expect(seatAgentName(run, "W.1 x")).toMatch(rule);
  expect(seatAgentName(run, "task2_add_unique_words_a")).not.toBe(seatAgentName(run, "task2_add_unique_words_b"));
});

test("the Lead report is delivered in-process exactly once, queued when busy, and retried after a failed send", () => {
  const run = mkdtempSync(join(tmpdir(), "seats-deliver-"));
  const sent: Array<{ text: string; options: unknown }> = [];
  const pi = { sendUserMessage: (text: string, options?: unknown) => { sent.push({ text, options }); } } as any;
  expect(deliverLeadReport(pi, run, true)).toBe(false);
  mkdir(join(run, "reports"));
  write(join(run, "reports/lead.json"), JSON.stringify({ id: "lead", role: "lead", status: "completed", summary: "three results", at: "t" }));
  const failing = { sendUserMessage: () => { throw new Error("session gone"); } } as any;
  expect(() => deliverLeadReport(failing, run, true)).toThrow("session gone");
  expect(existsSync(join(run, "reports/lead.delivered"))).toBe(false);
  expect(deliverLeadReport(pi, run, false)).toBe(true);
  expect(deliverLeadReport(pi, run, true)).toBe(false);
  expect(sent).toHaveLength(1);
  expect(sent[0].text).toContain("[Shop] Lead report");
  expect(sent[0].text).toContain("three results");
  expect(sent[0].text).toContain("First answer the user's original request");
  expect(sent[0].options).toEqual({ deliverAs: "followUp" });
});

test("agent start waits for a slow shell, but other errors and the deadline still fail", async () => {
  let calls = 0;
  const busyTwice = async () => { calls++; if (calls <= 2) throw new Error("agent_pane_busy: pane is not an available shell"); };
  await startWhenShellReady(["a"], busyTwice, 5_000, 1);
  expect(calls).toBe(3);
  await expect(startWhenShellReady(["a"], async () => { throw new Error("invalid_agent_name"); }, 5_000, 1)).rejects.toThrow("invalid_agent_name");
  await expect(startWhenShellReady(["a"], async () => { throw new Error("agent_pane_busy"); }, 20, 5)).rejects.toThrow("agent_pane_busy");
});

test("one-step runs stay pending until their Lead launch is claimed, exactly once", () => {
  const root = mkdtempSync(join(tmpdir(), "seats-launch-"));
  const run = (name: string) => { const dir = join(root, name); mkdir(dir); return dir; };
  const reviewed = run("reviewed"), oneStep = run("one-step"), launched = run("launched");
  mkdir(join(launched, "seats")); write(join(launched, "seats/lead.json"), JSON.stringify({ id: "lead", role: "lead" }));
  const entry = (dir: string, launch: boolean) => ({ type: "custom", customType: "shop-seat-run", data: { dir, launch } });
  const ctx = { sessionManager: { getEntries: () => [entry(reviewed, false), entry(oneStep, true), entry(launched, true)] } } as any;
  expect(pendingLaunches(ctx)).toEqual([oneStep]);
  expect(claimLaunch(oneStep)).toBe(true);
  expect(claimLaunch(oneStep)).toBe(false);
  expect(pendingLaunches(ctx)).toEqual([]);
});
