import {
  Children,
  isValidElement,
  memo,
  useId,
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
const MAX_LINK_TEXT_LENGTH = 2048;
// Unicode full stops that the URL host parser maps to "." (UTS 46).
const HOST_LABEL_SEPARATOR = /[.\u3002\uFF0E\uFF61]/u;
// Link text without a scheme is a host candidate only when its top-level label
// is one of these common web labels or a punycode (xn--) label, however many
// labels it has. A port, path, query, or fragment after the name does not by
// itself make it a host, so file names, file:line references, and dotted code
// identifiers such as setup.py:42, README.md#install, vite.config.ts, and
// foo.bar.baz stay unmarked. The list leaves out labels that double as common
// file extensions, such as md, py, sh, ts, and rs. Text with an explicit
// scheme is always parsed as a URL.
const WEB_TOP_LABELS: ReadonlySet<string> = new Set([
  "com",
  "org",
  "net",
  "edu",
  "gov",
  "mil",
  "int",
  "io",
  "dev",
  "app",
  "ai",
  "co",
  "me",
  "info",
  "biz",
  "cloud",
  "tech",
  "xyz",
  "site",
  "online",
  "page",
  "us",
  "uk",
  "de",
  "fr",
  "eu",
  "ca",
  "au",
  "jp",
  "cn",
  "ru",
  "br",
  "nl",
  "ch",
  "se",
  "es",
  "example",
]);
// Punctuation that commonly wraps a URL inside prose, such as "(github.com)".
const WRAPPING_PUNCTUATION = /^[("'<[{]+|[)"'>\]},;:!?.]+$/gu;

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
  const descriptionId = useId();
  const host = externalLinkHost(destination);
  const mismatched = linkTextNamesOtherOrigin(linkText(children), destination);
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
        aria-describedby={descriptionId}
        className="safe-markdown-link"
        data-link-host={host}
        disabled={opening}
        onClick={(event) => void activate(event)}
        role="link"
        title={destination}
        type="button"
      >
        {children}
      </button>
      {mismatched ? (
        <span
          className="safe-markdown-link-host safe-markdown-link-host-mismatch"
          id={descriptionId}
        >
          (opens {host})
        </span>
      ) : (
        <span className="safe-markdown-link-host" hidden id={descriptionId}>
          Opens {host}
        </span>
      )}
      {failed && (
        <span className="safe-markdown-link-error" role="status">
          Could not open link.
        </span>
      )}
    </>
  );
}

/** Returns the host shown for an admitted destination, in its ASCII form. */
export function externalLinkHost(destination: string): string {
  try {
    return new URL(destination).host;
  } catch {
    return destination;
  }
}

/**
 * Reports whether link text reads as a URL or host naming a different host
 * (hostname plus effective port) than the destination, so a spoofed forge
 * address can be flagged before activation. The scheme is not compared, so
 * http://github.com/x text for an https://github.com/x destination is not
 * flagged. Text longer than the inspection limit fails closed and is flagged.
 */
export function linkTextNamesOtherOrigin(
  text: string,
  destination: string,
): boolean {
  let target: URL;
  try {
    target = new URL(destination);
  } catch {
    return false;
  }
  const trimmed = text.trim();
  if (trimmed.length === 0) return false;
  if (trimmed.length > MAX_LINK_TEXT_LENGTH) return true;
  // The text is flagged when any word, stripped of wrapping punctuation such as
  // "(github.com)" or a trailing "github.com.", reads as a URL or host on a
  // different host.
  return trimmed
    .split(/\s+/u)
    .map((word) => word.replace(WRAPPING_PUNCTUATION, ""))
    .some((word) => word.length > 0 && tokenNamesOtherHost(word, target));
}

function tokenNamesOtherHost(token: string, target: URL): boolean {
  if (/^[a-z][a-z0-9+.-]*:\/\//iu.test(token)) {
    try {
      return new URL(token).host !== target.host;
    } catch {
      return false;
    }
  }
  return schemelessTextNamesOtherHost(token, target);
}

// Link text without a scheme is parsed as an https URL so the host parser
// applies IDNA mapping: homoglyphs become punycode, Unicode full stops become
// dots, and ignorable characters are removed before the hosts are compared.
function schemelessTextNamesOtherHost(text: string, target: URL): boolean {
  const hostPart = text.split(/[/?#]/u, 1)[0] ?? "";
  let parsed: URL;
  try {
    parsed = new URL(`https://${text}`);
  } catch {
    // Unparseable text still gets the marker when its top-level label, with
    // any port or trailing symbols removed, reads like a web or non-ASCII one.
    const labels = hostPart.replace(/:[^:]*$/u, "").split(HOST_LABEL_SEPARATOR);
    if (labels.length < 2) return false;
    const topLabel = /^[\p{L}\p{N}-]*/u.exec(
      (labels[labels.length - 1] ?? "").normalize("NFKC").toLowerCase(),
    )?.[0];
    return (
      topLabel !== undefined &&
      (isWebTopLabel(topLabel) || /[^\x00-\x7f]/u.test(topLabel))
    );
  }
  // Userinfo that itself reads like a dotted name, as in
  // "github.com@evil.example", disguises the real host.
  if (parsed.username.includes(".")) return true;
  const labels = parsed.hostname.split(".");
  if (labels.length < 2) return false;
  if (!isWebTopLabel(labels[labels.length - 1] ?? "")) return false;
  return parsed.host !== target.host;
}

function isWebTopLabel(label: string): boolean {
  return WEB_TOP_LABELS.has(label) || /^xn--[a-z0-9-]+$/u.test(label);
}

function linkText(children: ReactNode): string {
  let text = "";
  const stack: ReactNode[] = [children];
  while (stack.length > 0 && text.length <= MAX_LINK_TEXT_LENGTH) {
    const node = stack.pop();
    if (typeof node === "string" || typeof node === "number") {
      text += String(node);
    } else if (Array.isArray(node)) {
      const items = Children.toArray(node as ReactNode);
      for (let index = items.length - 1; index >= 0; index -= 1) {
        stack.push(items[index]);
      }
    } else if (isValidElement<{ alt?: unknown; children?: ReactNode }>(node)) {
      // An image inside a link renders its alt text as "[Image: <alt>]".
      if (typeof node.props.alt === "string") text += ` ${node.props.alt} `;
      stack.push(node.props.children);
    }
  }
  return text;
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

export function safeMarkdownPresentationBytes(source: string): number {
  const presented = utf8Exceeds(source, MAX_INPUT_BYTES)
    ? boundedPreview(source).preview
    : source;
  return utf8ByteLength(presented);
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

function utf8ByteLength(source: string): number {
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
  }
  return bytes;
}
