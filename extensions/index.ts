import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { registerLanguageCommand } from "./language-ui.js";
import { registerSeats } from "./seats.js";
import { editConfiguration } from "./settings-ui.js";

/** Shop: ephemeral seats (Architect → Lead → Workers) and their model configuration. */
export default function shop(pi: ExtensionAPI) {
  // Lead/Worker seats get only their seat tools.
  if (process.env.SHOP_SEAT_ROLE) { registerSeats(pi); return; }
  registerLanguageCommand(pi);
  pi.registerCommand("shop-config", {
    description: "Shop settings / 席位配置: Lead and Worker models and thinking (global, project, session)",
    handler: async (args, ctx) => {
      try { await editConfiguration(pi, ctx, args.trim()); } catch (error) { ctx.ui.notify(String(error), "error"); }
    },
  });
  // Seats open Herdr panes, so /shop-go and /shop-spec exist only inside Herdr.
  if (process.env.HERDR_ENV === "1") registerSeats(pi);
}
