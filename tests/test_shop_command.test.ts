import { test, expect } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import extension from "../extensions/index.ts";

async function fixture(run: (f: any) => Promise<void>) {
  const dir = mkdtempSync(join(tmpdir(), "shop-command-")), old = { ...process.env };
  Object.assign(process.env, { HERDR_ENV: "1", HERDR_TAB_ID: "t", HERDR_PANE_ID: "p", HERDR_SOCKET_PATH: "/socket", SHOP_LOCATOR: join(dir,"bridge.json") });
  try {
    mkdirSync(join(dir,"runtime")); mkdirSync(join(dir,"roles"));
    writeFileSync(join(dir,"roles/architect.md"), "EXPLICIT_ROLE");
    writeFileSync(join(dir,"bridge.json"), JSON.stringify({protocol:1,core_root:dir,state_dir:dir,config_dir:dir}));
    const key = createHash("sha256").update("/socket:t").digest("hex").slice(0,12);
    const file = join(dir,"runtime",key+".json");
    const state = {tab:"t",cwd:"/repo",phase:"ready",shop_id:"s",architect:{pane:"p"},lead:{name:"lead",pane:"l"},
      lifecycle:{version:1,session_id:"session",root:"/repo",socket:"/socket"}};
    writeFileSync(file,JSON.stringify(state));
    const f: any = { file, state, idle:true, session:"session", sent:[], notices:[], statuses:[], calls:0,
      hooks:{}, commands:{}, reason:undefined, change:()=>{} };
    f.ctx = {cwd:"/repo",hasUI:true,isIdle:()=>f.idle,
      sessionManager:{getSessionId:()=>f.session},
      ui:{notify:(s:string)=>f.notices.push(s),setStatus:(_key:string,s:string)=>f.statuses.push(s)}};
    extension({on:(n:string,h:any)=>f.hooks[n]=h,registerCommand:(n:string,c:any)=>f.commands[n]=c,registerTool:()=>{},
      sendUserMessage:(s:string)=>f.sent.push(s), exec:async (_bin:string,args:string[])=>{
        expect(args.slice(1)).toEqual(["preflight","--session-id",f.session]);
        f.calls++;
        const proof = {decision:f.reason ? "blocked" : "ready",reason:f.reason,shop_id:"s",run_id:null,
          session_id:f.session,state_revision:createHash("sha256").update(readFileSync(file)).digest("hex"),expires_at:Date.now()+15000};
        f.change();
        return {code:0,stdout:JSON.stringify(proof),stderr:"",killed:false};
      }} as any);
    await run(f);
  } finally {
    for(const key of Object.keys(process.env)) if(!(key in old)) delete process.env[key];
    Object.assign(process.env,old);rmSync(dir,{recursive:true,force:true});
  }
}

test("explicit command verifies live state before one delegation; ordinary stays local; busy refused", () => fixture(async f => {
  const ordinary=f.hooks.before_agent_start({prompt:"fix code",systemPrompt:"BASE"},f.ctx);
  expect(ordinary.systemPrompt).toContain("普通单 agent");
  expect(f.calls).toBe(0);
  expect(f.statuses.at(-1)).not.toContain("SHOP ready");
  await f.commands.shop.handler("analyze project",f.ctx);
  expect(f.calls).toBe(1); expect(f.sent).toHaveLength(1);
  expect(f.hooks.before_agent_start({prompt:f.sent[0],systemPrompt:"BASE"},f.ctx).systemPrompt).toContain("EXPLICIT_ROLE");
  expect(f.hooks.before_agent_start({prompt:"next ordinary",systemPrompt:"BASE"},f.ctx).systemPrompt).not.toContain("EXPLICIT_ROLE");
  f.idle=false; await f.commands.shop.handler("another",f.ctx); expect(f.sent).toHaveLength(1);
  f.idle=true; await f.commands.shop.handler("",f.ctx); expect(f.sent).toHaveLength(1);
}));

test("failed native verification spends no model turn and cannot present ready", () => fixture(async f => {
  f.reason="Lead moved to another tab";
  await f.commands.shop.handler("do work",f.ctx);
  expect(f.sent).toHaveLength(0);
  expect(f.notices.join(" ")).toContain("Lead moved");
  expect(f.statuses.at(-1)).not.toContain("SHOP ready");
}));

test("recovery flag and session change block before native preflight or model dispatch", () => fixture(async f => {
  writeFileSync(f.file,JSON.stringify({...f.state,recovery_required:true}));
  await f.commands.shop.handler("do work",f.ctx);
  expect(f.sent).toHaveLength(0); expect(f.calls).toBe(0);
  writeFileSync(f.file,JSON.stringify(f.state)); f.session="another-session";
  await f.commands.shop.handler("do work",f.ctx);
  expect(f.sent).toHaveLength(0); expect(f.calls).toBe(0);
}));

test("asynchronous identity drift and queued registration changes do not authorize old Shop", () => fixture(async f => {
  f.change=()=>{ f.session="other"; };
  await f.commands.shop.handler("do work",f.ctx); expect(f.sent).toHaveLength(0);
  f.session="session"; f.change=()=>{};
  await f.commands.shop.handler("do work",f.ctx); expect(f.sent).toHaveLength(1);
  writeFileSync(f.file,JSON.stringify({...f.state,recovery_required:true}));
  const result=f.hooks.before_agent_start({prompt:f.sent[0],systemPrompt:"BASE"},f.ctx);
  expect(result.systemPrompt).not.toContain("EXPLICIT_ROLE");
}));
