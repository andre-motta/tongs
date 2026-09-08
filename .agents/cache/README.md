# Cache

## CacheStore

Defined in `src/tongs/cache/store.py`. Async SQLite-backed cache for forge API responses with TTL expiration and LRU eviction.

## Storage

- **Database:** SQLite via `aiosqlite` (async wrapper)
- **Location:** `platformdirs.user_cache_dir("tongs") / "cache.db"`
- **Journal mode:** WAL (`PRAGMA journal_mode=WAL`) for concurrent read/write
- **File permissions:** Directory `0o700`, database file `0o600` (created via `os.open` before aiosqlite opens it)

## Schema

```sql
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL,
    expires_at REAL NOT NULL,    -- Unix timestamp for TTL
    created_at REAL NOT NULL,    -- Unix timestamp, updated on read (LRU)
    size_bytes INTEGER NOT NULL  -- For size-based eviction
)
```

## API

### Core Methods

- `open()` -- Create directory/file, connect, enable WAL, create table. Must be called before any other method.
- `get(key) -> bytes | None` -- Return cached value if key exists and not expired. Updates `created_at` on hit (LRU touch). Returns None if not found, expired, or excluded.
- `put(key, value, ttl)` -- Insert or replace entry. `ttl` is seconds from now. Calls `_enforce_size_limit()` after write.
- `invalidate(key)` -- Delete a single entry.
- `invalidate_prefix(prefix)` -- Delete all entries matching prefix (SQL `LIKE prefix%`).
- `clear()` -- Delete all entries.
- `prune()` -- Delete all expired entries.
- `close()` -- Close the aiosqlite connection.

### Convenience Methods

- `get_json(key) -> dict | list | None` -- Calls `get()`, deserializes JSON.
- `put_json(key, value, ttl)` -- Serializes to JSON bytes, calls `put()`.

## Eviction Strategy

**TTL:** Each entry has an `expires_at` timestamp. `get()` checks TTL on read; expired entries return None but are not proactively deleted (use `prune()` for that).

**LRU:** On cache hit, `get()` updates `created_at` to the current time. When size limit is exceeded, the oldest entries (by `created_at`) are evicted first.

**Size limit:** `_enforce_size_limit()` runs after every `put()`. If total `size_bytes` exceeds `max_size_bytes` (default 100 MB, configurable via `Config.max_cache_size_mb`), it deletes the oldest 25% of entries by `created_at`.

## Excluded Keys

Keys starting with `_EXCLUDED_PREFIXES` (`"job_log:"`, `"stream_log:"`) are silently ignored by both `get()` and `put()`. This prevents large, ephemeral CI log data from being cached.

## CachedForgeClient

`src/tongs/cache/cached_client.py` wraps any `ForgeClient` with transparent SQLite caching. `ForgeRegistry.get_client()` automatically wraps every forge client in `CachedForgeClient` when a cache is configured.

- **Cached reads:** `list_mrs` (keyed by repo_path + state, TTL = mr_list_ttl) and `get_mr_diff` (keyed by repo_path + MR number, TTL = diff_ttl). On cache hit, returns deserialized JSON without an API call.
- **Mutation coherence:** approve/unapprove, close/reopen, general and inline
  comments, replies, discussion resolution, and review submission all call
  `_finish_review_mutation()`. It marks the review and list prefixes dirty
  before scheduling invalidation, so reads bypass stale cache while cleanup is
  pending or has failed. Mutation receipts report cache invalidation
  conservatively rather than claiming the background delete already completed.
- **Merge coherence:** `merge_mr` marks the whole repository prefix dirty and
  schedules the broader invalidation.
- **Explicit invalidation:** `invalidate_review_reads()` uses the same generation
  guard and clears a dirty prefix only after all matching invalidations succeed.
- **Pass-through:** Operations without an explicit cache or coherence wrapper,
  including pipeline reads/mutations and job logs, delegate through
  `__getattr__`. Job logs remain excluded from storage.
- **Serialization:** `_mr_summary_to_dict` / `_dict_to_mr_summary` handle MRSummary round-trip through JSON, including enum values (CIStatus, MRState, ForgeType) and datetime fields.

## Clear Cache operations

The terminal command palette clears the session-owned cache through
`app.cache.clear()` and shows a notification. The production desktop invokes the
exact `utilities.cache_clear` protocol method; its handler calls
`ApplicationSession.clear_cache()` and then emits `RESYNC_REQUIRED` so desktop
views refetch current data. Neither path deletes durable review drafts, editor
exports, configuration, or arbitrary files.

The desktop renderer has no generic cache-key or path API. The sidecar owns the
clear authority and ignores renderer paths or keys.

## Integration

- `ApplicationSession.start()` creates or opens
  `CacheStore(max_size_mb=config.max_cache_size_mb)` and passes it to
  `ForgeRegistry`.
- `ForgeRegistry` wraps clients in `CachedForgeClient` when a cache is present.
- `TongsApp` and `TUIServiceAdapter` use the session; `cache` and
  `forge_registry` properties on the app remain compatibility views of
  session-owned resources.
- The production sidecar creates its own session and closes it when the protocol
  connection ends.
- `ApplicationSession.close()` closes forge clients, drafts, and the cache with
  bounded cleanup and retains a safe cleanup failure when necessary.
- Cache size is configurable via `[cache] max_size_mb` (default 100). TTLs are
  `mr_list_ttl` (default 60 seconds) and `diff_ttl` (default 300 seconds).

## Security

- Database file is created with `0o600` (owner-only read/write)
- Cache directory is created with `0o700` (owner-only access)
- Tokens are never stored in cache keys or values
- Job logs are excluded from caching via prefix filter
- WAL file inherits database permissions

## Testing

Tests in `tests/test_cache/test_store.py` cover:
- Store open/close lifecycle
- get/put with TTL expiration
- LRU eviction behavior
- JSON convenience methods
- Prefix invalidation
- Excluded key prefixes
- Size limit enforcement

`tests/test_cache/test_cached_client.py` covers read caching, review and merge
dirty-prefix coherence, invalidation failure, bounded background cleanup, close
behavior, and delegation of uncached methods.
