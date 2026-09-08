export type MarkdownBlock =
  | { readonly kind: "heading"; readonly level: number; readonly text: string }
  | { readonly kind: "paragraph" | "list" | "code"; readonly text: string };

export function parseInertMarkdown(source: string): readonly MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  let paragraph: string[] = [];
  let code: string[] | null = null;
  const flush = (): void => {
    if (paragraph.length)
      blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
    paragraph = [];
  };
  for (const line of source.replaceAll("\r\n", "\n").split("\n")) {
    if (line.startsWith("```")) {
      if (code === null) {
        flush();
        code = [];
      } else {
        blocks.push({ kind: "code", text: code.join("\n") });
        code = null;
      }
      continue;
    }
    if (code !== null) {
      code.push(line);
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      flush();
      blocks.push({
        kind: "heading",
        level: heading[1]?.length ?? 1,
        text: heading[2] ?? "",
      });
    } else if (/^[-*]\s+/.test(line)) {
      flush();
      blocks.push({ kind: "list", text: line.replace(/^[-*]\s+/, "") });
    } else if (!line.trim()) flush();
    else paragraph.push(line.trim());
  }
  if (code !== null) blocks.push({ kind: "code", text: code.join("\n") });
  flush();
  return Object.freeze(blocks);
}
