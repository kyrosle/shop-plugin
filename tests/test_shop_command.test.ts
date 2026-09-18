import { test, expect } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import extension from "../extensions/index.ts";

test("explicit command delegates once; ordinary next request stays local; busy refused", async () => {
  const dir = mkdtempSync(join(tmpdir(), "shop-command-"));
  const old = { ...process.env };
  Object.assign(process.env, { HERDR_ENV: "1", HERDR_TAB_ID: "t", HERDR_PANE_ID: "p", HERDR_SOCKET_PATH: "/socket", SHOP_LOCATOR: join(dir,"bridge.json") });
  try {
    mkdirSync(join(dir,"runtime")); mkdirSync(join(dir,"roles"));
    writeFileSync(join(dir,"roles/architect.md"), "EXPLICIT_ROLE");
    writeFileSync(join(dir,"bridge.json"), JSON.stringify({protocol:1,core_root:dir,state_dir:dir,config_dir:dir}));
    const key = createHash("sha256").update("/socket:t").digest("hex").slice(0,12);
    writeFileSync(join(dir,"runtime",key+".json"),JSON.stringify({tab:"t",cwd:"/repo",phase:"ready",shop_id:"s",architect:{pane:"p"},lead:{name:"lead",pane:"l"}}));
    const hooks:any = {}, commands:any = {}; const sent:string[] = [];
    extension({on:(n:string,h:any)=>hooks[n]=h,registerCommand:(n:string,c:any)=>commands[n]=c,registerTool:()=>{},sendUserMessage:(s:string)=>sent.push(s)} as any);
    let idle=true;
    const ctx:any={cwd:"/repo",hasUI:true,isIdle:()=>idle,ui:{notify:()=>{},setStatus:()=>{}}};
    const ordinary=hooks.before_agent_start({prompt:"fix code",systemPrompt:"BASE"},ctx);
    expect(ordinary.systemPrompt).toContain("普通单 agent");
    await commands.shop.handler("analyze project",ctx);
    expect(sent).toHaveLength(1);
    expect(hooks.before_agent_start({prompt:sent[0],systemPrompt:"BASE"},ctx).systemPrompt).toContain("EXPLICIT_ROLE");
    expect(hooks.before_agent_start({prompt:"next ordinary",systemPrompt:"BASE"},ctx).systemPrompt).not.toContain("EXPLICIT_ROLE");
    idle=false; await commands.shop.handler("another",ctx); expect(sent).toHaveLength(1);
    idle=true; await commands.shop.handler("",ctx); expect(sent).toHaveLength(1);
  } finally {
    for(const key of Object.keys(process.env)) if(!(key in old)) delete process.env[key];
    Object.assign(process.env,old);rmSync(dir,{recursive:true,force:true});
  }
});
