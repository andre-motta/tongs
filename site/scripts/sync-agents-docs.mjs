// Fetch the tongs-agents documentation pinned by ../agents-docs.lock.json and
// write it to src/content/agents/, which the docs collection publishes under
// /agents/. The pages are never committed: the build and dev scripts run this
// first, so every build uses exactly the pinned commit.
//
// The source archive is GitHub's tarball of the pinned commit. Its sha256 must
// match the lock file, so a moved or tampered archive fails the build, and so
// does a build without network access (unless the verified archive is already
// cached under node_modules/.cache). Only docs/** is extracted, without the
// repository-only docs/work/** and docs/SDLC.md, and the archive is refused as
// a whole if any member has an absolute path or a `..` segment, or if a link
// or special file sits under docs/.
//
// Each page gets a `slug` front matter for its /agents/ URL. Relative links
// between published pages become site URLs (/agents/...), because the site's
// link validator refuses relative links. Relative links to anything not
// published (other repository files, docs/work/**) become GitHub URLs at the
// pinned commit.

import { createHash } from 'node:crypto';
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { gunzipSync } from 'node:zlib';

const SITE = fileURLToPath(new URL('..', import.meta.url));

export const LOCK_PATH = path.join(SITE, 'agents-docs.lock.json');
export const OUT_DIR = path.join(SITE, 'src/content/agents');
export const CACHE_DIR = path.join(SITE, 'node_modules/.cache/tongs-agents-docs');
/** The site path the pages are published under. */
export const SITE_PREFIX = '/agents/';

const LOCK_KEYS = ['repo', 'commit', 'sha256'];
const MAX_ARCHIVE_BYTES = 64 * 1024 * 1024;
const FETCH_TIMEOUT_MS = 60_000;

/** Validate a parsed lock file and return it. Throws on any other shape. */
export function validateLock(lock) {
  if (lock === null || typeof lock !== 'object' || Array.isArray(lock)) {
    throw new Error('the lock file must be a JSON object');
  }
  const keys = Object.keys(lock).sort();
  if (keys.join() !== [...LOCK_KEYS].sort().join()) {
    throw new Error(`the lock file must have exactly the keys ${LOCK_KEYS.join(', ')}; found ${keys.join(', ')}`);
  }
  if (typeof lock.repo !== 'string' || !/^[A-Za-z0-9-]+\/[A-Za-z0-9._-]+$/.test(lock.repo)) {
    throw new Error(`lock "repo" must be an owner/name GitHub repository; found ${JSON.stringify(lock.repo)}`);
  }
  if (typeof lock.commit !== 'string' || !/^[0-9a-f]{40}$/.test(lock.commit)) {
    throw new Error(`lock "commit" must be a full 40-character lowercase commit SHA; found ${JSON.stringify(lock.commit)}`);
  }
  if (typeof lock.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(lock.sha256)) {
    throw new Error(`lock "sha256" must be a 64-character lowercase hex digest; found ${JSON.stringify(lock.sha256)}`);
  }
  return lock;
}

export async function readLock(file = LOCK_PATH) {
  return validateLock(JSON.parse(await readFile(file, 'utf8')));
}

export const archiveUrl = (lock) => `https://codeload.github.com/${lock.repo}/tar.gz/${lock.commit}`;

const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

export function verifyArchive(bytes, lock) {
  const actual = sha256(bytes);
  if (actual !== lock.sha256) {
    throw new Error(
      `sha256 mismatch for ${archiveUrl(lock)}: expected ${lock.sha256}, got ${actual}. ` +
        'Refusing the archive; if the pin was bumped on purpose, update sha256 in site/agents-docs.lock.json.',
    );
  }
  return bytes;
}

