# -*- coding: utf-8 -*-
"""
Мини-база (SQLite) в файле data/bot.db.

Хранит:
  * messages   — всю переписку, которую видит бот (входящую и исходящую);
  * chats      — настройки по каждому чату (режим, пауза, последняя связка business);
  * facts      — «память» о собеседниках (что бот запомнил про человека);
  * notes      — заметки владельца;
  * drafts     — черновики ответов (режим «подсказки»);
  * connections— какие бизнес-аккаунты подключены к боту.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime

from bot.config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    chat_title TEXT,
    user_id    INTEGER,
    user_name  TEXT,
    username   TEXT,
    text       TEXT,
    is_out     INTEGER DEFAULT 0,
    kind       TEXT DEFAULT 'text',
    tg_id      INTEGER,
    conn_id    TEXT,
    by_bot     INTEGER DEFAULT 0,   -- 1 = сообщение написал сам бот, 0 = человек
    created    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS chats (
    chat_id         INTEGER PRIMARY KEY,
    title           TEXT,
    username        TEXT,
    conn_id         TEXT,
    mode            TEXT,              -- NULL = как в общих настройках
    paused_until    REAL DEFAULT 0,
    last_reply      REAL DEFAULT 0,
    minute_start    REAL DEFAULT 0,
    replies_minute  INTEGER DEFAULT 0,
    note            TEXT,
    updated         TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS facts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    text    TEXT NOT NULL,
    created TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    text    TEXT NOT NULL,
    done    INTEGER DEFAULT 0,
    created TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS drafts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    conn_id TEXT,
    source  TEXT,
    draft   TEXT,
    status  TEXT DEFAULT 'new',        -- new | sent | dropped | manual
    created TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS connections (
    conn_id    TEXT PRIMARY KEY,
    user_id    INTEGER,
    username   TEXT,
    name       TEXT,
    is_enabled INTEGER DEFAULT 1,
    rights     TEXT,
    updated    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Общая память владельца: предпочтения, правила, важные факты о людях
CREATE TABLE IF NOT EXISTS memory (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    text    TEXT NOT NULL,
    created TEXT DEFAULT (datetime('now'))
);

-- Обучение на ответах владельца: пара «сообщение собеседника → ответ владельца»
CREATE TABLE IF NOT EXISTS style_pairs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER,
    incoming TEXT,
    outgoing TEXT,
    created  TEXT DEFAULT (datetime('now'))
);
"""


def init() -> None:
    """Создаёт папку data/ и таблицы, если их ещё нет (+ маленькие миграции)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
        cols = {row["name"] for row in con.execute("PRAGMA table_info(messages)")}
        if "conn_id" not in cols:
            con.execute("ALTER TABLE messages ADD COLUMN conn_id TEXT")
        if "by_bot" not in cols:
            con.execute("ALTER TABLE messages ADD COLUMN by_bot INTEGER DEFAULT 0")


@contextmanager
def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_ts() -> float:
    return time.time()


# ----------------------------------------------------------------- сообщения
def log_message(
    chat_id: int,
    text: str | None,
    *,
    chat_title: str | None = None,
    user_id: int | None = None,
    user_name: str | None = None,
    username: str | None = None,
    is_out: bool = False,
    kind: str = "text",
    tg_id: int | None = None,
    conn_id: str | None = None,
    by_bot: bool = False,
) -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO messages
               (chat_id, chat_title, user_id, user_name, username, text, is_out, kind, tg_id, conn_id,
                by_bot, created)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (chat_id, chat_title, user_id, user_name, username, text,
             int(bool(is_out)), kind, tg_id, conn_id, int(bool(by_bot)), now_str()),
        )


def recent(chat_id: int, limit: int = 20) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def search(query: str, limit: int = 30) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM messages WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def last_conn_id(chat_id: int) -> str | None:
    """Какая бизнес-связка последней использовалась в этом чате."""
    with connect() as con:
        row = con.execute("SELECT conn_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if row and row["conn_id"]:
            return row["conn_id"]
        row = con.execute(
            """SELECT conn_id FROM messages WHERE chat_id=? AND conn_id IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
    return row["conn_id"] if row else None


