"""SQLite storage. One connection shared by the app, guarded by a lock."""
import json
import os
import sqlite3
import threading
import time

DATA_DIR = os.environ.get("JM_DATA", "/data")
DB_PATH = os.path.join(DATA_DIR, "jellymirror.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS servers (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  url TEXT NOT NULL,
  api_key TEXT NOT NULL,
  access TEXT NOT NULL DEFAULT 'none',      -- local | ssh | none
  ssh_host TEXT, ssh_port INTEGER DEFAULT 22, ssh_user TEXT,
  path_maps TEXT NOT NULL DEFAULT '[]',     -- [[jellyfin_prefix, fs_prefix], ...]
  enabled INTEGER NOT NULL DEFAULT 1,
  lib_cursor TEXT,                          -- ISO time of last library poll
  status TEXT, status_at REAL
);

CREATE TABLE IF NOT EXISTS persons (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  jf_user_id TEXT NOT NULL, jf_user_name TEXT,
  watch_cursor TEXT,
  UNIQUE(server_id, jf_user_id)
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY,
  source_server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  source_item_id TEXT NOT NULL,
  item_type TEXT NOT NULL,                  -- Series | Movie
  title TEXT NOT NULL, year INTEGER,
  provider_ids TEXT NOT NULL DEFAULT '{}',
  sync_watch INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  src_sig TEXT,                             -- fingerprint of the title's files on the source
  created REAL
);
CREATE TABLE IF NOT EXISTS rule_users (
  rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  PRIMARY KEY(rule_id, person_id)
);
CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY,
  rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
  server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  sync_files INTEGER NOT NULL DEFAULT 1,
  root TEXT,                                -- jellyfin path of the target library folder
  template TEXT,
  method TEXT NOT NULL DEFAULT 'auto',      -- auto | rsync | api
  delay_seconds INTEGER NOT NULL DEFAULT 300,
  bwlimit_kbps INTEGER NOT NULL DEFAULT 0,
  due_at REAL,
  matched_item_id TEXT,                     -- the title's item id on the target server
  last_eval REAL, last_result TEXT,
  UNIQUE(rule_id, server_id)
);
CREATE TABLE IF NOT EXISTS placed (
  id INTEGER PRIMARY KEY,
  target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  dest_path TEXT NOT NULL,                  -- jellyfin path on the target
  source_path TEXT, source_size INTEGER,
  owned INTEGER NOT NULL DEFAULT 1,         -- 0 = file was already there, never delete
  created REAL,
  UNIQUE(target_id, dest_path)
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                       -- copy | delete
  source_item_id TEXT,
  src_path TEXT, dest_path TEXT NOT NULL,
  size INTEGER, method TEXT,
  status TEXT NOT NULL DEFAULT 'queued',    -- queued | running | paused | done | failed | cancelled
  progress INTEGER NOT NULL DEFAULT 0,
  error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  created REAL, updated REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS applied (            -- echo suppression for watch sync
  server_id INTEGER, user_id TEXT, item_id TEXT, state TEXT, at REAL,
  PRIMARY KEY(server_id, user_id, item_id)
);
CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY, at REAL, level TEXT, msg TEXT);
"""

DEFAULT_SETTINGS = {
    "lib_poll_seconds": "120",
    "watch_poll_seconds": "30",
    "reconcile_hours": "6",
    "max_parallel_jobs": "2",
    "series_template": "{title} ({year})/Season {season:02}/{filename}",
    "movie_template": "{title} ({year})/{filename}",
}

_lock = threading.RLock()
_conn = None


def connect():
    global _conn
    os.makedirs(DATA_DIR, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        _conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    _migrate()
    # Jobs that were running when the container stopped resume on next start.
    _conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")


def _migrate():
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(rules)")}
    if "src_sig" not in cols:
        _conn.execute("ALTER TABLE rules ADD COLUMN src_sig TEXT")
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(servers)")}
    for c in ("up_kbps", "down_kbps"):     # server-wide speed limits, 0 = unlimited
        if c not in cols:
            _conn.execute(f"ALTER TABLE servers ADD COLUMN {c} INTEGER NOT NULL DEFAULT 0")


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        return cur.lastrowid


def setting(key, cast=str):
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    val = row["value"] if row else DEFAULT_SETTINGS.get(key)
    return cast(val)


def set_setting(key, value):
    x("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, str(value)))


def log(msg, level="info"):
    print(f"[{level}] {msg}", flush=True)
    x("INSERT INTO log(at, level, msg) VALUES (?, ?, ?)", (time.time(), level, msg))
    x("DELETE FROM log WHERE id < (SELECT MAX(id) - 5000 FROM log)")


def server(sid):
    s = one("SELECT * FROM servers WHERE id=?", (sid,))
    if s:
        s["path_maps"] = json.loads(s["path_maps"] or "[]")
    return s


def servers(enabled_only=False):
    rows = q("SELECT * FROM servers" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY name")
    for s in rows:
        s["path_maps"] = json.loads(s["path_maps"] or "[]")
    return rows
