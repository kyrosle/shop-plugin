import type { Theme } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";

export type FrameTheme = Pick<Theme, "fg">;

/** ANSI/CJK-aware frame. Caller supplies content rendered at width - 4. */
export function panelFrame(title: string, lines: string[], width: number, theme: FrameTheme): string[] {
  width = Math.max(0, Math.floor(width));
  const singleLine = (text: string) => text.replace(/[\r\n\t]/g, " ");
  title = singleLine(title);
  lines = lines.map(singleLine);
  if (width < 4) return [title, ...lines].map(line => truncateToWidth(line, width, ""));
  const inner = width - 4;
  const heading = truncateToWidth(` ${title} `, width - 2, "");
  const border = (text: string) => theme.fg("border", text);
  return [border(`╭${heading}${"─".repeat(Math.max(0, width - 2 - visibleWidth(heading)))}╮`),
    ...lines.map(line => {
      const content = truncateToWidth(line, inner, "");
      return border("│ ") + content + " ".repeat(Math.max(0, inner - visibleWidth(content))) + border(" │");
    }), border(`╰${"─".repeat(width - 2)}╯`)];
}
