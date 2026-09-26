import { expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { writeFileSync as write, mkdirSync as mkdir, existsSync } from "node:fs";
import { deliverLeadReport, excludeShopDir, seatAgentName } from "../extensions/seats.ts";

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