async function download(lock, fetchImpl) {
  const url = archiveUrl(lock);
  let response;
  try {
    response = await fetchImpl(url, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS), redirect: 'follow' });
  } catch (error) {
    throw new Error(`could not download ${url} (is the network to codeload.github.com available?): ${error.message}`);
  }
  if (!response.ok) throw new Error(`could not download ${url}: HTTP ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length > MAX_ARCHIVE_BYTES) throw new Error(`${url} is larger than ${MAX_ARCHIVE_BYTES} bytes`);
  return bytes;
}

/**
 * The verified archive bytes: from the cache when a file for this commit is
 * there and still matches the pinned hash, otherwise downloaded, verified and
 * then cached.
 */
export async function fetchArchive(lock, { cacheDir = CACHE_DIR, fetchImpl = globalThis.fetch } = {}) {
  const cached = cacheDir && path.join(cacheDir, `${lock.commit}.tar.gz`);
  if (cached) {
    const bytes = await readFile(cached).catch(() => undefined);
    if (bytes && sha256(bytes) === lock.sha256) return bytes;
  }
  const bytes = verifyArchive(await download(lock, fetchImpl), lock);
  if (cached) {
    await mkdir(cacheDir, { recursive: true });
    const partial = `${cached}.${process.pid}.partial`;
    await writeFile(partial, bytes);
    await rename(partial, cached);
  }
  return bytes;
}

// ---- tar --------------------------------------------------------------------

const BLOCK = 512;

function field(header, offset, length) {
  const raw = header.subarray(offset, offset + length);
  const end = raw.indexOf(0);
  return raw.subarray(0, end === -1 ? length : end).toString('utf8');
}

function octal(header, offset, length) {
  const text = field(header, offset, length).trim();
  if (!/^[0-7]*$/.test(text)) throw new Error(`malformed tar header number ${JSON.stringify(text)}`);
  return text ? parseInt(text, 8) : 0;
}

function paxRecords(body) {
  const records = {};
  let at = 0;
  while (at < body.length) {
    const space = body.indexOf(0x20, at);
    const length = parseInt(body.subarray(at, space).toString('utf8'), 10);
    if (space === -1 || !Number.isInteger(length) || length <= 0) throw new Error('malformed pax header');
    const record = body.subarray(space + 1, at + length - 1).toString('utf8');
    const eq = record.indexOf('=');
    records[record.slice(0, eq)] = record.slice(eq + 1);
    at += length;
  }
  return records;
}

/**
 * The members of an uncompressed tar archive (ustar, pax and GNU long names):
 * `{ name, type, data }`, where type is 'file', 'directory', 'link' or
 * 'other'. Pax and GNU metadata records are folded into the member they
 * describe; the pax global header is returned as `global`.
 */
export function readTar(tar) {
  const members = [];
  let global = {};
  let pending = {};
  let at = 0;
  while (at + BLOCK <= tar.length) {
    const header = tar.subarray(at, at + BLOCK);
    if (header.every((b) => b === 0)) break;
    const sum = octal(header, 148, 8);
    let check = 0;
    for (let i = 0; i < BLOCK; i += 1) check += i >= 148 && i < 156 ? 0x20 : header[i];
    if (sum !== check) throw new Error('tar header checksum mismatch');
    const size = octal(header, 124, 12);
    const flag = String.fromCharCode(header[156] || 0x30);
    const start = at + BLOCK;
    if (start + size > tar.length) throw new Error('truncated tar archive');
    const data = tar.subarray(start, start + size);
    at = start + Math.ceil(size / BLOCK) * BLOCK;
    if (flag === 'g') {
      global = { ...global, ...paxRecords(data) };
      continue;
    }
    if (flag === 'x') {
      pending = { ...pending, ...paxRecords(data) };
      continue;
    }
    if (flag === 'K') continue; // GNU long link name: the link member itself follows
    if (flag === 'L') {
      pending = { ...pending, path: field(data, 0, data.length) };
      continue;
    }
    const prefix = field(header, 345, 155);
    const short = field(header, 0, 100);
    const name = pending.path ?? (prefix ? `${prefix}/${short}` : short);
    const type =
      flag === '0' || flag === '7'
        ? 'file'
        : flag === '5'
          ? 'directory'
          : flag === '1' || flag === '2'
            ? 'link'
            : 'other';
    members.push({ name, type, data });
    pending = {};
  }
  return { members, global };
}

// ---- selection --------------------------------------------------------------

/** Why a member name is unsafe, or undefined when it is a plain relative path. */
export function unsafeName(name) {
  if (name.startsWith('/') || /^[A-Za-z]:/.test(name)) return 'absolute path';
  if (name.includes('\\')) return 'backslash in path';
  if (name.includes('\0')) return 'NUL in path';
  if (name.split('/').includes('..')) return "'..' segment";
  return undefined;
}

/** True for a docs/-relative path that is never published. */
export const isRepositoryOnly = (rel) => rel === 'SDLC.md' || rel === 'work' || rel.startsWith('work/');

/** The output path, relative to the content directory, for a docs/ path. */
export const outputPath = (rel) =>
  rel === 'README.md' ? 'index.md' : rel.endsWith('/README.md') ? `${rel.slice(0, -'README.md'.length)}index.md` : rel;

/**
 * The published docs/ files of an archive: a map of docs/-relative path to
 * bytes. Throws on an unsafe member anywhere in the archive, on a link or
 * special file under docs/, and on members outside the expected top folder.
 */
export function selectDocs(tarBytes, lock) {
  const { members, global } = readTar(tarBytes);
  if (global.comment !== undefined && global.comment !== lock.commit) {
    throw new Error(`archive is for commit ${global.comment}, not the pinned ${lock.commit}`);
  }
  const top = `${lock.repo.split('/')[1]}-${lock.commit}/`;
  const docsRoot = `${top}docs/`;
  const files = new Map();
  for (const member of members) {
    const why = unsafeName(member.name);
    if (why) throw new Error(`refusing archive member ${JSON.stringify(member.name)}: ${why}`);
    if (member.name !== top && !member.name.startsWith(top)) {
      throw new Error(`refusing archive member ${JSON.stringify(member.name)}: outside ${top}`);
    }
    if (!member.name.startsWith(docsRoot)) continue;
    const rel = member.name.slice(docsRoot.length).replace(/\/$/, '');
    if (member.type === 'link' || member.type === 'other') {
      throw new Error(`refusing archive member ${JSON.stringify(member.name)}: ${member.type === 'link' ? 'link' : 'special file'} under docs/`);
    }
    if (member.type !== 'file' || !rel || isRepositoryOnly(rel)) continue;
    files.set(rel, member.data);
  }
  if (!files.has('README.md')) throw new Error(`archive has no ${docsRoot}README.md for the /agents/ index`);
  return files;
}

// ---- link rewriting ---------------------------------------------------------

const isPage = (rel) => /\.mdx?$/.test(rel);

/** The site URL of a published page, by its docs/-relative source path. */
export function pageUrl(rel) {
  const out = outputPath(rel).replace(/\.mdx?$/, '');
  const route = out === 'index' ? '' : out.endsWith('/index') ? out.slice(0, -'index'.length) : `${out}/`;
  return `${SITE_PREFIX}${route}`;
}

/**
 * Rewrite one link destination found in docs/<from>. Absolute URLs, site
 * paths and same-page anchors stay as they are.
 */
export function rewriteTarget(target, from, published, lock) {
  if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(target) || target.startsWith('/') || target.startsWith('#') || target === '') {
    return target;
  }
  const hashAt = target.indexOf('#');
  const pathPart = hashAt === -1 ? target : target.slice(0, hashAt);
  const hash = hashAt === -1 ? '' : target.slice(hashAt);
  const queryAt = pathPart.indexOf('?');
  const bare = queryAt === -1 ? pathPart : pathPart.slice(0, queryAt);
  const resolved = path.posix.normalize(path.posix.join('docs', path.posix.dirname(from), decodeURI(bare)));
  const isDir = bare.endsWith('/') || resolved === 'docs';
  const rel = resolved.startsWith('docs/') ? resolved.slice('docs/'.length) : undefined;
  if (rel !== undefined && published.has(rel) && isPage(rel)) return pageUrl(rel) + hash;
  if (resolved.startsWith('../')) throw new Error(`link ${JSON.stringify(target)} in docs/${from} leaves the repository`);
  const kind = isDir ? 'tree' : 'blob';
  const encoded = resolved.replace(/\/$/, '').split('/').map(encodeURIComponent).join('/');
  return `https://github.com/${lock.repo}/${kind}/${lock.commit}/${encoded}${isDir ? '/' : ''}${hash}`;
}

