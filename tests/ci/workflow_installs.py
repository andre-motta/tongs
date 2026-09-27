"""Find every ``pip install`` a workflow job runs, tokenized as a shell would.

``test_workflow_hygiene.py`` and ``test_release_lock.py`` share this scanner so
that both see the same installs: ``pip``, ``pip3``, ``python -m pip`` and a
path to either, with any spacing, line continuations joined, comments removed
and ``;``, ``&&``, ``||`` and ``|`` splitting one line into several commands.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

_PIP_INSTALL = re.compile(r"(^|[\s;&|(/\"'])pip3?\s+install(\s|$)")

#: Shell control tokens that end one command and start the next.
COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})


def shell_tokens(line: str) -> list[str]:
    """Split one shell line into words and control tokens, without comments."""

    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    return list(lexer)


def pip_installs(tokens: list[str]) -> list[list[str]]:
    """Return each ``pip install`` command in a token list, one per command."""

    commands = []
    start = 0
    for index, token in enumerate(tokens):
        if token in COMMAND_SEPARATORS:
            start = index + 1
            continue
        is_pip = token in {"pip", "pip3"} or token.endswith(("/pip", "/pip3"))
        if is_pip and tokens[index + 1 : index + 2] == ["install"]:
            end = next(
                (
                    position
                    for position in range(index + 2, len(tokens))
                    if tokens[position] in COMMAND_SEPARATORS
                ),
                len(tokens),
            )
            commands.append(tokens[start:end])
    return commands


def install_commands(job: dict[str, Any]) -> list[list[str]]:
    """Return every pip install in the job's ``run`` steps, in step order."""

    commands = []
    for step in job.get("steps", []):
        script = (step.get("run") or "").replace("\\\n", " ")
        for line in script.splitlines():
            if _PIP_INSTALL.search(line):
                commands.extend(pip_installs(shell_tokens(line)))
    return commands
