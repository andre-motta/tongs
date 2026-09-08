# Desktop workspace

The desktop workspace brings local repository discovery, review reading, code
diffs, CI inspection, and installed plugin views into one window. This page
describes the current unreleased desktop interface. Tongs remains
terminal-first, and this page does not publish a desktop artifact or change
how Tongs is installed. See [Desktop
installation](installation.md) for the per-user lifecycle and [Desktop plugin
providers](../plugins/provider.md) for provider packaging and configuration.

## Start with local repositories

The sidebar is headed **Repositories**. Tongs scans the configured local scan
root for Git repositories and admits repositories whose remotes identify a
supported GitHub or GitLab forge. It skips nested repositories and remotes it
cannot identify. The default scan root is `~/git`; change it in the Tongs
configuration when your checkouts live elsewhere:

```toml
[general]
scan_root = "~/src"
scan_depth = 5
```

The desktop workspace does not provide an online repository picker. Clone a
repository below the scan root, then choose **Refresh local repositories** in
the sidebar. The repository name then appears as a sidebar entry. Use the
sidebar search to filter by display name, the forge filter to choose **All
forges**, **GitHub**, or **GitLab**, and the sort control to order by **Name**,
**Forge**, or **Host**. Repositories without a detected host remain visible;
**Host** sorting places them after entries with a known host and then uses the
display name as a tie-breaker.

Choose **All reviews** to combine review reads from every discovered
repository, or choose one repository to scope the inbox to that project. A
refresh keeps the current repository context, review text, and saved drafts
when that context is still available. Choosing a different repository starts
that repository's inbox context and resets the inbox query to its default
state. If a selected repository disappears during discovery, the workspace
returns to **All reviews** while preserving unsaved review text and saved
drafts.

While discovery is running, the sidebar shows **Finding admitted
repositories…**. If no repository is found, it shows **No local repositories
were found. Clone a repository under the configured scan root, then refresh.**
If discovery fails, use **Retry**.

## Find a review

The workspace opens on **All reviews** with **Open** selected. The inbox
scopes are **My Reviews**, **My MRs**, and **All Open**. **My Reviews** and
**My MRs** show open items only. **All Open** can switch between **Open** and
**Closed & merged**. The closed state is unavailable in the two personal
scopes because closed and merged reviews are provided by **All Open**.

Use the review sort control to choose **Updated**, **Title**, **CI status**, or
**Author**. Choose **Refresh reviews** to fetch the current list again; while
an existing list refreshes, the control is labelled **Refreshing…**. An
individual repository page uses the same controls but reads only that
repository.

Each review card shows its CI state, number, title, author, source and target
branches, and last update time. Select a card to open its detail view. When
**All reviews** spans several repositories, a failed repository read is shown
above the cards while reviews from repositories that did respond remain
available. An empty scope shows **No open reviews match this repository
scope.** or **No closed or merged reviews match this repository scope.** The
personal scopes use their own empty messages, such as **No open reviews are
waiting for you.** and **You have no open merge requests.**

The review header contains **← Reviews**, the review number and title, and an
**Open on forge** button. That button opens the review's existing forge URL in
the external browser. The desktop workspace continues to use the local
repositories and review data described above.

## Read review details

Review tabs are contributed by the installed workspace features:

- **Overview** shows the review description and a **Review status** panel with
  state, merge status, CI status, update time, and change counts. A review
  without a description shows **No description was provided.**
- **Files changed** opens the code diff. Its layout controls are described
  below.
- **Commits** lists commit subjects, short SHAs, authors, and timestamps. An
  empty history shows **This review has no commits.**
- **Discussions** shows review threads and the commenting and review controls.
- **Pipelines** opens the CI view described below.

Each read view has a refresh control. If a refresh fails after data was already
loaded, the desktop keeps the previous result visible and labels the failure.
Use the view's refresh control to try again. If the current revision is not
available, revision-bound reads are disabled until the review can be loaded
again.

