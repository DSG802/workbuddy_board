"""SQLite 持久层：账户 / 会话 / 事件流水 / 积分快照。

单账户 payload < 100KB、账户数十个以内，单文件 SQLite 足够；WAL + busy_timeout 处理并发写。

表结构
  accounts(uid PK, token, token_exp, refresh_token, payload, created, updated_at)
      payload = 原 users/<uid>.json 的完整内容（含 snap / hist）
  sessions(sid PK, uid, at)                     wb_sid → uid，90 天过期清理
  events(id, uid, ts, kind, credit, ok, msg, payload)   签到/旅行流水
  snapshots(id, uid, ts, balance, total, payload)       每次刷新一条
  meta(key PK, value)                           迁移标记
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_DB = ""
_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  uid          TEXT PRIMARY KEY,
  token        TEXT,
  token_exp    TEXT,
  refresh_token TEXT,
  payload      TEXT NOT NULL,
  created      INTEGER,
  updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
  sid TEXT PRIMARY KEY,
  uid TEXT NOT NULL,
  at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_at ON sessions(at);
CREATE TABLE IF NOT EXISTS events(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  uid     TEXT NOT NULL,
  ts      TEXT,
  kind    TEXT,
  credit  INTEGER,
  ok      INTEGER,
  msg     TEXT,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_uid ON events(uid, id DESC);
CREATE TABLE IF NOT EXISTS snapshots(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  uid     TEXT NOT NULL,
  ts      TEXT,
  balance INTEGER,
  total   INTEGER,
  payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_uid ON snapshots(uid, id DESC);
CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS devices(
  dev TEXT NOT NULL,
  uid TEXT NOT NULL,
  at  INTEGER NOT NULL,
  PRIMARY KEY (dev, uid)
);
CREATE INDEX IF NOT EXISTS idx_devices_dev ON devices(dev, at DESC);
"""


