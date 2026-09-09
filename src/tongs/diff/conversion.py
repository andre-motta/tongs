"""Convert GitHub and GitLab change payloads into forge-agnostic diff files."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from tongs.diff.models import DiffFile, DiffHunk, FileStatus, LineType
from tongs.diff.parser import _detect_language, parse_diff

# Suffixes whose content a unified diff cannot show as reviewable text.  The
# probe that uses this set applies only where the forge withheld both the patch
# and the reason, so a false positive replaces no information rather than real
# diff content.
_BINARY_SUFFIXES = frozenset(
    {
        "7z",
        "a",
        "avi",
        "avif",
        "bin",
        "bmp",
        "bz2",
        "class",
        "dat",
        "db",
        "deb",
        "dll",
        "dmg",
        "doc",
        "docx",
        "dylib",
        "eot",
        "exe",
        "flac",
        "gif",
        "gz",
        "ico",
        "img",
        "iso",
        "jar",
        "jpeg",
        "jpg",
        "mkv",
        "mov",
        "mp3",
        "mp4",
        "npy",
        "npz",
        "o",
        "odp",
        "ods",
        "odt",
        "ogg",
        "onnx",
        "otf",
        "pdf",
        "pkl",
        "png",
        "ppt",
        "pptx",
        "pyc",
        "pyd",
        "rar",
        "rpm",
        "safetensors",
        "so",
        "sqlite",
        "sqlite3",
        "tar",
        "tiff",
        "ttf",
        "wasm",
        "wav",
        "webm",
        "webp",
        "whl",
        "woff",
        "woff2",
        "xls",
        "xlsx",
        "xz",
        "zip",
        "zst",
    }
)


def convert_forge_changes(
    changes: Sequence[Mapping[str, object]],
) -> tuple[DiffFile, ...]:
    """Convert the documented GitHub/GitLab change shapes.

    GitHub uses ``filename``/``previous_filename``/``patch`` while GitLab
    uses ``old_path``/``new_path``/``diff``.  The API's file metadata remains
    authoritative for paths, status, and aggregate counts.  Patch text is
    parsed only in memory, and an absent patch is represented as an explicit
    empty, rename-only or truncated file according to the available metadata.

    GitHub's files endpoint withholds binary, mode and emptiness metadata, so
    the shapes it does determine are derived from the fields it does send plus
    a cheap local reading of the path, and the remainder stays explicitly
    unavailable.  Missing patch text alone is never evidence of any shape: for
    a file the forge says has no changed lines, a known binary suffix reads as
    binary, a path that resolves to a text lexer licenses the rename-only and
    empty readings, and a path that says neither leaves the file unavailable.
    Every derivation here is local, and none issues a forge request.
    """

    return tuple(_convert_change(change) for change in changes)


def _convert_change(change: Mapping[str, object]) -> DiffFile:
    old_path, new_path = _paths(change)
    status = _status(change)
    patch = _patch_text(change)
    parsed = _parse_patch(patch)
    parsed_file = parsed[0] if parsed else None
    hunks = parsed_file.hunks if parsed_file is not None else ()

    is_binary = _first_bool(change, "is_binary", "binary")
    if is_binary is None:
        is_binary = (
            parsed_file.is_binary if parsed_file is not None else False
        ) or _patch_mentions_binary(patch)
    status_value = _string(change.get("status"))
    if status_value is not None and status_value.lower() == "binary":
        is_binary = True
    is_truncated_value = _first_bool(
        change,
        "is_truncated",
        "truncated",
        "patch_truncated",
        "too_large",
        "collapsed",
    )
    mode_only_value = _first_bool(change, "is_mode_only", "mode_only", "mode_changed")

    parsed_additions = parsed_file.additions if parsed_file else 0
    parsed_deletions = parsed_file.deletions if parsed_file else 0
    reported_additions = _json_count(change.get("additions"))
    reported_deletions = _json_count(change.get("deletions"))
    additions = (
        reported_additions if reported_additions is not None else parsed_additions
    )
    deletions = (
        reported_deletions if reported_deletions is not None else parsed_deletions
    )
    has_hunks = bool(hunks)
    path = new_path or old_path
    # One Pygments lookup per file, reused below as the text signal and further
    # down as the reported language.
    path_language = _detect_language(path)

    # GitHub omits ``patch`` and reports zero counts for a binary, empty,
    # rename-only or mode-only file, and its payload for a binary file is the
    # same whether or not the bytes changed.  So a withheld patch is only ever
    # read through the path: a known binary suffix means binary, a resolved
    # text lexer licenses the content-free readings below, and a path that says
    # neither leaves the file explicitly unavailable.
    withholds_patch = not _has_patch_key(change)
    reports_no_lines = _reports_no_line_change(change)
    withheld_no_lines = not has_hunks and withholds_patch and reports_no_lines
    # Settle binary before every other withheld-patch shape.  A binary path
    # must never reach the rename-only or empty derivations, because both of
    # those assert there is no content change to review.
    if not is_binary and withheld_no_lines and _has_binary_suffix(path):
        is_binary = True

    is_mode_only = (
        mode_only_value is True
        or (
            mode_only_value is None
            and _modes_differ(change)
            and status == FileStatus.MODIFIED
        )
    ) and not has_hunks

    # A positive aggregate count with no patch means the forge gave us file
    # metadata but withheld the body.  This is the useful, conservative
    # distinction between a truncated patch and an intentionally empty file.
    incomplete_hunk = _has_incomplete_hunk(hunks)
    aggregate_shortfall = (
        has_hunks
        and not is_binary
        and (
            (reported_additions is not None and parsed_additions < reported_additions)
            or (
                reported_deletions is not None and parsed_deletions < reported_deletions
            )
        )
    )
    is_truncated = (
        incomplete_hunk
        or aggregate_shortfall
        or (
            is_truncated_value is True
            or (
                is_truncated_value is None
                and not has_hunks
                and not is_binary
                and bool(additions or deletions)
            )
        )
    )

    explicit_empty = _first_bool(change, "is_empty", "empty", "empty_diff")

    # A rename the forge describes with no content change.  GitLab states it
    # with an empty patch body; GitHub only omits the patch, so that reading
    # needs a text signal from the path, or a renamed binary whose bytes also
    # changed would claim there is nothing to review.  Any explicit forge flag
    # to the contrary wins, which keeps this state exclusive and stops it from
    # contradicting something the forge actually reported.
    is_rename_only = (
        not has_hunks
        and not is_binary
        and not is_truncated
        and not is_mode_only
        and explicit_empty is not True
        and status is FileStatus.RENAMED
        and old_path != new_path
        and (
            _has_explicit_empty_patch(change)
            or (withheld_no_lines and bool(path_language))
        )
    )

    if explicit_empty is None:
        # GitLab states emptiness with an empty patch body.  GitHub sends an
        # added or deleted binary file exactly as it sends an added or deleted
        # empty file, so Empty is inferred there only when the path resolves to
        # a text lexer.  Without that signal the payload does not separate the
        # two, and the file stays unavailable instead.
        reports_empty_content = _has_explicit_empty_patch(change) or (
            withheld_no_lines
            and status in (FileStatus.ADDED, FileStatus.DELETED)
            and bool(path_language)
        )
        is_empty = (
            reports_empty_content
            and not has_hunks
            and not is_binary
            and not is_truncated
            and not is_mode_only
            and not is_rename_only
        )
    else:
        is_empty = explicit_empty
    is_unavailable = (
        not has_hunks
        and not is_binary
        and not is_truncated
        and not is_mode_only
        and not is_empty
        and not is_rename_only
        and explicit_empty is None
    )

    language = _string(change.get("language"))
    if language is None:
        language = path_language
        if not language and parsed_file is not None:
            language = parsed_file.language

    return DiffFile(
        old_path=old_path,
        new_path=new_path,
        status=status,
        hunks=tuple(hunks),
        additions=additions,
        deletions=deletions,
        is_binary=is_binary,
        language=language,
        is_truncated=is_truncated,
        is_empty=is_empty,
        is_mode_only=is_mode_only,
        is_rename_only=is_rename_only,
        is_unavailable=is_unavailable,
    )


def _paths(change: Mapping[str, object]) -> tuple[str, str]:
    """Read forge paths without normalizing away spaces or rename identity."""

    filename = _string(change.get("filename")) or ""
    previous_filename = _string(change.get("previous_filename"))
    old_path = _string(change.get("old_path")) or previous_filename or filename
    new_path = _string(change.get("new_path")) or filename or old_path
    return old_path, new_path


def _status(change: Mapping[str, object]) -> FileStatus:
    status = _string(change.get("status"))
    if status:
        status_value = status.lower()
        status_map = {
            "added": FileStatus.ADDED,
            "created": FileStatus.ADDED,
            "deleted": FileStatus.DELETED,
            "removed": FileStatus.DELETED,
            "renamed": FileStatus.RENAMED,
            "copied": FileStatus.RENAMED,
            "modified": FileStatus.MODIFIED,
            "changed": FileStatus.MODIFIED,
        }
        if status_value in status_map:
            return status_map[status_value]

    if _is_true(change.get("new_file")):
        return FileStatus.ADDED
    if _is_true(change.get("deleted_file")):
        return FileStatus.DELETED
    if _is_true(change.get("renamed_file")):
        return FileStatus.RENAMED
    return FileStatus.MODIFIED


def _patch_text(change: Mapping[str, object]) -> str:
    diff = _string(change.get("diff"))
    if diff is not None:
        return diff
    patch = _string(change.get("patch"))
    return patch or ""


def _parse_patch(patch: str) -> list[DiffFile]:
    if not patch.strip():
        return []

    stripped = patch.lstrip()
    if stripped.startswith(("--- ", "diff --git ")):
        return parse_diff(stripped)

    # Forge change endpoints normally return only the @@ section.  Supplying
    # synthetic headers lets the existing parser preserve its line accounting
    # while the caller's API paths remain authoritative in the resulting file.
    # Paths come from forge metadata, while synthetic headers are fixed safe
    # tokens.  A malformed API path must never inject newlines or diff
    # boundaries into the patch parser.
    old_header = "__tongs_old__"
    new_header = "__tongs_new__"
    clean_patch = patch.rstrip("\n")
    text = f"--- a/{old_header}\n+++ b/{new_header}\n{clean_patch}"
    return parse_diff(text)


def _json_count(value: object) -> int | None:
    """Return a non-negative JSON integer when one was reported."""

    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _modes_differ(change: Mapping[str, object]) -> bool:
    old_mode = _string(change.get("a_mode")) or _string(change.get("old_mode"))
    new_mode = _string(change.get("b_mode")) or _string(change.get("new_mode"))
    return old_mode is not None and new_mode is not None and old_mode != new_mode


def _reports_no_line_change(change: Mapping[str, object]) -> bool:
    """Whether the forge affirmatively reported zero changed lines.

    GitHub's files endpoint always carries ``changes``, ``additions`` and
    ``deletions``, so a reported zero is evidence about the file.  A forge that
    reports no counts at all offers no such evidence and must not be read as
    if it had reported zero.
    """

    changes = _json_count(change.get("changes"))
    additions = _json_count(change.get("additions"))
    deletions = _json_count(change.get("deletions"))
    if changes is None and (additions is None or deletions is None):
        return False
    return not (changes or additions or deletions)


def _has_patch_key(change: Mapping[str, object]) -> bool:
    """Whether the forge sent patch text at all, empty text included."""

    return any(isinstance(change.get(key), str) for key in ("diff", "patch"))


def _has_binary_suffix(path: str) -> bool:
    """Cheap local probe for a path whose content is not reviewable text.

    This runs only for a file the forge says has no changed lines and no
    patch, where the alternative is no information at all.  It reads the path
    the forge already sent and never issues a request.
    """

    _, separator, suffix = path.rpartition(".")
    return bool(separator) and suffix.lower() in _BINARY_SUFFIXES


def _patch_mentions_binary(patch: str) -> bool:
    return any(line.startswith("Binary files ") for line in patch.splitlines())


def _has_incomplete_hunk(hunks: Sequence[DiffHunk]) -> bool:
    for hunk in hunks:
        old_lines = sum(
            line.line_type in (LineType.CONTEXT, LineType.DELETION)
            for line in hunk.lines
        )
        new_lines = sum(
            line.line_type in (LineType.CONTEXT, LineType.ADDITION)
            for line in hunk.lines
        )
        if old_lines < hunk.old_count or new_lines < hunk.new_count:
            return True
    return False


def _has_explicit_empty_patch(change: Mapping[str, object]) -> bool:
    for key in ("diff", "patch"):
        if key in change and isinstance(change[key], str):
            return not change[key].strip()
    return False


def _first_bool(change: Mapping[str, object], *keys: str) -> bool | None:
    saw_false = False
    for key in keys:
        value = change.get(key)
        if value is True:
            return True
        if value is False:
            saw_false = True
    return False if saw_false else None


def _is_true(value: object) -> bool:
    return value is True


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None
