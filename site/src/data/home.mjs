// Homepage key strip and draft steps. Keys use the docs' `++key++` spec
// syntax (without the pluses) and must exist in docs/reference/keybindings.md.

/** 6x2 key grid, "Twelve keys cover most of a review". */
export const keymap = [
  { keys: ['j', 'k'], sep: '/', what: 'Move down and up', where: 'diff, threads, pipeline' },
  { keys: ['enter'], what: 'Open the selection', where: 'inbox, pipeline' },
  { keys: ['1', '2', '3'], what: 'Switch inbox tabs', where: 'inbox' },
  { keys: ['v'], what: 'Unified or split diff', where: 'diff' },
  { keys: ['c'], what: 'Comment on a line or selection', where: 'diff' },
  { keys: ['f3'], what: 'Suggest a change in $EDITOR', where: 'diff' },
  { keys: ['ctrl+g'], what: 'Start or open the review draft', where: 'review' },
  { keys: ['ctrl+s'], what: 'Submit a comment or review', where: 'editor, draft' },
  { keys: ['slash'], what: 'Filter repos or search a log', where: 'repos, pipeline' },
  { keys: ['R', 'R'], what: 'Retry a job, asks twice', where: 'pipeline' },
  { keys: ['ctrl+p'], what: 'Command palette', where: 'everywhere' },
  { keys: ['question'], what: 'A short help hint', where: 'everywhere' },
];

/** Review draft steps. */
export const draftSteps = [
  { key: 'ctrl+g', title: 'Start a review.', text: 'Comments go to the draft.' },
  { key: 'c', title: 'Comment.', text: 'Each one is saved locally.' },
  { key: 'v', title: 'Pick a verdict.', text: 'Comment or approve, and request changes on GitHub.' },
  { key: 'ctrl+s', title: 'Submit once.', text: 'The forge gets one review.' },
  { key: 'ctrl+n', title: 'Recover.', text: 'Cycle drafts kept from earlier sessions.' },
];
