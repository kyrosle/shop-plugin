import type { ExtensionCommandContext } from "@earendil-works/pi-coding-agent";
import { fuzzyFilter, Input, Key, matchesKey, SelectList, truncateToWidth,
  type Component, type Focusable, type KeybindingsManager, type SelectItem, type TuiMouseEvent } from "@earendil-works/pi-tui";
import { panelFrame, type FrameTheme } from "./ui-frame.js";
import { t } from "./i18n.js";

type Model = ReturnType<ExtensionCommandContext["modelRegistry"]["getAvailable"]>[number];
export type ModelChoice = "inherit" | `model:${string}`;
export type ModelPickerOptions = { title: string; models: Model[]; current?: string; parent?: string };

/** Pure draft selector: no model changes, provider calls, or default-setting writes. */
export class ModelPicker implements Component, Focusable {
  private input = new Input({ placeholder: t("Search models or providers…") });
  private list!: SelectList;
  private models: Model[];
  private filtered: SelectItem[] = [];
  private query = "";
  private pageSize = 10;
  private compact = false;
  private listY = 4;
  private inputY = 2;
  get focused(): boolean { return this.input.focused; }
  set focused(value: boolean) { this.input.focused = value; }

  constructor(private options: ModelPickerOptions, private theme: FrameTheme,
    private keys: Pick<KeybindingsManager, "matches">, private done: (choice: ModelChoice | undefined) => void,
    private requestRender: () => void, private terminalRows: () => number = () => 24) {
    this.models = [...options.models].sort((a, b) => `${a.provider}/${a.id}`.localeCompare(`${b.provider}/${b.id}`));
    this.rebuild(options.current ? `model:${options.current}` : "inherit");
  }

  private rebuild(selected?: string): void {
    const models = fuzzyFilter(this.models, this.query, m => `${m.id} ${m.provider} ${m.name ?? ""}`);
    this.filtered = models.map(m => ({ value: `model:${m.provider}/${m.id}`,
      label: `${m.id} [${m.provider}]${`${m.provider}/${m.id}` === this.options.current ? " ✓" : ""}` }));
    if (!this.query.trim()) this.filtered.unshift({ value: "inherit",
      label: t("Inherit parent: {0}", [this.options.parent ?? t("Not configured")]) });
    this.list = new SelectList(this.filtered, this.pageSize, {
      selectedPrefix: s => this.theme.fg("accent", s), selectedText: s => this.theme.fg("accent", s),
      description: s => this.theme.fg("muted", s), scrollInfo: s => this.theme.fg("dim", s),
      noMatch: () => this.theme.fg("warning", t("No matching models")),
    });
    this.list.setSelectedIndex(Math.max(0, this.filtered.findIndex(item => item.value === selected)));
    this.list.onSelect = item => this.done(item.value as ModelChoice);
  }

  invalidate(): void { this.input.invalidate(); this.list.invalidate(); }

  handleInput(data: string): void {
    if (matchesKey(data, Key.escape) || matchesKey(data, Key.ctrl("c")) || this.keys.matches(data, "tui.select.cancel")) {
      this.done(undefined); return;
    }
    if (this.keys.matches(data, "tui.select.confirm")) {
      const item = this.list.getSelectedItem();
      if (item) this.done(item.value as ModelChoice);
      return;
    }
    const index = this.filtered.findIndex(item => item.value === this.list.getSelectedItem()?.value);
    if (this.keys.matches(data, "tui.select.up") || this.keys.matches(data, "tui.select.down")) {
      const delta = this.keys.matches(data, "tui.select.up") ? -1 : 1;
      if (this.filtered.length) this.list.setSelectedIndex((index + delta + this.filtered.length) % this.filtered.length);
    } else if (matchesKey(data, Key.pageUp) || matchesKey(data, Key.pageDown)) {
      this.list.setSelectedIndex(index + (matchesKey(data, Key.pageUp) ? -this.pageSize : this.pageSize));
    } else {
      this.input.handleInput(data);
      if (this.query !== this.input.getValue()) {
        this.query = this.input.getValue(); this.rebuild();
      }
    }
    this.requestRender();
  }

  handleMouse(event: TuiMouseEvent) {
    if (event.type === "wheel" || (event.y >= this.listY && event.y < this.listY + this.pageSize)) {
      return this.list.handleMouse({ ...event, x: event.x - 2, y: event.y - this.listY });
    }
    if (event.y === this.inputY) return this.input.handleMouse({ ...event, x: event.x - 2, y: 0 });
    return undefined;
  }

  render(width: number): string[] {
    const budget = Math.max(1, Math.floor(this.terminalRows() * 0.95));
    this.compact = budget < 12;
    const size = Math.max(1, Math.min(10, budget - (this.compact ? 5 : 9)));
    if (size !== this.pageSize) { this.pageSize = size; this.rebuild(this.list.getSelectedItem()?.value); }
    const inner = Math.max(1, width - 4);
    const items = this.list.render(inner);
    // Reserve scroll-info row even for few/no matches so filtering never jumps.
    while (items.length < this.pageSize + 1) items.push("");
    const selected = this.list.getSelectedItem();
    const model = this.models.find(m => `model:${m.provider}/${m.id}` === selected?.value);
    const detail = model ? t("Model name: {0}", [model.name ?? model.id])
      : selected?.value === "inherit" ? t("Inherit parent: {0}", [this.options.parent ?? t("Not configured")]) : t("No matching models");
    const hint = this.theme.fg("dim", t("↑↓ scroll · Enter choose · Esc back · Clear search to inherit"));
    this.inputY = this.compact ? 1 : 2;
    this.listY = this.compact ? 2 : 4;
    const lines = this.compact ? [...this.input.render(inner), ...items, hint]
      : [this.theme.fg("muted", t("Available providers only · Selection changes the Shop draft, not Pi defaults")),
        ...this.input.render(inner), "", ...items, "", this.theme.fg("muted", detail), hint];
    return panelFrame(this.options.title, lines.map(line => truncateToWidth(line, inner, "")), width, this.theme).slice(0, budget);
  }
}

export async function selectModel(ctx: Pick<ExtensionCommandContext, "ui">, options: ModelPickerOptions,
  signal: AbortSignal): Promise<ModelChoice | undefined> {
  if (signal.aborted) return undefined;
  let detach: (() => void) | undefined;
  try {
    return await ctx.ui.custom<ModelChoice | undefined>((tui, theme, keys, done) => {
      let settled = false;
      const finish = (choice: ModelChoice | undefined) => {
        if (settled) return;
        settled = true; detach?.(); done(choice);
      };
      const abort = () => finish(undefined);
      detach = () => signal.removeEventListener("abort", abort);
      signal.addEventListener("abort", abort, { once: true });
      const picker = new ModelPicker(options, theme, keys, finish, () => tui.requestRender(), () => tui.terminal.rows);
      if (signal.aborted) abort();
      return picker;
    }, { overlay: true, overlayOptions: { width: "85%", maxHeight: "95%", anchor: "center" } });
  } finally { detach?.(); }
}
