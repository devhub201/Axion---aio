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
CREATE TABLE IF NOT EXISTS levels(
    guild_id INTEGER, user_id INTEGER, xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0, msgs INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id, user_id));

CREATE TABLE IF NOT EXISTS economy(
    guild_id INTEGER, user_id INTEGER, wallet INTEGER DEFAULT 0, last_daily REAL DEFAULT 0,
    last_work REAL DEFAULT 0, streak INTEGER DEFAULT 0, PRIMARY KEY(guild_id, user_id));

CREATE TABLE IF NOT EXISTS shop(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, price INTEGER,
    role_id INTEGER, description TEXT, UNIQUE(guild_id, name));

CREATE TABLE IF NOT EXISTS reminders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, guild_id INTEGER, channel_id INTEGER,
    text TEXT, due REAL);

CREATE TABLE IF NOT EXISTS afk(guild_id INTEGER, user_id INTEGER, reason TEXT, since REAL, PRIMARY KEY(guild_id, user_id));

CREATE TABLE IF NOT EXISTS stickies(channel_id INTEGER PRIMARY KEY, guild_id INTEGER, text TEXT, last_msg_id INTEGER);

CREATE TABLE IF NOT EXISTS responders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, trigger TEXT, reply TEXT, UNIQUE(guild_id, trigger));

CREATE TABLE IF NOT EXISTS vouches(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, stars INTEGER, text TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS suggestions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, message_id INTEGER UNIQUE, user_id INTEGER,
    text TEXT, status TEXT DEFAULT 'open');

CREATE TABLE IF NOT EXISTS suggestion_votes(message_id INTEGER, user_id INTEGER, vote INTEGER, PRIMARY KEY(message_id, user_id));

CREATE TABLE IF NOT EXISTS polls(
    message_id INTEGER PRIMARY KEY, guild_id INTEGER, question TEXT, options TEXT, ends_at REAL,
    ended INTEGER DEFAULT 0, channel_id INTEGER);

CREATE TABLE IF NOT EXISTS poll_votes(message_id INTEGER, user_id INTEGER, choice INTEGER, PRIMARY KEY(message_id, user_id));

CREATE TABLE IF NOT EXISTS starred(message_id INTEGER PRIMARY KEY, guild_id INTEGER, board_msg_id INTEGER);

CREATE TABLE IF NOT EXISTS birthdays(guild_id INTEGER, user_id INTEGER, month INTEGER, day INTEGER, PRIMARY KEY(guild_id, user_id));

CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, ticket_id INTEGER, user_id INTEGER, staff_id INTEGER,
    plan TEXT, amount REAL, method TEXT, created_at REAL);
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
            for stmt in ("ALTER TABLE tickets ADD COLUMN rating INTEGER",
                         "ALTER TABLE giveaways ADD COLUMN req_role INTEGER",
                         "ALTER TABLE giveaways ADD COLUMN req_invites INTEGER",
                         "ALTER TABLE giveaways ADD COLUMN req_level INTEGER"):
                try:
                    self._conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass                              # column already exists
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
