"""SQLite storage (stdlib only). All queries run in a worker thread so the bot never blocks."""
import asyncio
import json
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id, key));

CREATE TABLE IF NOT EXISTS tickets(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER UNIQUE,
    user_id INTEGER, category TEXT, subject TEXT, status TEXT DEFAULT 'open',
    claimed_by INTEGER, ai_enabled INTEGER DEFAULT 1, ai_replies INTEGER DEFAULT 0,
    created_at REAL, closed_at REAL);

CREATE TABLE IF NOT EXISTS warnings(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER,
    mod_id INTEGER, reason TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS invites(
    guild_id INTEGER, member_id INTEGER, inviter_id INTEGER, code TEXT, joined_at REAL,
    PRIMARY KEY(guild_id, member_id));

CREATE TABLE IF NOT EXISTS giveaways(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER,
    message_id INTEGER, prize TEXT, winners INTEGER, ends_at REAL, host_id INTEGER,
    ended INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS giveaway_entries(
    giveaway_id INTEGER, user_id INTEGER, PRIMARY KEY(giveaway_id, user_id));

CREATE TABLE IF NOT EXISTS services(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, kind TEXT,
    target TEXT, last_ok INTEGER DEFAULT 1, maint INTEGER DEFAULT 0, UNIQUE(guild_id, name));
"""


class Database:
    def __init__(self, path="data/axionhost.db"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- raw helpers ---------------------------------------------------------
    def _exec(self, sql, params):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid, cur.rowcount

    def _all(self, sql, params):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    async def execute(self, sql, params=()):
        """Returns lastrowid."""
        return (await asyncio.to_thread(self._exec, sql, tuple(params)))[0]

    async def rowcount(self, sql, params=()):
        return (await asyncio.to_thread(self._exec, sql, tuple(params)))[1]

    async def fetchall(self, sql, params=()):
        return await asyncio.to_thread(self._all, sql, tuple(params))

    async def fetchone(self, sql, params=()):
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    # -- JSON settings ---------------------------------------------------------
    async def get(self, guild_id, key, default=None):
        row = await self.fetchone("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    async def set(self, guild_id, key, value):
        await self.execute(
            "INSERT INTO settings(guild_id,key,value) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
            (guild_id, key, json.dumps(value)))

    async def cfg(self, guild_id, key, defaults):
        """Stored dict merged over defaults (new options appear automatically after updates)."""
        merged = json.loads(json.dumps(defaults))
        stored = await self.get(guild_id, key, {})
        if isinstance(stored, dict):
            merged.update(stored)
        return merged

    async def cfg_set(self, guild_id, key, defaults, **changes):
        data = await self.cfg(guild_id, key, defaults)
        data.update(changes)
        await self.set(guild_id, key, data)
        return data