/**
 * Rewrite the inline link destinations of a Markdown page, outside fenced
 * code blocks and inline code spans.
 */
export function rewriteLinks(markdown, from, published, lock) {
  let fence;
  return markdown
    .split('\n')
    .map((line) => {
      const opener = /^\s*(`{3,}|~{3,})/.exec(line);
      if (fence) {
        if (opener && opener[1][0] === fence[0] && opener[1].length >= fence.length && line.trim() === opener[1]) fence = undefined;
        return line;
      }
      if (opener) {
        fence = opener[1];
        return line;
      }
      return line
        .split(/(`+[^`]*`+)/)
        .map((part, i) =>
          i % 2 === 1
            ? part
            : part.replace(/\]\((<[^>\n]*>|[^)\s]+)((?:\s+"[^"\n]*")?\))/g, (_, dest, rest) => {
                const angled = dest.startsWith('<');
                const raw = angled ? dest.slice(1, -1) : dest;
                const next = rewriteTarget(raw, from, published, lock);
                return `](${angled ? `<${next}>` : next}${rest}`;
              }),
        )
        .join('');
    })
    .join('\n');
}

/**
 * Add `slug: agents/...` (and, on the index, `eyebrow: overview`) to a page's
 * front matter. The id then no longer
 * depends on where the file sits, and the link validator, which derives ids
 * from paths under src/content/docs, files the page under its real URL.
 */