def _con() -> sqlite3.Connection:
    con = getattr(_LOCAL, "con", None)
    if con is None:
        con = sqlite3.connect(_DB, timeout=10, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=8000")
        con.row_factory = sqlite3.Row
        _LOCAL.con = con
    return con


def init(db_path: str) -> None:
    """建库建表（幂等）。含 token，权限 600。"""
    global _DB
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _DB = db_path
    con = _con()
    con.executescript(SCHEMA)
    con.commit()
    try:
        os.chmod(db_path, 0o600)
        for suffix in ("-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.chmod(p, 0o600)
    except OSError:
        pass


def _meta_get(key: str) -> str:
    r = _con().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else ""


def _meta_set(key: str, value: str) -> None:
    con = _con()
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()


def get_account(uid: str) -> dict | None:
    if not uid:
        return None
    r = _con().execute("SELECT payload FROM accounts WHERE uid=?", (uid,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["payload"])
    except ValueError:
        return None


def put_account(u: dict) -> None:
    if not u or not u.get("uid"):
        return
    con = _con()
    con.execute(
        "INSERT INTO accounts(uid,token,token_exp,refresh_token,payload,created,updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET "
        "token=excluded.token, token_exp=excluded.token_exp, "
        "refresh_token=COALESCE(excluded.refresh_token, accounts.refresh_token), "
        "payload=excluded.payload, updated_at=excluded.updated_at",
        (u["uid"], u.get("token") or "", u.get("token_exp"),
         u.get("refresh_token"), json.dumps(u, ensure_ascii=False),
         int(u.get("created") or time.time()), u.get("updated_at")))
    con.commit()


def account_uids() -> list[str]:
    return [r["uid"] for r in
            _con().execute("SELECT uid FROM accounts ORDER BY created").fetchall()]


def has_account(uid: str) -> bool:
    return bool(_con().execute("SELECT 1 FROM accounts WHERE uid=?", (uid,)).fetchone())


def session_uid(sid: str) -> str:
    if not sid:
        return ""
    r = _con().execute("SELECT uid FROM sessions WHERE sid=?", (sid,)).fetchone()
    return r["uid"] if r else ""


def put_session(sid: str, uid: str, ttl: int) -> None:
    con = _con()
    t = int(time.time())
    con.execute("INSERT INTO sessions(sid,uid,at) VALUES(?,?,?) "
                "ON CONFLICT(sid) DO UPDATE SET uid=excluded.uid, at=excluded.at",
                (sid, uid, t))
    con.execute("DELETE FROM sessions WHERE at < ?", (t - ttl,))
    con.commit()


def del_session(sid: str) -> None:
    con = _con()
    con.execute("DELETE FROM sessions WHERE sid=?", (sid,))
    con.commit()


def session_count() -> int:
    r = _con().execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
    return int(r["n"] if r else 0)


def all_sessions() -> dict:
    """兼容 checkin.py 老的 sessions() 形状：{sid: {"uid": uid, "at": ts}}。"""
    rows = _con().execute("SELECT sid,uid,at FROM sessions").fetchall()
    return {r["sid"]: {"uid": r["uid"], "at": r["at"]} for r in rows}


def put_device(dev: str, uid: str) -> None:
    if not dev or not uid:
        return
    con = _con()
    con.execute("INSERT INTO devices(dev,uid,at) VALUES(?,?,?) "
                "ON CONFLICT(dev,uid) DO UPDATE SET at=excluded.at",
                (dev, uid, int(time.time())))
    con.commit()


def device_uids(dev: str) -> list[str]:
    """最近用过的排前面。"""
    if not dev:
        return []
    rows = _con().execute(
        "SELECT uid FROM devices WHERE dev=? ORDER BY at DESC", (dev,)).fetchall()
    return [r["uid"] for r in rows]


def device_has(dev: str, uid: str) -> bool:
    if not dev or not uid:
        return False
    return bool(_con().execute("SELECT 1 FROM devices WHERE dev=? AND uid=?",
                               (dev, uid)).fetchone())


def del_device(dev: str, uid: str) -> None:
    con = _con()
    con.execute("DELETE FROM devices WHERE dev=? AND uid=?", (dev, uid))
    con.commit()


def del_account(uid: str) -> None:
    """彻底移除一个账户：凭据 + 会话 + 设备关联全清，events / snapshots 保留。"""
    if not uid:
        return
    con = _con()
    con.execute("DELETE FROM accounts WHERE uid=?", (uid,))
    con.execute("DELETE FROM sessions WHERE uid=?", (uid,))
    con.execute("DELETE FROM devices WHERE uid=?", (uid,))
    con.commit()


def add_event(uid: str, item: dict) -> None:
    if not uid:
        return
    con = _con()
    con.execute(
        "INSERT INTO events(uid,ts,kind,credit,ok,msg,payload) VALUES(?,?,?,?,?,?,?)",
        (uid, item.get("ts"), item.get("kind"),
         int(item.get("credit") or 0), 1 if item.get("ok") else 0,
         item.get("msg"), json.dumps(item, ensure_ascii=False)))
    con.commit()


def list_events(uid: str, limit: int = 60) -> list[dict]:
    """新 → 旧（看板渲染顺序）。"""
    rows = _con().execute(
        "SELECT payload FROM events WHERE uid=? ORDER BY id DESC LIMIT ?",
        (uid, max(1, int(limit)))).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except ValueError:
            pass
    return out


def event_count(uid: str) -> int:
    r = _con().execute("SELECT COUNT(*) AS n FROM events WHERE uid=?", (uid,)).fetchone()
    return int(r["n"] if r else 0)


def backfill_events(uid: str, jsonl_path, limit: int = 300) -> int:
    """把老的 history.jsonl 尾部导入 events（仅当该账户还没有任何流水，幂等）。"""
    if not uid or event_count(uid):
        return 0
    p = Path(jsonl_path)
    if not p.is_file():
        return 0
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return 0
    n = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            add_event(uid, rec)
            n += 1
    return n


def add_snapshot(uid: str, snap: dict) -> None:
    if not uid or not snap:
        return
    con = _con()
    con.execute(
        "INSERT INTO snapshots(uid,ts,balance,total,payload) VALUES(?,?,?,?,?)",
        (uid, snap.get("balance_at"), int(snap.get("balance") or 0),
         int(snap.get("balance_total") or 0), json.dumps(snap, ensure_ascii=False)))
    # 每账户只留最近 2000 条。
    con.execute("DELETE FROM snapshots WHERE uid=? AND id NOT IN "
                "(SELECT id FROM snapshots WHERE uid=? ORDER BY id DESC LIMIT 2000)",
                (uid, uid))
    con.commit()


def list_snapshots(uid: str, limit: int = 200) -> list[dict]:
    rows = _con().execute(
        "SELECT ts,balance,total FROM snapshots WHERE uid=? ORDER BY id DESC LIMIT ?",
        (uid, max(1, int(limit)))).fetchall()
    return [{"ts": r["ts"], "balance": r["balance"], "total": r["total"]}
            for r in reversed(rows)]


def migrate_json(users_dir, sessions_file, force: bool = False) -> dict:
    """把老的 users/*.json + sessions.json 一次性搬进库（幂等，已存在的账户不覆盖）。"""
    if _meta_get("json_migrated") and not force:
        return {"skip": True}
    n_acc = n_ev = n_ss = 0
    udir = Path(users_dir)
    if udir.is_dir():
        for p in sorted(udir.glob("*.json")):
            try:
                u = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            uid = u.get("uid") or p.stem
            if not uid or has_account(uid):
                continue
            u["uid"] = uid
            put_account(u)
            n_acc += 1
            for item in (u.get("hist") or []):
                if isinstance(item, dict):
                    add_event(uid, item)
                    n_ev += 1
            snap = u.get("snap") or {}
            if snap.get("balance") is not None:
                add_snapshot(uid, snap)
    sf = Path(sessions_file)
    if sf.is_file():
        try:
            m = json.loads(sf.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            m = {}
        con = _con()
        for sid, v in m.items():
            uid = (v or {}).get("uid") or ""
            at = int((v or {}).get("at") or 0)
            if not sid or not uid:
                continue
            con.execute("INSERT OR IGNORE INTO sessions(sid,uid,at) VALUES(?,?,?)",
                        (sid, uid, at))
            n_ss += 1
        con.commit()
    _meta_set("json_migrated", time.strftime("%Y-%m-%dT%H:%M:%S"))
    return {"accounts": n_acc, "events": n_ev, "sessions": n_ss}


def stats() -> dict:
    con = _con()
    out = {}
    for t in ("accounts", "sessions", "events", "snapshots", "devices"):
        r = con.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
        out[t] = int(r["n"] if r else 0)
    try:
        out["db_bytes"] = os.path.getsize(_DB)
    except OSError:
        out["db_bytes"] = 0
    return out
