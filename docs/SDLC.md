# Tongs SDLC profile

Tongs adopts Agent SDLC **0.1.0**, maintained in the private
[agent-sdlc repository](https://github.com/andre-motta/agent-sdlc).
Source commit: `4e851d1b8a903aa8bebceea078860a21152ee8e8`.
Read the installed `agent-sdlc` skill's `references/workflow.md` with this profile.
Contributors without access to the private package can still follow the project
review process in [CONTRIBUTING.md](https://github.com/andre-motta/tongs/blob/main/CONTRIBUTING.md); the private skill is an
orchestration aid, not a prerequisite for ordinary contributions.

## Project settings

| Setting | Value |
| --- | --- |
| Repository / tracker | `andre-motta/tongs`, GitHub Issues; reuse existing issues and native sub-issue/blocking links where available |
| Local integration target | `main`; Astra verifies on a candidate integration branch before promotion |
| Worktrees | Sibling checkouts under `../tongs-worktrees/`, one `codex/<initiative>/<item>` branch per assignment |
| Runtime | Python 3.12+; current CI tests 3.12 and 3.13 |
| Setup | Create checkout-local `.venv`; activate per shell call; install editable `.[dev]` plus Ruff; add `mcp` extra for MCP tests |
| Focused checks | Tests for changed subsystems, plus relevant lint/format checks |
| Integrated checks | `pytest`, `ruff check src/ tests/`, `ruff format --check src/ tests/`; include optional dependencies required by changed features |
| Documentation checks | Local links, command examples, `git diff --check`; `mkdocs build --strict` for site inputs/navigation |
| Functional proof | Exercise affected TUI/desktop workflows and attach observable evidence; distinguish mocked APIs from live forge calls |
| Commit format | Title, blank line, one-line body; use `git commit -s`; Codex co-author uses `noreply@openai.com` |
| Upstream path | CTO-approved branch push and PR; merge/tag/release/deployment need authorization covering those actions |
| Initiative records | `docs/work/<initiative>.md`; keep public-safe summaries and use access-appropriate locations for sensitive artifacts |

Use `python -m pip install -e ".[dev,mcp]" ruff` in the activated environment for
full core/MCP validation. The test suite mocks forge traffic. Installation may
need network access; a skipped optional test is not proof of that subsystem.

## Authority and integration

Andre approved adoption of this workflow. Astra owns overarching architecture,
design, writing direction, scheduling, and integration. Sol at `high` performs
senior implementation and independent review; Luna at `xhigh` handles bounded
assignments under Sol review. A Sol author has a separate Sol reviewer.

Approved initiatives allow signed-off local commits and validated local
integration without per-commit approval. Preserve unrelated changes and dirty
or occupied default-branch checkouts; leave promotion pending when necessary.
Use the existing four review areas: architecture, security, UX, and QE.

CTO gates apply to design and upstream publication. After approval of an
initiative's design and issue breakdown, Astra may publish and maintain those
issues and dependency links within the approved scope. Do not close issues for
local-only work. Tongs code/documentation pushes are not authorized by the
separate request to publish the private SDLC repository.

## Publication effects

- Pushes/PRs targeting `main` trigger CI.
- Pushes to `main` trigger the MkDocs GitHub Pages deployment; documentation
  under `docs/` is site input even when absent from navigation. Keep it public-safe.
- `v*` tag pushes trigger the PyPI publication workflow using the `pypi` environment.
- A PR push is not permission to merge, tag a release, or trigger deployment.

## Desktop initiative

See [the desktop planning record](work/desktop.md). Its product and prototype
decisions are retained, but its production design and issue graph are not yet
approved. SDLC adoption does not authorize desktop implementation.