export function addSlug(markdown, rel) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---(\r?\n|$)/.exec(markdown);
  if (!match) throw new Error(`docs/${rel} has no front matter`);
  if (/^slug\s*:/m.test(match[1])) throw new Error(`docs/${rel} sets its own slug`);
  const slug = pageUrl(rel).replace(/^\/|\/$/g, '');
  // The index would otherwise read `$ docs / agents / agents` in its eyebrow.
  const eyebrow = outputPath(rel) === 'index.md' && !/^eyebrow\s*:/m.test(match[1]) ? 'eyebrow: overview\n' : '';
  return `---\nslug: ${slug}\n${eyebrow}${markdown.slice(markdown.indexOf('\n') + 1)}`;
}

// ---- sync -------------------------------------------------------------------

/** Replace outDir with the published pages of a verified archive. */
export async function writeDocs(tarGz, lock, outDir = OUT_DIR) {
  const files = selectDocs(gunzipSync(tarGz), lock);
  const outputs = new Map();
  for (const [rel, data] of files) {
    const out = outputPath(rel);
    if (outputs.has(out)) throw new Error(`docs/${rel} and docs/${outputs.get(out).rel} both map to ${out}`);
    const body = isPage(rel)
      ? Buffer.from(addSlug(rewriteLinks(data.toString('utf8'), rel, files, lock), rel), 'utf8')
      : data;
    outputs.set(out, { rel, body });
  }
  const root = path.resolve(outDir);
  await rm(root, { recursive: true, force: true });
  for (const [out, { body }] of outputs) {
    const target = path.resolve(root, out);
    if (!target.startsWith(root + path.sep)) throw new Error(`refusing to write ${out} outside ${root}`);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, body);
  }
  return [...outputs.keys()].sort();
}

export async function syncAgentsDocs({
  lockPath = LOCK_PATH,
  outDir = OUT_DIR,
  cacheDir = CACHE_DIR,
  fetchImpl = globalThis.fetch,
} = {}) {
  const lock = await readLock(lockPath);
  const archive = await fetchArchive(lock, { cacheDir, fetchImpl });
  const written = await writeDocs(archive, lock, outDir);
  return { lock, written };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const { lock, written } = await syncAgentsDocs();
    console.log(`agents docs: ${written.length} files from ${lock.repo}@${lock.commit.slice(0, 12)} in ${path.relative(SITE, OUT_DIR)}/`);
  } catch (error) {
    console.error(`agents docs sync failed: ${error.message}`);
    process.exit(1);
  }
}