# --------------------------------------------------------------- настройки чата
def ensure_chat(chat_id: int, title: str | None = None,
                username: str | None = None, conn_id: str | None = None) -> dict:
    with connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO chats (chat_id, title, username, conn_id) VALUES (?,?,?,?)",
            (chat_id, title, username, conn_id),
        )
        sets, values = [], []
        if title:
            sets.append("title=?"); values.append(title)
        if username:
            sets.append("username=?"); values.append(username)
        if conn_id:
            sets.append("conn_id=?"); values.append(conn_id)
        if sets:
            sets.append("updated=?"); values.append(now_str())
            values.append(chat_id)
            con.execute(f"UPDATE chats SET {', '.join(sets)} WHERE chat_id=?", values)
        row = con.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else {}


def get_chat(chat_id: int) -> dict:
    with connect() as con:
        row = con.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    return dict(row) if row else {}


def set_chat(chat_id: int, **fields) -> None:
    fields = {k: v for k, v in fields.items() if v is not None or k in ("mode", "note")}
    if not fields:
        return
    ensure_chat(chat_id)
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE chats SET {sets}, updated=? WHERE chat_id=?",
                    [*fields.values(), now_str(), chat_id])


def all_chats(limit: int = 60) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            """SELECT c.*,
                      (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.chat_id) AS msg_count,
                      (SELECT MAX(created) FROM messages m WHERE m.chat_id=c.chat_id) AS last_msg
               FROM chats c ORDER BY last_msg DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------- память
def add_fact(chat_id: int, text: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO facts (chat_id, text) VALUES (?,?)", (chat_id, text.strip()))


def facts(chat_id: int, limit: int = 20) -> list[str]:
    with connect() as con:
        rows = con.execute(
            "SELECT text FROM facts WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [r["text"] for r in reversed(rows)]


def clear_facts(chat_id: int) -> int:
    with connect() as con:
        cur = con.execute("DELETE FROM facts WHERE chat_id=?", (chat_id,))
    return cur.rowcount


def add_note(text: str) -> int:
    with connect() as con:
        cur = con.execute("INSERT INTO notes (text) VALUES (?)", (text.strip(),))
    return cur.lastrowid


def notes(only_open: bool = True, limit: int = 30) -> list[dict]:
    sql = "SELECT * FROM notes"
    if only_open:
        sql += " WHERE done=0"
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as con:
        rows = con.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def close_note(note_id: int) -> bool:
    with connect() as con:
        cur = con.execute("UPDATE notes SET done=1 WHERE id=?", (note_id,))
    return cur.rowcount > 0


# ------------------------------------------------------------------ черновики
def add_draft(chat_id: int, conn_id: str, source: str, draft: str) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO drafts (chat_id, conn_id, source, draft) VALUES (?,?,?,?)",
            (chat_id, conn_id, source, draft),
        )
    return cur.lastrowid


def get_draft(draft_id: int) -> dict:
    with connect() as con:
        row = con.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    return dict(row) if row else {}


def update_draft(draft_id: int, *, draft: str | None = None, status: str | None = None) -> None:
    with connect() as con:
        if draft is not None:
            con.execute("UPDATE drafts SET draft=? WHERE id=?", (draft, draft_id))
        if status is not None:
            con.execute("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))


def draft_for_chat(chat_id: int) -> dict:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM drafts WHERE chat_id=? AND status='new' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    return dict(row) if row else {}


def close_drafts(chat_id: int, status: str = "manual") -> int:
    with connect() as con:
        cur = con.execute(
            "UPDATE drafts SET status=? WHERE chat_id=? AND status='new'", (status, chat_id)
        )
    return cur.rowcount


# ---------------------------------------------------------------- подключения
def save_connection(conn_id: str, user_id: int | None, username: str | None,
                    name: str | None, is_enabled: bool, rights: str = "") -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO connections (conn_id, user_id, username, name, is_enabled, rights, updated)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(conn_id) DO UPDATE SET
                 user_id=excluded.user_id, username=excluded.username, name=excluded.name,
                 is_enabled=excluded.is_enabled, rights=excluded.rights, updated=excluded.updated""",
            (conn_id, user_id, username, name, int(bool(is_enabled)), rights, now_str()),
        )