## Comment, suggest, and submit

The **Discussions** panel has two paths. **Quick comment** and **Quick inline
comment** act immediately against the selected review. Choose **Start review**
to create a durable draft, or **Resume review** to reopen one saved draft.
While a durable draft is active, the controls add general comments, selected
lines, and suggestions to that draft. The draft remains bound to the review
revision it was created from. If another writer changes the draft, the
workspace reports a version conflict and preserves your unsaved text so you
can resolve it deliberately.

If the review revision changes, old inline anchors stay on the old draft and
cannot be submitted against the new revision. The workspace can create a
separate current-revision draft with portable body, verdict, and general
comments; old inline comments and replies remain for deliberate recreation.
Submit a draft only after checking its current revision.

Quick actions can be accepted, rejected, or unknown. Durable submissions show
confirmed progress and can end submitted, paused, or unknown. An unknown
result requires an explicit receipt check or reconciliation choice. Confirmed
steps are not replayed automatically, and the workspace never silently
repeats an uncertain remote write. A partial or paused submission can resume
its existing durable attempt; an unknown submission offers explicit choices to
retry remaining steps, return the draft to editing, or mark it submitted after
you inspect the forge.

Suggestions are available from a valid selection in the current complete diff.
Select contiguous new-side lines in either **Unified** or **Split** layout,
then choose **Suggest replacement**. Deletions, old-side selections, partial
diffs, unavailable files, and selections from an earlier revision cannot
produce a suggestion. Edit both the optional explanation and replacement
code before choosing **Post quick suggestion** or **Add suggestion to draft**;
**Cancel and keep text** leaves the entered text available. GitHub suggestions
use its `suggestion` block, while GitLab uses its `suggestion:-0+N` form.

## Work with a diff

Open **Files changed** to see the changed-file navigation and the selected
file's content. The toolbar provides **Unified**, **Split**, and **Refresh**
controls. The current head revision is shown beside the controls so you can
recognize which review revision the displayed diff represents.

The changed-file list shows each path, additions and deletions, and any
available hunk context. A file can carry these visible badges:

- **Binary** when the file has no text diff.
- **Truncated** when the forge returned only a bounded portion.
- **Empty** when the file has no changed rows.
- **Mode only** when the change is a file-mode update.
- **Unavailable** when the forge could not provide the file content.

Select a file or hunk to move the content view. Large files are displayed in
bounded row windows with **Previous rows** and **Next rows** controls. In
**Unified** layout, rows are shown in one sequence. In **Split** layout, the
old and new sides are aligned in two panes; an empty cell represents a side
with no corresponding row. Changed lines and their available line numbers are
selectable to highlight the chosen line in the current diff. Non-text,
unavailable, and placeholder rows remain read-only.

The desktop checks the revision and snapshot while it loads pages. If a
snapshot expires or the review changes during loading, it asks you to reload
the latest diff. If paging reaches a safety bound, it keeps a bounded partial
view and explains that the diff is incomplete. Invalid or inconsistent page
data is reported as an error rather than being silently combined with another
revision.

The diff toolbar also provides **Suggest replacement** when the selected
context meets the rules above. Suggestions are limited to the current
revision and contiguous new-side source lines. The layout changes how the
selection is displayed, not which side can be suggested.

## Read descriptions and discussions

Descriptions and discussions support ordinary Markdown formatting, including
headings, lists, emphasis, code blocks, and links. Raw HTML stays inert, and
images are shown as text placeholders. A body that is too large or complex
for the safe renderer falls back to an explicitly limited plain-text preview;
additional content is marked as omitted. The combined discussion display also
has a bounded Markdown budget, so later comments can show an omission notice
when that budget is exhausted.

HTTPS links open in the external browser only after you explicitly activate
the link. Displaying a link does not open it.

## Inspect pipelines, jobs, and logs

