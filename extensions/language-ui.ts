import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { join } from "node:path";
import { readBridge } from "./bridge.js";
import { LANGUAGES, readPreference, resolveLanguage, selectAction, t, type LanguagePreference } from "./i18n.js";

export function registerLanguageCommand(pi: ExtensionAPI): void {
  pi.registerCommand("shop-language", {
    description: "Shop language / 界面语言: auto, zh-CN, en",
    getArgumentCompletions: prefix => LANGUAGES.filter(value => value.startsWith(prefix)).map(value => ({ value, label: value })),
    handler: async (args, ctx) => {
      try {
        const current = readPreference();
        const bridge = readBridge();
        const session = ctx.sessionManager.getSessionId();
        let choice = args.trim();
        if (!choice) {
          if (!ctx.hasUI) return;
          choice = await selectAction(ctx, t("Shop language: {0} → {1}", [current.language, resolveLanguage()]), [
            ["auto", "Auto / 跟随系统"], ["zh-CN", "简体中文"], ["en", "English"],
          ]) ?? "";
          if (!choice) return;
        }
        if (!LANGUAGES.includes(choice as LanguagePreference)) {
          ctx.ui.notify(t("Usage: /shop-language [auto|zh-CN|en]"), "warning"); return;
        }
        if (ctx.sessionManager.getSessionId() !== session || JSON.stringify(readBridge()) !== JSON.stringify(bridge))
          throw new Error(t("Session or bridge changed; language not saved"));
        if (!bridge) throw new Error(t("Shop bridge unconfigured; run core/plugin.py configure first"));
        const result = await pi.exec("python3", [join(bridge.core_root, "core/language.py"), choice,
          "--expected", current.revision, "--expected-path", current.path], { timeout: 5000 });
        if (result.code || result.killed)
          throw new Error(t("Language save failed or outcome unknown; inspect before retry") + "\n" + (result.stderr || result.stdout).slice(0, 2000));
        // Python writes only language.json under its own CAS lock. No model publisher or transport restart.
        ctx.ui.notify(t("Language saved: {0}. Reopen panels; tasks and models unchanged.", [choice]), "info");
      } catch (error) { ctx.ui.notify(String(error), "error"); }
    },
  });
}
