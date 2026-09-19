import { test, expect } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { readMode, instructions } from "../extensions/state.ts";
import extension from "../extensions/index.ts";

function fixture(fn: (root: string, env: NodeJS.ProcessEnv, file: string, state: any) => void) {
  const root = mkdtempSync(join(tmpdir(), "shop-mode-"));
  const env = { HERDR_ENV: "1", HERDR_TAB_ID: "t1", HERDR_PANE_ID: "p1", HERDR_SOCKET_PATH: "/mock/socket" };
  const key = createHash("sha256").update("/mock/socket:t1").digest("hex").slice(0,12);
  mkdirSync(join(root,"runtime"));
  const file = join(root,"runtime",key+".json");
  const state = { tab:"t1", cwd:"/repo", shop_id:"s1", phase:"ready", architect:{pane:"p1"}, lead:{name:"s-lead",pane:"p2"} };
  try { fn(root,env,file,state); } finally { rmSync(root,{recursive:true,force:true}); }
}

test("outside Herdr and absent workstation remain ordinary", () => fixture((r,e,f,s) => {
  expect(readMode({},r,"/repo").kind).toBe("off");
  expect(readMode(e,r,"/repo").kind).toBe("off");
  expect(instructions(readMode(e,r,"/repo"),"",false)).toBeUndefined();
}));
test("late setup enables only Architect, reset disables", () => fixture((r,e,f,s) => {
  writeFileSync(f,JSON.stringify(s));
  const active=readMode(e,r,"/repo");
  expect(active.kind).toBe("architect");
  expect(instructions(active,"ROLE",true,true)).toContain("s-lead");
  expect(instructions(active,"ROLE",true,true)).toContain("显式委托");
  expect(instructions(active,"ROLE",true)).toContain("普通单 agent");
  expect(instructions(active,"ROLE",true)).not.toContain("ROLE");
  expect(readMode({...e,HERDR_PANE_ID:"p2"},r,"/repo").kind).toBe("off");
  unlinkSync(f);
  expect(readMode(e,r,"/repo").kind).toBe("off");
  expect(instructions(readMode(e,r,"/repo"),"",true)).toContain("普通单 agent");
}));
test("partial or corrupted registration does not fall back to implementation", () => fixture((r,e,f,s) => {
  writeFileSync(f,JSON.stringify({...s,phase:"partial"}));
  expect(readMode(e,r,"/repo").kind).toBe("blocked");
  writeFileSync(f,"{");
  expect(readMode(e,r,"/repo").kind).toBe("blocked");
}));
test("other tab/cwd and changing run fenced", () => fixture((r,e,f,s) => {
  writeFileSync(f,JSON.stringify(s));
  expect(readMode(e,r,"/different").kind).toBe("blocked");
  expect(readMode({...e,HERDR_TAB_ID:"t2"},r,"/repo").kind).toBe("off");
  const before=readMode(e,r,"/repo").token;
  writeFileSync(f,JSON.stringify({...s,run_id:"R1"}));
  expect(readMode(e,r,"/repo").token).not.toBe(before);
}));
test("factory outside Herdr registers no hooks", () => {
  const old=process.env.HERDR_ENV;
  delete process.env.HERDR_ENV;
  try { let hooks=0; extension({on(){hooks++;}} as any); expect(hooks).toBe(0); }
  finally { if(old!==undefined)process.env.HERDR_ENV=old; }
});
test("Herdr factory registers only scoped tools and no interception or outgoing prompts", () => {
  const old=process.env.HERDR_ENV; process.env.HERDR_ENV="1";
  try {
    const hooks: string[]=[];
    const tools: string[] = [], commands: string[] = [];
    // Reset remains a user command, never a callable model tool or interception.
    extension({on(name:string){hooks.push(name);}, registerTool(t:any){tools.push(t.name);}, registerCommand(name:string){commands.push(name);}} as any);
    expect(commands).toContain("shop-reset");
    expect(tools).toEqual(["shop_message", "shop_status", "shop_patrol", "shop_dispatch", "shop_handoff"]);
    expect(hooks).toEqual(["session_start","session_shutdown","session_tree","session_tree","session_start","session_shutdown","before_agent_start"]);
    expect(hooks).not.toContain("tool_call");
  } finally { if(old===undefined)delete process.env.HERDR_ENV;else process.env.HERDR_ENV=old; }
});
