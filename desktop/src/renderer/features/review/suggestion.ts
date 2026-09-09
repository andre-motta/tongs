import type { RepositoryDto } from "../../../shared/bridge.js";
import type { MutationDiffAnchorDto } from "../../../shared/review.js";
import type { InlineAnchorSelection } from "../../core/navigation.js";
import type { DraftAnchorSelection } from "./state.js";

export type SuggestionForge = RepositoryDto["forge_type"];

export interface PreparedSuggestionTarget {
  readonly originalCode: string;
  readonly originalLineCount: number;
  readonly mutationAnchor: MutationDiffAnchorDto;
  readonly draftSelection: DraftAnchorSelection;
}

export function computeSuggestionFence(code: string): string {
  let longest = 0;
  let current = 0;
  for (const character of code) {
    if (character === "`") {
      current += 1;
      longest = Math.max(longest, current);
    } else {
      current = 0;
    }
  }
  return "`".repeat(Math.max(3, longest + 1));
}

export function formatSuggestionBody(
  replacement: string,
  originalLineCount: number,
  forge: SuggestionForge,
  comment = "",
): string {
  if (!Number.isInteger(originalLineCount) || originalLineCount < 1)
    throw new Error("A suggestion must replace at least one source line");
  const fence = computeSuggestionFence(replacement);
  const directive =
    forge === "gitlab"
      ? `suggestion:-0+${originalLineCount - 1}`
      : "suggestion";
  const block = `${fence}${directive}\n${replacement}\n${fence}`;
  const note = comment.trim();
  return note ? `${note}\n\n${block}` : block;
}

export function suggestionDisabledReason(
  selection: InlineAnchorSelection | null,
  forge: SuggestionForge | null,
): string | null {
  if (!selection) return "Select a source line in the current review diff.";
  if (!forge) return "The selected review repository is unavailable.";
  if (selection.side !== "new")
    return "Suggestions can replace lines on the new side only.";
  if (!selection.contextComplete)
    return "The selected diff is partial. Refresh the complete diff before suggesting.";
  const lines = selection.selectedLines;
  if (!lines || lines.length === 0)
    return "Reselect source code in the diff to capture the original text.";
  let previous: number | null = null;
  for (const line of lines) {
    if (
      line.newLine === null ||
      line.newLine <= 0 ||
      line.lineType === "deletion" ||
      line.lineType === "no_newline"
    )
      return "The selection must contain only new-side source lines.";
    if (previous !== null && line.newLine !== previous + 1)
      return "The selection must contain contiguous new-side source lines.";
    previous = line.newLine;
  }
  return null;
}

export function prepareSuggestionTarget(
  selection: InlineAnchorSelection,
  forge: SuggestionForge,
): PreparedSuggestionTarget {
  const reason = suggestionDisabledReason(selection, forge);
  if (reason) throw new Error(reason);
  const lines = selection.selectedLines!;
  const first = lines[0]!;
  const last = lines.at(-1)!;
  const anchorLine = forge === "github" ? last : first;
  const multiline = lines.length > 1;
  const mutationAnchor: MutationDiffAnchorDto = Object.freeze({
    old_path: selection.oldPath,
    new_path: selection.newPath,
    line: anchorLine.newLine!,
    side: "RIGHT",
    ...(forge === "github" && multiline
      ? { start_line: first.newLine!, start_side: "RIGHT" as const }
      : {}),
  });
  const draftSelection: DraftAnchorSelection = Object.freeze({
    review: selection.review,
    revision: Object.freeze({ ...selection.revision }),
    oldPath: selection.oldPath,
    newPath: selection.newPath,
    side: "new",
    oldLine: anchorLine.oldLine,
    newLine: anchorLine.newLine,
    startLine: forge === "github" && multiline ? first.newLine : null,
    startSide: forge === "github" && multiline ? "new" : null,
    contextLines: Object.freeze([...selection.contextLines]),
    contextComplete: selection.contextComplete,
  });
  return Object.freeze({
    originalCode: lines.map((line) => line.content).join("\n"),
    originalLineCount: lines.length,
    mutationAnchor,
    draftSelection,
  });
}

/**
 * A fenced suggestion block, in either forge's syntax. A body that already
 * carries one must not be wrapped in another: the second fence would open
 * inside the first block and the reader would lose both.
 */
const SUGGESTION_BLOCK = /^`{3,}suggestion(?::-\d+\+\d+)?[ \t]*$/m;

export function containsSuggestionBlock(body: string): boolean {
  return SUGGESTION_BLOCK.test(body);
}

/**
 * The composer's Insert suggestion pre-fill: the selected new-side source
 * lines wrapped in the block this forge understands, with whatever the reader
 * had already typed kept above it as the explanation. The rules are the
 * existing ones, so an old-side or partial selection refuses here too.
 */
export function suggestionPrefill(
  selection: InlineAnchorSelection,
  forge: SuggestionForge,
  comment = "",
): string {
  const target = prepareSuggestionTarget(selection, forge);
  return formatSuggestionBody(
    target.originalCode,
    target.originalLineCount,
    forge,
    comment,
  );
}
