// Pi-side rendering of the bounded snapshot: compact summary and facts only.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { snapshotFacts, summarizeSnapshot } from "../extensions/state.ts";

const SNAPSHOT = readFileSync(join(import.meta.dir, "fixtures-snapshot/snapshot-sample.json"), "utf8");

test("snapshot facts expose counts without raw content", () => {
  const facts = snapshotFacts(SNAPSHOT);
  expect(facts.schema).toBe("shop.snapshot/v1");
  expect(facts.phase).toBe("ready");
  expect(facts.runId).toBe("demo-run");
  expect(facts.members).toBe(3);
  expect(facts.attention).toBeGreaterThan(0);
  expect(facts.unknowns).toBe(0);
  expect(facts.parseError).toBeUndefined();
});

test("compact summary renders members, tickets, attention and read-only focus links", () => {
  const summary = summarizeSnapshot(SNAPSHOT, 4000, "en");
  expect(summary.length).toBeLessThanOrEqual(4000);
  expect(summary).toContain("shop.snapshot/v1");
  expect(summary).toContain("demo-worker");
  expect(summary).toContain("working");
  expect(summary).toContain("T2 a2 review");
  expect(summary).toContain("read-only focus commands");
  expect(summary).toContain("live+event");
  expect(summary).toContain("events: observed");
  expect(summary).toContain("transport: reported");
  expect(summary).toContain("broker live");
  expect(summary).toContain("handoff: reported");
  expect(summary).toContain("herdr workspace focus w1");
  // Allowlisted fields only: an injected non-allowlisted key is never rendered.
  expect(summary).not.toContain("RAW PROMPT MUST NOT BE RENDERED");
  expect(summary).not.toContain("RAW TRANSCRIPT MUST NOT BE RENDERED");
});

test("unreadable snapshot input degrades to a bounded message", () => {
  const summary = summarizeSnapshot("not json at all", 4000, "en");
  expect(summary).toContain("unreadable");
  expect(summary.length).toBeLessThan(700);
  const facts = snapshotFacts("not json at all");
  expect(facts.parseError).toBeDefined();
  expect(facts.attention).toBe(0);
});

test("long snapshots stay bounded by the summary limit", () => {
  const huge = JSON.stringify({
    schema: "shop.snapshot/v1",
    shop: { phase: "ready", run_id: "r" },
    members: Array.from({ length: 200 }, (_v, index) => ({
      role: "worker", name: `w${index}`, pane: `p${index}`,
      observed: { status: "idle" },
      tickets: [{ ticket_id: `T${index}`, attempt: 1, status: "assigned", objective: "o".repeat(400) }],
    })),
    attention: Array.from({ length: 50 }, (_v, index) => ({ code: "c", severity: "info", target: `t${index}` })),
    unknowns: ["u"],
    events: { coverage: "partial", revision: 3, fact_count: 2, reason: "revision gap" },
  });
  const bounded = summarizeSnapshot(huge, 4000, "en");
  expect(bounded.length).toBeLessThanOrEqual(4000);
  expect(bounded).toContain("events: partial");
  expect(bounded).toContain("transport:");
});
