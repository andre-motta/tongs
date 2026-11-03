// Tests for site/scripts/sync-agents-docs.mjs and site/agents-docs.lock.json.
// The archives are small tarballs built here; nothing touches the network.

import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, readdir, rm, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { after, describe, test } from 'node:test';
import { gzipSync } from 'node:zlib';

import {
  LOCK_PATH,
  archiveUrl,
  readLock,
  rewriteLinks,
  syncAgentsDocs,
  validateLock,
} from '../../site/scripts/sync-agents-docs.mjs';

const COMMIT = 'a'.repeat(40);
const REPO = 'example/tongs-agents';
const TOP = `tongs-agents-${COMMIT}`;

// ---- a minimal tar writer ----------------------------------------------------

function header(name, { type = '0', size = 0, linkname = '' } = {}) {
  const block = Buffer.alloc(512);
  const put = (text, offset, length) => block.write(text, offset, length, 'utf8');
  const num = (value, offset, length) => put(`${value.toString(8).padStart(length - 1, '0')}\0`, offset, length);
  put(name, 0, 100);
  num(0o644, 100, 8);
  num(0, 108, 8);
  num(0, 116, 8);
  num(size, 124, 12);
  num(0, 136, 12);
  put('        ', 148, 8);
  put(type, 156, 1);
  put(linkname, 157, 100);
  put('ustar\0', 257, 6);
  put('00', 263, 2);
  let sum = 0;
  for (const byte of block) sum += byte;
  put(`${sum.toString(8).padStart(6, '0')}\0 `, 148, 8);
  return block;
}

function member(name, content = '', options = {}) {
  const data = Buffer.from(content, 'utf8');
  const padding = Buffer.alloc((512 - (data.length % 512)) % 512);
  return Buffer.concat([header(name, { ...options, size: data.length }), data, padding]);
}

function paxRecord(key, value) {
  const body = ` ${key}=${value}\n`;
  let length = body.length + 1;
  while (`${length}${body}`.length !== length) length += 1;
  return `${length}${body}`;
}

/** A gzipped tarball of `members`, each [name, content, options]. */
function archive(members, { comment = COMMIT } = {}) {
  const parts = [];
  if (comment) parts.push(member('pax_global_header', paxRecord('comment', comment), { type: 'g' }));
  for (const [name, content, options] of members) {
    if (Buffer.byteLength(name) > 100) parts.push(member('PaxHeader', paxRecord('path', name), { type: 'x' }));
    parts.push(member(name.slice(0, 100), content, options));
  }
  parts.push(Buffer.alloc(1024));
  return gzipSync(Buffer.concat(parts));
}

const page = (title, body = '') => `---\ntitle: "${title}"\ndescription: "About ${title}."\n---\n\n${body}\n`;

const DIR = { type: '5' };
const GOOD = [
  [`${TOP}/`, '', DIR],
  [`${TOP}/README.md`, '# root readme, not published'],
  [`${TOP}/docs/`, '', DIR],
  [
    `${TOP}/docs/README.md`,
    page(
      'Index',
      [
        'Start with [the tutorial](tutorials/first.md) or [a guide](guide.md#keys).',
        'See [the source](../src/pkg/cli.py), [a plan](work/plan.md) and [records](work/).',
        'Already absolute: [site](https://example.com/x.md) and [anchor](#top).',
        '',
        '```md',
        '[not a link](guide.md)',
        '```',
        '',
        'Inline `[code](guide.md)` stays.',
      ].join('\n'),
    ),
  ],
  [`${TOP}/docs/guide.md`, page('Guide', 'Back to [the index](README.md) and [evidence](../validation/e.json).')],
  [`${TOP}/docs/SDLC.md`, page('SDLC')],
  [`${TOP}/docs/work/`, '', DIR],
  [`${TOP}/docs/work/plan.md`, page('Plan')],
  [`${TOP}/docs/tutorials/`, '', DIR],
  [`${TOP}/docs/tutorials/first.md`, page('First', 'Read [profiles](../guide.md#profiles) and [home](../README.md).')],
  [`${TOP}/docs/tutorials/${'long-name-'.repeat(12)}.md`, page('Long')],
  // A link outside docs/ is never extracted, so it does not refuse the archive.
  [`${TOP}/CLAUDE.md`, '', { type: '2', linkname: 'AGENTS.md' }],
];

// ---- fixtures ---------------------------------------------------------------

const scratch = await mkdtemp(path.join(tmpdir(), 'tongs-agents-docs-'));
after(() => rm(scratch, { recursive: true, force: true }));
let counter = 0;

/** A fresh lock, output dir with a sentinel file, and cache dir for `bytes`. */
async function setup(bytes, { sha256 } = {}) {
  const root = path.join(scratch, String((counter += 1)));
  await mkdir(root);
  const lock = { repo: REPO, commit: COMMIT, sha256: sha256 ?? createHash('sha256').update(bytes).digest('hex') };
  const lockPath = path.join(root, 'lock.json');
  await writeFile(lockPath, JSON.stringify(lock));
  const outDir = path.join(root, 'out');
  await mkdir(outDir);
  await writeFile(path.join(outDir, 'sentinel.md'), 'previous sync');
  const cacheDir = path.join(root, 'cache');
  const fetched = [];
  const fetchImpl = async (url) => {
    fetched.push(url);
    return new Response(bytes);
  };
  return { lock, lockPath, outDir, cacheDir, fetched, fetchImpl };
}

