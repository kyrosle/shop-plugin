import { test, expect } from "bun:test";
import { CURSOR_MARKER, getKeybindings, visibleWidth } from "@earendil-works/pi-tui";
import { ModelPicker, selectModel, type ModelChoice } from "../extensions/model-picker.ts";
import { panelFrame } from "../extensions/ui-frame.ts";

const theme = { fg: (_: string, text: string) => text } as any;
const models = Array.from({ length: 100 }, (_, index) => ({ provider: "vendor-a", id: `model-${index.toString().padStart(3, "0")}`,
  name: `Model ${index}`, api: "openai-completions", reasoning: true })) as any[];
const duplicate = { ...models[0], provider: "vendor-b", name: "Needle Fast 模型" };
function picker(overrides: any = {}) {
  const chosen: Array<ModelChoice | undefined> = [];
  const component = new ModelPicker({ title: "Worker model", models: [...models, duplicate],
    parent: "vendor-a/parent", ...overrides }, theme, getKeybindings(), value => chosen.push(value), () => {}, overrides.rows ?? (() => 24));
  return { component, chosen };
}

test("frame pads styled Unicode text without line breaks or width overflow", () => {
  const colors: any = { fg: (_: string, text: string) => `\x1b[34m${text}\x1b[0m` };
  for (const width of [0, 1, 3, 4, 8, 40, 100]) {
    const lines = panelFrame("模型 🔎 标题", ["\x1b[35m当前模型\x1b[0m", "name\nsecond\tpart"], width, colors);
    for (const line of lines) {
      expect(visibleWidth(line)).toBeLessThanOrEqual(width);
      expect(line).not.toMatch(/[\r\n\t]/);
      if (width >= 4) expect(visibleWidth(line)).toBe(width);
    }
  }
});

test("model search uses fuzzy tokens across id, provider and display name; duplicate ids stay distinct", () => {
  const { component, chosen } = picker();
  const height = component.render(100).length;
  component.handleInput("vndr-b needle");
  const text = component.render(100).join("\n");
  expect(text).toContain("model-000 [vendor-b]");
  expect(text).toContain("Needle Fast 模型");
  expect(text).not.toContain("model-000 [vendor-a]");
  expect(component.render(100)).toHaveLength(height);
  component.handleInput("\r");
  expect(chosen).toEqual(["model:vendor-b/model-000"]);
});

test("fixed-height viewport scrolls all models; resize keeps current selection visible", () => {
  let rows = 24;
  const { component, chosen } = picker({ current: "vendor-a/model-090", rows: () => rows });
  let text = component.render(100).join("\n");
  expect(text).toContain("model-090 [vendor-a] ✓");
  expect(text).not.toContain("model-000 [vendor-a]");
  expect(text).not.toContain("model-099 [vendor-a]");
  const height = component.render(100).length;
  for (let i = 0; i < 5; i++) component.handleInput("\x1b[B");
  expect(component.render(100)).toHaveLength(height);
  expect(component.render(100).join("\n")).toContain("model-095");
  rows = 12;
  expect(component.render(50).length).toBeLessThanOrEqual(Math.floor(rows * 0.95));
  expect(component.render(50).join("\n")).toContain("model-095");
  rows = 24;
  component.render(100);
  component.handleInput("\r");
  expect(chosen).toEqual(["model:vendor-a/model-095"]);
});

test("page navigation and mouse wheel scroll inside the bounded viewport", () => {
  const { component, chosen } = picker();
  const height = component.render(100).length;
  component.handleInput("\x1b[6~"); // PageDown
  expect(component.render(100)).toHaveLength(height);
  expect(component.render(100).join("\n")).toContain("model-009");
  component.handleMouse({ type: "wheel", wheelDelta: 1, x: 5, y: 5 } as any);
  component.handleInput("\r");
  expect(chosen).toEqual(["model:vendor-a/model-010"]);
});

test("no matches cannot choose an arbitrary model; clearing search restores inheritance", () => {
  const { component, chosen } = picker();
  const height = component.render(90).length;
  component.handleInput("zzzz-impossible-xyz");
  expect(component.render(90)).toHaveLength(height);
  component.handleInput("\r");
  expect(chosen).toEqual([]);
  component.handleInput("\x15"); // Ctrl+U clears the search, not draft settings.
  expect(component.render(90).join("\n")).toContain("vendor-a/parent");
  component.handleInput("\r");
  expect(chosen).toEqual(["inherit"]);
});

test("empty catalogue still offers inheritance; Esc/Ctrl+C cancel; Ctrl+S never saves", () => {
  const { component, chosen } = picker({ models: [] });
  component.handleInput("\x13");
  expect(chosen).toEqual([]);
  component.handleInput("\r");
  expect(chosen).toEqual(["inherit"]);
  for (const input of ["\x1b", "\x03"]) {
    const next = picker(); next.component.handleInput(input);
    expect(next.chosen).toEqual([undefined]);
  }
});

test("IME focus reaches search Input; Unicode and ANSI stay bounded at narrow widths", () => {
  const { component } = picker({ title: "工作席位 模型 🔎", models: [duplicate] });
  component.focused = true;
  component.handleInput("\x1b[200~模型\x1b[201~");
  expect(component.render(80).join("\n")).toContain(CURSOR_MARKER);
  component.focused = false;
  expect(component.render(80).join("\n")).not.toContain(CURSOR_MARKER);
  for (const width of [0, 1, 3, 4, 8, 24, 40, 80]) {
    for (const line of component.render(width)) expect(visibleWidth(line)).toBeLessThanOrEqual(width);
  }
});

test("injected navigation bindings route to the list, not the search field", () => {
  const chosen: any[] = [];
  const keys: any = { matches: (data: string, id: string) => data === ({
    "tui.select.down": "next", "tui.select.confirm": "choose",
  } as any)[id] };
  const component = new ModelPicker({ title: "Model", models: [duplicate] }, theme, keys, c => chosen.push(c), () => {});
  component.handleInput("next"); component.handleInput("choose");
  expect(chosen).toEqual(["model:vendor-b/model-000"]);
});

test("abort closes a pending picker exactly once; pre-aborted requests open nothing", async () => {
  const controller = new AbortController();
  let component!: ModelPicker;
  let opened = 0, completions = 0;
  const ctx: any = { ui: { custom: (factory: any) => {
    opened++;
    return new Promise(resolve => {
      component = factory({ terminal: { rows: 24 }, requestRender() {} }, theme, getKeybindings(), (value: any) => {
        completions++; resolve(value);
      });
    });
  } } };
  const pending = selectModel(ctx, { title: "Model", models }, controller.signal);
  controller.abort();
  expect(await pending).toBeUndefined();
  component.handleInput("\r");
  expect(completions).toBe(1);
  expect(await selectModel(ctx, { title: "Model", models }, controller.signal)).toBeUndefined();
  expect(opened).toBe(1);
});
