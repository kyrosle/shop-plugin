// Static isolation audit for the Shop built-in transport.
// Asserts the ported module closure cannot reach the upstream pi-intercom
// broker/runtime, cannot regress to the removed orchestration behaviours, and
// keeps Python Herdr access inside core/herdr.py.
import { expect, test } from "bun:test";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = join(import.meta.dir, "..");
const TRANSPORT_DIR = join(ROOT, "transport");

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) walk(path, out);
    else out.push(path);
  }
  return out;
}

function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("//") && !line.trimStart().startsWith("#"))
    .join("\n");
}

const TRANSPORT_FILES = walk(TRANSPORT_DIR);
// Documentation (NOTICE/README) is attribution, so the literal scan only covers
// executable sources; NOTICE contents are asserted separately below.
const AUDITED = [
  ...TRANSPORT_FILES.filter((path) => !path.endsWith(".md")),
  join(ROOT, "extensions/transport.ts"),
  join(ROOT, "extensions/index.ts"),
  join(ROOT, "core/transport.py"),
  join(ROOT, "core/transport_cli.py"),
  join(ROOT, "core/handoff.py"),
  join(ROOT, "bin/shop-transport"),
];

test("no audited file references the upstream intercom runtime outside attribution", () => {
  const offenders: string[] = [];
  for (const path of AUDITED) {
    if (!existsSync(path)) {
      offenders.push(`missing audited file ${relative(ROOT, path)}`);
      continue;
    }
    const code = stripComments(readFileSync(path, "utf8"));
    for (const needle of ["pi-intercom", "PI_INTERCOM", "getIntercomDirPath", "intercom:", "agent/intercom"]) {
      if (code.includes(needle)) offenders.push(`${relative(ROOT, path)}: ${needle}`);
    }
  }
  expect(offenders).toEqual([]);
});

test("protocol identity is exactly pi-shop-transport v1 with the Shop env namespace", () => {
  const paths = readFileSync(join(TRANSPORT_DIR, "shared/paths.ts"), "utf8");
  expect(paths).toContain('SHOP_TRANSPORT_PROTOCOL_NAME = "pi-shop-transport"');
  expect(paths).toContain("SHOP_TRANSPORT_PROTOCOL_VERSION = 1");
  expect(paths).toContain("PI_SHOP_TRANSPORT_DIR");
  expect(paths).not.toContain("PI_INTERCOM_TRANSPORT");
  const python = readFileSync(join(ROOT, "core/transport.py"), "utf8");
  expect(python).toContain("shop-transport-v1");
  expect(python).not.toContain("PI_INTERCOM");
});

test("the broker and shared layer stay host-agnostic (no Pi SDK import)", () => {
  const offenders = TRANSPORT_FILES.filter((path) => {
    const code = stripComments(readFileSync(path, "utf8"));
    return code.includes("@earendil-works/pi-coding-agent");
  }).map((path) => relative(ROOT, path));
  expect(offenders).toEqual([]);
  const extension = readFileSync(join(ROOT, "extensions/transport.ts"), "utf8");
  expect(extension).toContain("@earendil-works/pi-coding-agent");
});

test("every ported file keeps an attribution header and NOTICE records the freeze", () => {
  const ported = ["shared/framing.ts", "shared/paths.ts", "shared/runtime-claim.ts",
    "shared/protocol.ts", "shared/types.ts", "broker/broker.ts", "client/client.ts", "client/spawn.ts"];
  for (const name of ported) {
    const source = readFileSync(join(TRANSPORT_DIR, name), "utf8");
    expect(source.startsWith("//"), name).toBe(true);
    expect(source).toMatch(/pi-intercom\s+0\.13\.0/);
    expect(source).toMatch(/See transport\/NOTICE\.md/);
  }
  const notice = readFileSync(join(TRANSPORT_DIR, "NOTICE.md"), "utf8");
  expect(notice).toContain("1d89bd31ca63cccd82a5c950cc043bca99e17e4664df7fd99f16859a0442279f");
  expect(notice).toContain("sha512-+QjKJRAEhrgQZj4+M9OW/8unRLvCzeCp0K66lZmbS5/me0fsClXRtANBgM6mY+EoX+Fcd+qBE8dTThp8+ND//g==");
  expect(notice).toContain("199279ae861bf53ce014809fb2a03337538ae13e");
  expect(notice).toContain("2d20dfacd9742706e564470dc77438608a1e54b0ed46959f080709389209093c");
  expect(notice).toContain("Copyright (c) 2026 Nico Bailon");
  expect(notice).toContain("MIT");
  const license = readFileSync(join(ROOT, "LICENSE.pi-intercom"), "utf8");
  expect(license).toContain("Permission is hereby granted, free of charge");
  expect(readFileSync(join(ROOT, "THIRD_PARTY.md"), "utf8")).toContain("pi-intercom");
});

test("removed orchestration behaviours cannot be reintroduced", () => {
  const offenders: string[] = [];
  for (const path of TRANSPORT_FILES.filter((item) => !item.endsWith(".md"))) {
    const code = stripComments(readFileSync(path, "utf8"));
    for (const needle of ["queueMailboxMessage", "flushMailboxForSession", "rememberDisconnectedSession",
      "findDisconnectedSessions", "askEdges", "pending-asks", "namespaceOwners", "sameCwd",
      "supersededBy", "ExtensionCapability", "resolveOutboxTarget", "extension_publish",
      "updateExtensionCapabilities", "cancelAsk"]) {
      if (code.includes(needle)) offenders.push(`${relative(ROOT, path)}: ${needle}`);
    }
  }
  expect(offenders).toEqual([]);
});

test("the broker launch path resolves no external package manager or network", () => {
  const spawn = stripComments(readFileSync(join(TRANSPORT_DIR, "client/spawn.ts"), "utf8"));
  for (const needle of ["tsx", "npx", "npm ", "yarn", "pnpm", "https://", "http://", "fetch("]) {
    expect(spawn.includes(needle), needle).toBe(false);
  }
  expect(spawn).toContain("--experimental-strip-types");
  expect(spawn).toContain("process.execPath");
  const broker = stripComments(readFileSync(join(TRANSPORT_DIR, "broker/broker.ts"), "utf8"));
  expect(broker).not.toContain("child_process");
});

test("new Python modules keep the single Herdr boundary and use no subprocess", () => {
  for (const name of ["transport.py", "transport_cli.py", "handoff.py"]) {
    const source = readFileSync(join(ROOT, "core", name), "utf8");
    expect(source.includes("import subprocess"), name).toBe(false);
    expect(source.includes("subprocess."), name).toBe(false);
    expect(source.includes("HERDR_BIN_PATH"), name).toBe(false);
    expect(source.includes("SHOP_HERDR_BIN"), name).toBe(false);
    expect(source.includes("import herdr"), name).toBe(false);
  }
});

test("bin/shop-transport is an executable wrapper over the Python CLI", () => {
  const path = join(ROOT, "bin/shop-transport");
  const stats = statSync(path);
  expect(stats.mode & 0o111).toBeGreaterThan(0);
  const source = readFileSync(path, "utf8");
  expect(source).toContain("core/transport_cli.py");
  expect(source).not.toContain("npx");
});

test("Python transport tests do not import a live Herdr or socket layer", () => {
  for (const name of ["test_transport.py", "test_handoff.py"]) {
    const source = readFileSync(join(ROOT, "tests", name), "utf8");
    expect(source.includes("herdr")).toBe(false);
    expect(source.includes("PI_SHOP_TRANSPORT_DIR")).toBe(false);
  }
});
