---
title: Workspace tour
description: "A tour of the tongs desktop workspace: local repositories, the review inbox, pipelines and logs, application bar commands and installed plugins."
lead: "One window for your local repositories, their reviews, CI and installed plugin views, on the same services as the terminal app."
---

:::note[Beta]
The desktop app is a beta. Each GitHub Release from v1.0.0 on carries it as a
per-user archive with an attestation, and as unsigned RPMs. The terminal app
remains the primary interface. See [Install the desktop app](/desktop/installation/).
:::

The desktop workspace reads the same local repositories, reviews and durable
drafts as the terminal app. Normal `tongs` startup still opens the terminal
interface; the desktop app starts only when you launch it.

This page walks through the workspace. [Reviewing on desktop](/desktop/reviewing/)
covers the diff, comments, suggestions and the **Your review** drawer. The
desktop review keys are listed in the [keybinding reference](/reference/keybindings/#desktop-app-beta).

## Start with local repositories

The sidebar is headed **Repositories**. tongs scans the configured scan root for
Git repositories and admits those whose remotes identify a supported GitHub or
GitLab forge. It skips nested repositories and remotes it cannot identify. The
default scan root is `~/git`. Change it in the tongs configuration when your
checkouts live elsewhere:

```toml title="~/.config/tongs/config.toml"
[general]
scan_root = "~/src"
scan_depth = 5
```

There is no online repository picker. Clone a repository below the scan root,
then choose **Refresh local repositories** in the sidebar, and the repository
appears as a sidebar entry.

The sidebar has three controls:

- **Search repositories** filters by display name.
- **Forge** chooses **All forges**, **GitHub** or **GitLab**.
- **Sort repositories** orders by **Name**, **Forge** or **Host**. Repositories without a
  detected host stay visible. **Host** sorting places them after entries with a
  known host and breaks ties by display name.

Choose **All reviews** to combine reviews from every discovered repository, or
choose one repository to scope the inbox to that project. A refresh keeps the
current repository, review text and saved drafts when that context is still
available. Choosing a different repository opens its inbox and resets the
inbox query to its default. If the selected repository disappears during
discovery, the workspace returns to **All reviews** and keeps unsaved review
text and saved drafts.

The sidebar states each outcome of discovery:

| State | Message |
|-------|---------|
| Scanning | **Finding admitted repositories…** |
| Nothing found | **No local repositories were found. Clone a repository under the configured scan root, then refresh.** |
| Everything filtered out | **No local repositories match the current search and forge filter.** |
| Discovery failed | An error with **Retry** |

The filtered message is separate from the empty one, so an empty list caused by
a filter is never mistaken for an empty scan root.

## Find a review

The workspace opens on **All reviews**, with the **All Open** scope and the
**Open** state selected. The inbox has three scopes:

- **My Reviews**: open reviews waiting for you.
- **My MRs**: your own open reviews.
- **All Open**: every review, with a choice of **Open** or **Closed & merged**.

The two personal scopes show open items only. Closed and merged reviews are
available in **All Open**.

The sort control orders by **Updated**, **Title**, **CI status** or **Author**.
**Refresh reviews** fetches the list again, and reads **Refreshing…** while an
existing list reloads. A repository page has the same controls but reads only
that repository.

Each list keeps its scope, state and sort for the rest of the session, held
separately for **All reviews** and for each repository page. Open a review and
choose **← Reviews**, and you return to the same list with the same controls.
The review you opened is selected, scrolled into view and focused. Changing a
control afterwards leaves focus on that control. A fresh launch starts on
**All reviews** with **Open** selected. Nothing about the list is written to
disk.

Each review card shows its CI state, number, title, author, source and target
branches, and last update time. Select a card to open the review. When
**All reviews** spans several repositories, a failed repository read is shown
above the cards, and reviews from the repositories that responded stay
available.

An empty scope says so: **No open reviews match this repository scope.** or
**No closed or merged reviews match this repository scope.** The personal
scopes have their own messages, such as **No open reviews are waiting for
you.** and **You have no open merge requests.**

## Read review details

The review header holds **← Reviews**, the review number and title, and
**Open on forge**, which opens the review's forge URL in your external browser.
On **Overview**, **Files changed** and **Discussions** the header is the same:
between the title and **Open on forge** it also carries the review lifecycle
actions, **Merge**, **Close**, **Reopen** and **Remove approval**, followed by
the **Your review** button. They appear once the review's current revision has
loaded. **Commits** and **Pipelines** hold no review state and do not show
them. In a narrow window the header wraps, so the controls move below the
title instead of running off the edge.

A review has five tabs:

- **Overview** shows the description and a **Review status** panel with state,
  merge status, CI status, update time and change counts. A review without a
  description shows **No description was provided.** Overview also holds the
  general composer, the review-level notes that have no diff position, and a
  **Quick verdict** section.
- **Files changed** opens the code diff.
- **Commits** lists commit subjects, short SHAs, authors and timestamps. An
  empty history shows **This review has no commits.**
- **Discussions** lists the published threads that have a diff position.
- **Pipelines** opens the CI view described [below](#inspect-pipelines-jobs-and-logs).

[Reviewing on desktop](/desktop/reviewing/) covers **Files changed**,
**Discussions**, the composers and **Quick verdict**.

Every read view except **Discussions** has a refresh control. If a refresh
fails after data was loaded, the previous result stays visible with the failure
labelled. Use the refresh control to try again. **Discussions** loads once when
the review opens and has no refresh control; leave the review and open it
again to re-read it. If the current revision is not available, reads bound to a
revision are disabled until the review loads again.

### Markdown and links

Descriptions and discussions render ordinary Markdown: headings, lists,
emphasis, code blocks and links. Raw HTML stays inert, and images appear as text
placeholders. A body too large or complex for the safe renderer falls back to a
limited plain-text preview, with the rest marked as omitted. The combined
discussion display has its own Markdown budget, so later comments can show an
omission notice once it runs out.

HTTPS links open in your external browser only when you activate them.
Displaying a link never opens it.

## Inspect pipelines, jobs, and logs

The **Pipelines** tab opens on **Continuous integration** with a **Refresh CI**
button. The pipeline list shows status, pipeline number, ref and a short commit
SHA. It reads **Loading pipelines…** while loading, and **No pipelines are
available for this review.** when there are none. Select a pipeline to see its
ref, source, creation time, supported actions and its **Jobs** section.

**Refresh jobs** reloads the selected pipeline's jobs. Each job shows its
status, name and stage. A pipeline without jobs shows **This pipeline has no
jobs.**

### Read and search a log

Select a job to open **Log · _job name_**. The log view has **Refresh log**,
line numbers, bounded row windows and a **Search log** field. Empty output shows
**This job has no log output.**

Type in **Search log**, or press ++slash++ anywhere in the Pipelines tab while no
text field holds the keyboard. ++slash++ may not move the keyboard into the
box yet ([#177](https://github.com/andre-motta/tongs/issues/177)); click
**Search log** instead. The log keeps all of its lines. Matching lines
are highlighted in place as you type, the first match is selected, and the row
window jumps to it.

- ++enter++ hands the keyboard back to the log.
- ++n++ and ++shift+n++ step to the next and previous match, wrapping around.
  **Previous match** and **Next match** do the same with the mouse.
- The counter shows the selected match and the total, or **No matches**.
- ++escape++ closes the search, clears the term, and restores the row window and
  the control that had focus when the search opened.

**Refresh log** also ends an open search. It clears the term and the selected
match and restores the row window, and leaves keyboard focus where it is. The
search keys act only on this view, so they are ignored while a dialog such as
**Clear shared API cache?** is open.

**Open log in editor**, bound to ++f2++ while the log is loaded and no text
field holds the keyboard, starts the configured graphical editor on a private, bounded export of the log. The
control reads **Starting editor…** while the editor starts. Starting the editor
process is reported separately from any confirmation that the editor read the
file, and closing tongs does not stop the editor. The
[configuration reference](/reference/configuration/) lists the supported editor
commands.

Long logs load and display in bounded pages and windows. If a refresh, a page
or a log revision check fails after output was loaded, the previous result stays
visible with an explanation. A log beyond the renderer's safe limits is shown as
a bounded partial result rather than filling the whole window.

### CI actions and outcomes

When the repository advertises support, the selected pipeline offers **Retry
pipeline** and **Cancel pipeline** beside **Open pipeline on forge**. The
selected job offers **Retry job** and **Cancel job** beside **Open job on
forge**. An unsupported action is disabled with a reason. So is an action whose
reported capability belongs to a different repository. If action support fails
to load, pipeline and log reads stay available.

Choosing an action opens a confirmation that names the exact pipeline or job,
with **Cancel** and **Confirm _action_**. After you confirm, the workspace shows
the action's operation identifier and one of these outcomes:

| Outcome | Meaning |
|---------|---------|
| **CI action accepted** | tongs received a receipt confirming acceptance. |
| **CI action rejected** | tongs received a known rejection. |
| **Remote outcome unknown** | The response did not establish the remote result. Choose **Refresh status** or **Check retained receipt**, and do not start a second action until you have reviewed this one. |
| **CI action needs reconciliation** | The result is uncertain or no retained receipt was available. The workspace offers **Refresh status**, **Check retained receipt** when possible, and **Acknowledge after review**. |

An event gap or a pipeline status event reloads the current pipeline view. The
workspace never silently replays an uncertain action.

## Application bar commands

The top application bar carries three built-in commands, plus any a plugin
contributes.

**Review workflow** appears while a review is open and sorts first. It moves the
review to its **Discussions** tab.

**Copy URL** appears only while a review is open. It copies that review's URL to
the clipboard, taken from the open review rather than from anything typed into
the window. The result appears as a dismissible notice. If the copy fails, the
notice reads **The review URL could not be copied. Check clipboard access and
retry.**

**Clear Cache** is always available. It opens a confirmation titled **Clear
shared API cache?** that says **Cached forge responses will be removed. Saved
and in-progress review drafts will be preserved.** It offers **Cancel** and
**Clear cache**, and ++escape++ cancels it. While the clear runs, the button
reads **Clearing…** and the command gives **The shared API cache is being
cleared.** as its reason for being unavailable. Clearing removes shared API
response entries only. It does not delete durable review drafts or any other
file of yours. If it fails, the notice reads **The shared API cache could not be
cleared. Retry after reconnecting the local service.**

## Use installed plugins

The sidebar also contains **Plugins**. Its refresh button, labelled **Refresh
installed plugins** for assistive technology, reloads the installed plugin
catalog. With no desktop providers installed it shows **No desktop plugins
installed.** A catalog failure appears as an error, and the repository and
review views stay available.

A started plugin lists its declared navigation entries below its title. Select
an entry to open its module. The module view names the plugin and version,
shows **Loading plugin module…** while it starts, and offers **Reload plugin**.
A plugin can also add a command button to the application bar, next to the
[built-in commands](#application-bar-commands). A module can publish
notifications; dismiss one with its close control.

The module's declared help appears under **Plugin help**. If the help cannot be
loaded, the workspace shows **Plugin help is unavailable.** A module whose
resources are missing or changed shows an error instead of loading an
unverified asset.

A plugin that is not running says why:

| Sidebar text | Meaning |
|--------------|---------|
| **Available in the terminal only.** | The distribution has a terminal plugin but no desktop entry point. |
| **Disabled in configuration.** | The plugin is disabled in your configuration. |
| **Incompatible with this desktop version.** | Its declared desktop API is not supported by this app. |
| **Plugin is starting.** | The provider is still starting. |
| **Plugin failed to start.** or **Plugin stopped.** | The provider did not stay available. When an error was recorded, the error text replaces the sentence. |
| **No desktop views declared.** | The provider started but has no navigation entry to show. |

:::caution[Trust]
Desktop providers are trusted, installed Python and UI code. tongs validates
each manifest, keeps navigation, methods, events, focus targets and assets
within the provider's declared scope, and isolates each module's stylesheet in
its own UI root. These controls prevent accidental cross-plugin routing and
resource access. They are not a sandbox against a malicious extension. Install
and enable a provider only when you trust its publisher.
:::

[Desktop plugin providers](/extend/desktop-providers/) covers the entry points
and configuration.