def connections() -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM connections ORDER BY updated DESC").fetchall()
    return [dict(r) for r in rows]


def connection_owner(conn_id: str) -> int | None:
    with connect() as con:
        row = con.execute("SELECT user_id FROM connections WHERE conn_id=?", (conn_id,)).fetchone()
    return row["user_id"] if row else None


# --------------------------------------------------- память владельца (общая)
def remember(text: str) -> int:
    """Запомнить что-то навсегда: предпочтение, правило, факт о человеке."""
    with connect() as con:
        cur = con.execute("INSERT INTO memory (text) VALUES (?)", (text.strip(),))
    return cur.lastrowid


def memory_items(limit: int = 50) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM memory ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def memory_texts(limit: int = 30) -> list[str]:
    with connect() as con:
        rows = con.execute(
            "SELECT text FROM memory ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [r["text"] for r in reversed(rows)]


def forget_memory(memory_id: int | None = None) -> int:
    """Удаляет одну запись памяти или всю память (memory_id=None)."""
    with connect() as con:
        if memory_id is None:
            cur = con.execute("DELETE FROM memory")
        else:
            cur = con.execute("DELETE FROM memory WHERE id=?", (memory_id,))
    return cur.rowcount


# --------------------------------------------------- обучение стилю владельца
def add_style_pair(chat_id: int, incoming: str | None, outgoing: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO style_pairs (chat_id, incoming, outgoing) VALUES (?,?,?)",
            (chat_id, (incoming or "")[:500], outgoing[:1000]),
        )


def style_pairs(chat_id: int | None = None, limit: int = 5) -> list[dict]:
    """Примеры «что писали мне → как я ответил» — сначала из этого же чата."""
    with connect() as con:
        if chat_id:
            rows = con.execute(
                "SELECT incoming, outgoing FROM style_pairs WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
            if rows:
                return [dict(r) for r in reversed(rows)]
        rows = con.execute(
            "SELECT incoming, outgoing FROM style_pairs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def last_incoming(chat_id: int) -> str | None:
    with connect() as con:
        row = con.execute(
            """SELECT text FROM messages WHERE chat_id=? AND is_out=0 AND text IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
    return row["text"] if row else None


def owner_replies(limit: int = 60, chat_id: int | None = None) -> list[str]:
    """Как владелец пишет сам — на этом учимся его манере.

    Сообщения самого бота (by_bot=1) не берём: иначе он учился бы на своём же тексте.
    """
    sql = ("SELECT text FROM messages WHERE is_out=1 AND by_bot=0 "
           "AND text IS NOT NULL AND text != ''")
    args: list = []
    if chat_id:
        sql += " AND chat_id=?"
        args.append(chat_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as con:
        rows = con.execute(sql, args).fetchall()
    return [r["text"] for r in reversed(rows)]


def pair_count() -> int:
    with connect() as con:
        return con.execute("SELECT COUNT(*) AS n FROM style_pairs").fetchone()["n"]


# --------------------------------------------------------------- статистика
def set_setting(key: str, value: str) -> None:
    """Настройки, которые должны переживать пересборку контейнера на хостинге."""
    with connect() as con:
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_setting(key: str, default=None):
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def stats() -> dict:
    with connect() as con:
        total = con.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        incoming = con.execute("SELECT COUNT(*) AS n FROM messages WHERE is_out=0").fetchone()["n"]
        outgoing = total - incoming
        chats = con.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"]
        today = con.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE date(created)=date('now')"
        ).fetchone()["n"]
        drafts = con.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE status='new'"
        ).fetchone()["n"]
    return {"total": total, "incoming": incoming, "outgoing": outgoing,
            "chats": chats, "today": today, "drafts": drafts}


def top_chats(limit: int = 8) -> list[dict]:
    with connect() as con:
        rows = con.execute(
            """SELECT chat_id, chat_title, COUNT(*) AS n, MAX(created) AS last_msg
               FROM messages GROUP BY chat_id ORDER BY n DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
