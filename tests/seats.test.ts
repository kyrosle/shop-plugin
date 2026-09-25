import { expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { excludeShopDir } from "../extensions/seats.ts";

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
