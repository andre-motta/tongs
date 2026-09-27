// One source for the information architecture.
// The Starlight sidebar, the header section links, the page eyebrows, the
// homepage "Find your way" routes and the footer all read from here, so the
// words cannot drift apart.

export const site = {
  version: 'v1.0.1',
  released: '2026-09-27',
  repo: 'https://github.com/andre-motta/tongs',
  pypi: 'https://pypi.org/project/tongs/',
  license: 'https://github.com/andre-motta/tongs/blob/main/LICENSE',
  author: { name: 'Andre Motta', url: 'https://alustos.us/' },
};

/**
 * Sidebar groups. `dir` is the mono label on the homepage routes, `blurb` the
 * one line under it. `nav: true` puts the group in the header. `badge` labels
 * the group in the header, sidebar and routes (the desktop app is labeled
 * beta wherever it appears). `isNew` marks pages the homepage flags as new.
 */
export const groups = [
  {
    label: 'Start',
    dir: 'start/',
    nav: true,
    blurb: 'Install, sign in to your forges, open your first inbox.',
    items: [
      { label: 'Install and sign in', slug: 'getting-started' },
      { label: 'First run', slug: 'start/first-run' },
    ],
  },
  {
    label: 'Review',
    dir: 'review/',
    nav: true,
    blurb: 'The daily loop in the terminal, one screen at a time.',
    items: [
      { label: 'Inbox', slug: 'guides/inbox' },
      { label: 'Diffs and comments', slug: 'guides/diffs' },
      { label: 'Review drafts', slug: 'guides/review-drafts', isNew: true },
      { label: 'Discussions', slug: 'guides/discussions' },
      { label: 'Pipelines and logs', slug: 'guides/pipelines' },
    ],
  },
  {
    label: 'Desktop',
    dir: 'desktop/',
    nav: true,
    badge: 'beta',
    blurb: 'The optional beta workspace on the same engine.',
    items: [
      { label: 'Install the desktop app', slug: 'desktop/installation' },
      { label: 'Workspace tour', slug: 'desktop/workspace' },
      { label: 'Reviewing on desktop', slug: 'desktop/reviewing' },
      { label: 'Troubleshooting', slug: 'desktop/troubleshooting' },
    ],
  },
  {
    label: 'Extend',
    dir: 'extend/',
    nav: true,
    blurb: 'Agents, plugins and providers.',
    items: [
      { label: 'MCP server', slug: 'extend/mcp', isNew: true },
      { label: 'Terminal plugins', slug: 'extend/terminal-plugins' },
      { label: 'Desktop plugin providers', slug: 'extend/desktop-providers' },
    ],
  },
  {
    label: 'Reference',
    dir: 'reference/',
    nav: true,
    blurb: 'Every key, setting and trust boundary.',
    items: [
      { label: 'Keybindings', slug: 'reference/keybindings' },
      { label: 'Configuration', slug: 'reference/configuration' },
      { label: 'Desktop lifecycle', slug: 'reference/desktop-lifecycle' },
      { label: 'Security and signing', slug: 'reference/security' },
    ],
  },
  {
    label: 'Project',
    dir: 'project/',
    nav: false,
    collapsed: true,
    blurb: 'Releases, known issues and how to contribute.',
    items: [
      { label: 'Releases', slug: 'releases' },
      { label: 'v1.0.1', slug: 'releases/v1.0.1' },
      { label: 'v1.0.0', slug: 'releases/v1.0.0' },
      { label: 'Known issues', slug: 'releases/known-issues' },
      { label: 'Contributing', slug: 'contributing' },
    ],
  },
];

/** Moved pages: old path to new path. Astro emits a redirect page for each. */
export const redirects = {
  '/guides/plugins': '/extend/terminal-plugins/',
  '/plugins/provider': '/extend/desktop-providers/',
  '/desktop/known-limitations': '/releases/known-issues/',
};

/**
 * Headings renamed when a page moved: old fragment to new fragment, per old
 * path. The redirect stubs keep the URL fragment and apply this map, so old
 * deep links land on the matching section.
 */
export const redirectHashes = {
  '/guides/plugins': { 'the-tongsplugin-abc': 'the-tongsplugin-base-class' },
  '/desktop/known-limitations': {
    'desktop-workspace': 'desktop-app-beta',
    terminal: 'terminal-app',
    'acceptance-coverage': 'supported-platform',
  },
};

/** URL for a slug, with the trailing slash Starlight uses. */
export const href = (slug) => `/${slug.replace(/^\/|\/$/g, '')}/`;

/** Starlight `sidebar` config. */
export const sidebar = groups.map((g) => ({
  label: g.label,
  collapsed: Boolean(g.collapsed),
  ...(g.badge ? { badge: { text: g.badge, variant: 'caution' } } : {}),
  items: g.items.map((i) => ({ label: i.label, slug: i.slug })),
}));

/** The group a docs slug belongs to, or undefined. */
export function groupOf(slug) {
  const clean = slug.replace(/^\/|\/$/g, '');
  return groups.find((g) => g.items.some((i) => i.slug === clean));
}

/** The sidebar item for a slug, or undefined. */
export function itemOf(slug) {
  const clean = slug.replace(/^\/|\/$/g, '');
  for (const g of groups) {
    const item = g.items.find((i) => i.slug === clean);
    if (item) return item;
  }
  return undefined;
}

/** Header links: each group's first page. */
export const headerLinks = groups
  .filter((g) => g.nav)
  .map((g) => ({ label: g.label, href: href(g.items[0].slug), group: g.label, badge: g.badge }));

/** Footer columns. */
export const footerColumns = [
  {
    title: 'docs',
    links: [
      { label: 'Install and sign in', href: href('getting-started') },
      { label: 'Keybindings', href: href('reference/keybindings') },
      { label: 'Configuration', href: href('reference/configuration') },
    ],
  },
  {
    title: 'project',
    links: [
      { label: 'GitHub', href: site.repo },
      { label: 'PyPI', href: site.pypi },
      { label: 'License (MIT)', href: site.license },
    ],
  },
  {
    title: 'releases',
    links: [
      { label: 'v1.0.1 notes', href: href('releases/v1.0.1') },
      { label: 'Known issues', href: href('releases/known-issues') },
      { label: 'Contributing', href: href('contributing') },
    ],
  },
];
