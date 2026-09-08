import {
  memo,
  useMemo,
  useState,
  type MouseEvent,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// Dense link delimiters make synchronous CommonMark parsing nonlinear. Bounded
// child measurements take about 60 ms at this ceiling and seconds at 64 KiB.
const MAX_INPUT_BYTES = 4 * 1024;
const MAX_AST_NODES = 4096;
const MAX_AST_DEPTH = 32;
const MAX_PREVIEW_CODE_POINTS = 4096;
const MAX_EXTERNAL_URL_LENGTH = 4096;

const ALLOWED_ELEMENTS = Object.freeze([
  "p",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "em",
  "strong",
  "del",
  "blockquote",
  "ol",
  "ul",
  "li",
  "br",
  "hr",
  "pre",
  "code",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "input",
  "section",
  "sup",
  "a",
  "img",
]);

interface MarkdownAstNode {
  readonly children?: unknown;
}

export interface SafeMarkdownProps {
  readonly source: string;
  readonly openExternal: (url: string) => Promise<boolean>;
}

function astLimitPlugin(): (tree: unknown) => void {
  return (tree: unknown): void => {
    const stack: Array<{ readonly node: unknown; readonly depth: number }> = [
      { node: tree, depth: 0 },
    ];
    let nodes = 0;
    while (stack.length > 0) {
      const current = stack.pop();
      if (!current) break;
      nodes += 1;
      if (nodes > MAX_AST_NODES || current.depth > MAX_AST_DEPTH) {
        throw new Error("Markdown structure exceeds the safe rendering limit");
      }
      if (!isMarkdownAstNode(current.node)) continue;
      const children = current.node.children;
      if (!Array.isArray(children)) continue;
      if (children.length > MAX_AST_NODES - nodes - stack.length) {
        throw new Error("Markdown structure exceeds the safe rendering limit");
      }
      for (let index = children.length - 1; index >= 0; index -= 1) {
        stack.push({ node: children[index], depth: current.depth + 1 });
      }
    }
  };
}

const REMARK_PLUGINS = [remarkGfm, astLimitPlugin];
Object.freeze(REMARK_PLUGINS);

export const SafeMarkdown = memo(function SafeMarkdown({
  source,
  openExternal,
}: SafeMarkdownProps): ReactNode {
  const components = useMemo<Components>(
    () => ({
      a: ({ children, href }) => {
        const destination = admitMarkdownExternalUrl(href ?? "");
        return destination ? (
          <SafeExternalLink destination={destination} openExternal={openExternal}>
            {children}
          </SafeExternalLink>
        ) : (
          <span className="safe-markdown-link-inert">{children}</span>
        );
      },
      img: ({ alt }) => (
        <span className="safe-markdown-image-placeholder">
          [Image: {alt || "image omitted"}]
        </span>
      ),
      input: ({ checked, type }) =>
        type === "checkbox" ? (
          <input
            aria-label={checked ? "Completed task" : "Incomplete task"}
            checked={Boolean(checked)}
            disabled
            readOnly
            type="checkbox"
          />
        ) : null,
    }),
    [openExternal],
  );

  if (utf8Exceeds(source, MAX_INPUT_BYTES)) {
    return (
      <MarkdownFallback
        reason="Markdown exceeds the safe input limit"
        source={source}
      />
    );
  }
  try {
    return (
      <div className="safe-markdown">
        {ReactMarkdown({
          allowedElements: ALLOWED_ELEMENTS,
          children: source,
          components,
          remarkPlugins: REMARK_PLUGINS,
          skipHtml: false,
          unwrapDisallowed: false,
          urlTransform: transformMarkdownUrl,
        })}
      </div>
    );
  } catch {
    return (
      <MarkdownFallback
        reason="Markdown structure exceeds the safe rendering limit"
        source={source}
      />
    );
  }
});

function SafeExternalLink({
  children,
  destination,
  openExternal,
}: {
  readonly children: ReactNode;
  readonly destination: string;
  readonly openExternal: (url: string) => Promise<boolean>;
}): ReactNode {
  const [opening, setOpening] = useState(false);
  const [failed, setFailed] = useState(false);
  const activate = async (event: MouseEvent<HTMLButtonElement>): Promise<void> => {
    event.preventDefault();
    if (opening) return;
    setOpening(true);
    setFailed(false);
    try {
      if (!(await openExternal(destination))) setFailed(true);
    } catch {
      setFailed(true);
    } finally {
      setOpening(false);
    }
  };
  return (
    <>
      <button
        className="safe-markdown-link"
        disabled={opening}
        onClick={(event) => void activate(event)}
        role="link"
        type="button"
      >
        {children}
      </button>
      {failed && (
        <span className="safe-markdown-link-error" role="status">
          Could not open link.
        </span>
      )}
    </>
  );
}

function MarkdownFallback({
  reason,
  source,
}: {
  readonly reason: string;
  readonly source: string;
}): ReactNode {
  const { preview, omitted } = boundedPreview(source);
  return (
    <div className="safe-markdown safe-markdown-fallback" role="status">
      <p>{reason}. Showing a plain-text preview.</p>
      <pre>{preview}</pre>
      {omitted && <p>Additional content was omitted from this preview.</p>}
    </div>
  );
}

function boundedPreview(source: string): {
  readonly preview: string;
  readonly omitted: boolean;
} {
  let preview = "";
  let points = 0;
  for (const character of source) {
    if (points === MAX_PREVIEW_CODE_POINTS) {
      return { preview, omitted: true };
    }
    preview += character;
    points += 1;
  }
  return { preview, omitted: false };
}

function transformMarkdownUrl(url: string, key: string): string {
  if (key !== "href") return "";
  return admitMarkdownExternalUrl(url) ?? "";
}

export function admitMarkdownExternalUrl(value: string): string | null {
  if (
    value.length === 0 ||
    value.length > MAX_EXTERNAL_URL_LENGTH ||
    /[\s\u0000-\u001f\u007f]/u.test(value)
  ) {
    return null;
  }
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol !== "https:" ||
      parsed.username.length > 0 ||
      parsed.password.length > 0
    ) {
      return null;
    }
    return parsed.href;
  } catch {
    return null;
  }
}

function isMarkdownAstNode(value: unknown): value is MarkdownAstNode {
  return typeof value === "object" && value !== null;
}

function utf8Exceeds(source: string, limit: number): boolean {
  let bytes = 0;
  for (let index = 0; index < source.length; index += 1) {
    const code = source.charCodeAt(index);
    if (code <= 0x7f) bytes += 1;
    else if (code <= 0x7ff) bytes += 2;
    else if (
      code >= 0xd800 &&
      code <= 0xdbff &&
      index + 1 < source.length &&
      source.charCodeAt(index + 1) >= 0xdc00 &&
      source.charCodeAt(index + 1) <= 0xdfff
    ) {
      bytes += 4;
      index += 1;
    } else bytes += 3;
    if (bytes > limit) return true;
  }
  return false;
}