async function tree(dir) {
  const entries = await readdir(dir, { recursive: true, withFileTypes: true });
  return entries
    .filter((entry) => entry.isFile())
    .map((entry) => path.relative(dir, path.join(entry.parentPath, entry.name)).split(path.sep).join('/'))
    .sort();
}

// ---- lock file --------------------------------------------------------------

describe('site/agents-docs.lock.json', () => {
  test('pins a full commit and an archive digest, nothing else', async () => {
    const raw = JSON.parse(await readFile(LOCK_PATH, 'utf8'));
    assert.deepEqual(Object.keys(raw).sort(), ['commit', 'repo', 'sha256']);
    const lock = await readLock();
    assert.equal(lock.repo, 'andre-motta/tongs-agents');
    assert.match(lock.commit, /^[0-9a-f]{40}$/);
    assert.match(lock.sha256, /^[0-9a-f]{64}$/);
    assert.equal(archiveUrl(lock), `https://codeload.github.com/andre-motta/tongs-agents/tar.gz/${lock.commit}`);
  });

  test('refuses other shapes', () => {
    const good = { repo: REPO, commit: COMMIT, sha256: 'b'.repeat(64) };
    assert.deepEqual(validateLock({ ...good }), good);
    for (const bad of [
      { ...good, ref: 'main' },
      { repo: REPO, commit: COMMIT },
      { ...good, commit: '5779a7e' },
      { ...good, commit: 'A'.repeat(40) },
      { ...good, sha256: 'b'.repeat(63) },
      { ...good, repo: 'https://github.com/example/tongs-agents' },
      { ...good, repo: '../tongs-agents' },
      [good],
      null,
    ]) {
      assert.throws(() => validateLock(bad), Error, JSON.stringify(bad));
    }
  });
});

// ---- sync -------------------------------------------------------------------