The **Pipelines** tab starts with **Continuous integration** and a **Refresh
CI** button. The pipeline list shows status, pipeline number, ref, and a short
commit SHA. Select a pipeline to see its ref, source, creation time, supported
actions, and its **Jobs** section.

While the list is loading, the tab shows **Loading pipelines…**. If the review
has no pipeline records, it shows **No pipelines are available for this
review.**

Use **Refresh jobs** to reload the selected pipeline's jobs. Each job shows its
status, name, and stage. A pipeline with no jobs shows **This pipeline has no
jobs.** Select a job to open **Log · _job name_**. The log view provides
**Refresh log**, line numbers, bounded windows, and a **Search log** field.
Enter a search term or press `/` while the pipeline view is focused, then use
**Previous match** and **Next match**. The match counter reports the selected
match and total matches; **No matches** is shown when appropriate. Empty output
shows **This job has no log output.**

Long logs are loaded and displayed in bounded pages and windows. If a refresh,
page, or log revision check fails after output was loaded, the previous bounded
result stays visible with an explanation. A log that exceeds the renderer's
safe limits is shown as a bounded partial result rather than consuming the
whole window.

### CI actions and outcomes

When the repository advertises support, the selected pipeline offers **Retry
pipeline** and **Cancel pipeline**. The selected job offers **Retry job** and
**Cancel job**. Unsupported actions are disabled with a reason, and an error
loading action support leaves pipeline and log reads available. Actions are
also disabled when the reported capability belongs to a different repository.

Selecting an action opens a confirmation naming the exact pipeline or job. The
confirmation has **Cancel** and **Confirm _action_** controls. After
confirmation, the workspace shows the action's operation identifier and one of
these outcomes:

- **CI action accepted** means Tongs received a receipt confirming acceptance.
- **CI action rejected** means Tongs received a known rejection.
- **Remote outcome unknown** means the response did not establish the remote
  result. Refresh status or choose **Check retained receipt**; do not start a
  second action until the current outcome is reviewed.
- **CI action needs reconciliation** means the result is uncertain or no
  retained receipt was available. The workspace provides **Refresh status**,
  **Check retained receipt** when possible, and **Acknowledge after review**.

An event gap or pipeline status event reloads the current pipeline view. The
workspace does not silently replay an uncertain action.

## Use installed plugins

The sidebar also contains **Plugins**. Use its refresh button to reload the
installed plugin catalog. The button is labelled **Refresh installed plugins**
for accessibility. With no installed desktop providers it shows **No desktop
plugins installed.** A catalog failure is shown as an error while core
repository and review views remain available.

Started plugins contribute their declared navigation entries below their title.
Select an entry to open its installed module. The module view identifies the
plugin and version, shows **Loading plugin module…** while it starts, and
provides **Reload plugin**. A plugin can also contribute a command button in
the top application bar. The module may publish notifications; dismiss a
notification with its close control.

The module's declared help appears under **Plugin help**. If the help asset
cannot be loaded, the workspace shows **Plugin help is unavailable.** A module
whose resources are missing or changed shows an error instead of loading an
unverified asset.

The sidebar makes non-started states explicit:

- **Available in the terminal only.** means the distribution has a terminal
  plugin without a desktop entry point.
- **Disabled in configuration.** means the plugin is disabled.
- **Incompatible with this desktop version.** means its declared desktop API
  is not supported by this application.
- **Plugin failed to start.** or **Plugin stopped.** means the provider did
  not remain available; the error detail is available from the item.
- **No desktop views declared.** means a started provider has no navigation
  entry to display.

Desktop providers are trusted installed Python and UI code. Tongs validates
their manifest, keeps navigation, methods, events, focus targets, and assets
within the provider's declared scope, and isolates each module's stylesheet in
its own UI root. These controls prevent accidental cross-plugin routing and
resource access; they are not a sandbox for a malicious installed extension.
Install and enable providers only when you trust their publisher. The provider
guide covers the supported entry points and configuration.
