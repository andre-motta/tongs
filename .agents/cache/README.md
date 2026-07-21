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
- **Mutation invalidation:** `approve_mr`, `unapprove_mr`, `close_mr`, `reopen_mr` invalidate MR list entries. `merge_mr` invalidates all entries for the repo (broader scope).
- **Pass-through:** All other methods (comments, discussions, pipelines, etc.) forward directly to the inner client via `__getattr__`.
- **Serialization:** `_mr_summary_to_dict` / `_dict_to_mr_summary` handle MRSummary round-trip through JSON, including enum values (CIStatus, MRState, ForgeType) and datetime fields.

## Clear Cache Command

The command palette includes a "Clear Cache" command that calls `cache.clear()` to delete all cached entries. Useful when cached data is stale or the user wants to force a fresh fetch.

## Integration

- `TongsApp.__init__()` creates `CacheStore(max_size_mb=config.max_cache_size_mb)`.
- `TongsApp.on_mount()` calls `cache.open()`.
- `ForgeRegistry` receives the cache instance in its constructor and wraps clients in `CachedForgeClient`.
- `TongsApp.on_unmount()` calls `cache.close()`.
- Cache size is configurable via `[cache] max_size_mb` in `config.toml` (default 100).
- TTL values are configurable: `mr_list_ttl` (default 60s), `diff_ttl` (default 300s).

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