describe('syncAgentsDocs', () => {
  test('extracts the published pages and rewrites their links', async () => {
    const bytes = archive(GOOD);
    const { lockPath, outDir, cacheDir, fetched, fetchImpl } = await setup(bytes);
    const { written } = await syncAgentsDocs({ lockPath, outDir, cacheDir, fetchImpl });

    assert.deepEqual(fetched, [`https://codeload.github.com/${REPO}/tar.gz/${COMMIT}`]);
    const long = `tutorials/${'long-name-'.repeat(12)}.md`;
    assert.deepEqual(written, ['guide.md', 'index.md', long, 'tutorials/first.md'].sort());
    // The previous sync is replaced, and nothing repository-only is written.
    assert.deepEqual(await tree(outDir), written);

    const index = await readFile(path.join(outDir, 'index.md'), 'utf8');
    assert.match(index, /^---\nslug: agents\neyebrow: overview\ntitle: "Index"\n/);
    assert.ok(index.includes('[the tutorial](/agents/tutorials/first/)'));
    assert.ok(index.includes('[a guide](/agents/guide/#keys)'));
    const blob = `https://github.com/${REPO}/blob/${COMMIT}`;
    assert.ok(index.includes(`[the source](${blob}/src/pkg/cli.py)`));
    assert.ok(index.includes(`[a plan](${blob}/docs/work/plan.md)`));
    assert.ok(index.includes(`[records](https://github.com/${REPO}/tree/${COMMIT}/docs/work/)`));
    assert.ok(index.includes('[site](https://example.com/x.md) and [anchor](#top)'));
    assert.ok(index.includes('```md\n[not a link](guide.md)\n```'));
    assert.ok(index.includes('Inline `[code](guide.md)` stays.'));

    const guide = await readFile(path.join(outDir, 'guide.md'), 'utf8');
    assert.match(guide, /^---\nslug: agents\/guide\ntitle: "Guide"\n/);
    assert.ok(guide.includes('[the index](/agents/)'));
    assert.ok(guide.includes(`[evidence](${blob}/validation/e.json)`));

    const first = await readFile(path.join(outDir, 'tutorials/first.md'), 'utf8');
    assert.match(first, /^---\nslug: agents\/tutorials\/first\n/);
    assert.ok(first.includes('[profiles](/agents/guide/#profiles) and [home](/agents/)'));

    // The verified archive is cached by commit and reused without the network.
    assert.ok(existsSync(path.join(cacheDir, `${COMMIT}.tar.gz`)));
    const offline = async () => {
      throw new Error('offline');
    };
    const again = await syncAgentsDocs({ lockPath, outDir, cacheDir, fetchImpl: offline });
    assert.deepEqual(again.written, written);
  });

  test('refuses an archive whose sha256 does not match the lock', async () => {
    const bytes = archive(GOOD);
    const { lockPath, outDir, cacheDir, fetchImpl } = await setup(bytes, { sha256: 'c'.repeat(64) });
    await assert.rejects(syncAgentsDocs({ lockPath, outDir, cacheDir, fetchImpl }), /sha256 mismatch/);
    assert.deepEqual(await tree(outDir), ['sentinel.md']);
    assert.equal(existsSync(cacheDir), false);
  });

  test('ignores a cached archive that no longer matches and downloads again', async () => {
    const bytes = archive(GOOD);
    const { lockPath, outDir, cacheDir, fetched, fetchImpl } = await setup(bytes);
    await mkdir(cacheDir);
    await writeFile(path.join(cacheDir, `${COMMIT}.tar.gz`), archive(GOOD.slice(0, 4)));
    await syncAgentsDocs({ lockPath, outDir, cacheDir, fetchImpl });
    assert.equal(fetched.length, 1);
    assert.deepEqual(await readFile(path.join(cacheDir, `${COMMIT}.tar.gz`)), bytes);
  });

  test('fails loudly without the network or on an HTTP error', async () => {
    const bytes = archive(GOOD);
    const { lockPath, outDir, cacheDir } = await setup(bytes);
    const offline = async () => {
      throw new TypeError('fetch failed');
    };
    await assert.rejects(syncAgentsDocs({ lockPath, outDir, cacheDir, fetchImpl: offline }), /codeload\.github\.com.*fetch failed/);
    const missing = async () => new Response('gone', { status: 404 });
    await assert.rejects(syncAgentsDocs({ lockPath, outDir, cacheDir, fetchImpl: missing }), /HTTP 404/);
    assert.deepEqual(await tree(outDir), ['sentinel.md']);
  });

  const unsafe = {
    "a '..' segment": [[`${TOP}/docs/../../evil.md`, page('Evil')]],
    'an absolute path': [[`/etc/${TOP}/docs/evil.md`, page('Evil')]],
    "a '..' segment outside docs/": [[`${TOP}/../outside.md`, 'x']],
    'a symlink under docs/': [[`${TOP}/docs/link.md`, '', { type: '2', linkname: '/etc/passwd' }]],
    'a hard link under docs/': [[`${TOP}/docs/hard.md`, '', { type: '1', linkname: `${TOP}/README.md` }]],
    'a device under docs/': [[`${TOP}/docs/dev`, '', { type: '3' }]],
    'a member outside the top folder': [[`other-${COMMIT}/docs/x.md`, page('X')]],
  };
  for (const [what, extra] of Object.entries(unsafe)) {
    test(`refuses the whole archive for ${what}`, async () => {
      const bytes = archive([...GOOD, ...extra]);
      const { lockPath, outDir, cacheDir, fetchImpl } = await setup(bytes);
      await assert.rejects(syncAgentsDocs({ lockPath, outDir, cacheDir, fetchImpl }), /refusing archive member/);
      assert.deepEqual(await tree(outDir), ['sentinel.md']);
    });
  }

  test('refuses an archive of another commit', async () => {
    const bytes = archive(GOOD, { comment: 'b'.repeat(40) });
    const { lockPath, outDir, cacheDir, fetchImpl } = await setup(bytes);
    await assert.rejects(syncAgentsDocs({ lockPath, outDir, cacheDir, fetchImpl }), /not the pinned/);
  });

  test('refuses a page without front matter or with its own slug', async () => {
    for (const body of ['# no front matter\n', '---\ntitle: x\nslug: elsewhere\n---\n']) {
      const bytes = archive([...GOOD, [`${TOP}/docs/odd.md`, body]]);
      const { lockPath, outDir, cacheDir, fetchImpl } = await setup(bytes);
      await assert.rejects(syncAgentsDocs({ lockPath, outDir, cacheDir, fetchImpl }), /docs\/odd\.md/);
    }
  });
});

describe('rewriteLinks', () => {
  const lock = { repo: REPO, commit: COMMIT, sha256: 'b'.repeat(64) };
  const published = new Map([
    ['README.md', Buffer.alloc(0)],
    ['guide.md', Buffer.alloc(0)],
  ]);

  test('keeps link titles and angle-bracket destinations', () => {
    assert.equal(
      rewriteLinks('[a](guide.md "Guide") [b](<guide.md#x>)', 'README.md', published, lock),
      '[a](/agents/guide/ "Guide") [b](</agents/guide/#x>)',
    );
  });

  test('leaves fences of any indentation alone', () => {
    const text = '1. Step\n\n    ~~~\n    [x](guide.md)\n    ~~~\n\n[y](guide.md)';
    assert.equal(
      rewriteLinks(text, 'README.md', published, lock),
      '1. Step\n\n    ~~~\n    [x](guide.md)\n    ~~~\n\n[y](/agents/guide/)',
    );
  });

  test('refuses a link that leaves the repository', () => {
    assert.throws(() => rewriteLinks('[x](../../up.md)', 'README.md', published, lock), /leaves the repository/);
  });
});
