#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二步：建账本（SQLite）+ 做基线。

基线 = 把邮箱里现有的邮件"登记在册"，但不推送。
这样以后新来的邮件才能被识别成"新的"。
全程只读，不改邮箱任何状态。
"""
import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone

from imapclient import IMAPClient

DB = "/root/mail-agent/mail.db"
ENV = "/root/mail-agent/.env"

# 要监控的文件夹：真实名字已在上一步确认
FOLDERS = [
    ("INBOX", "inbox"),
    ("Sent Messages", "sent"),
    ("Junk", "junk"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS folder (
  id            INTEGER PRIMARY KEY,
  name          TEXT    NOT NULL,           -- 邮箱里的真实名字
  role          TEXT    NOT NULL,           -- inbox | sent | junk
  uidvalidity   INTEGER NOT NULL DEFAULT 0, -- 代际号
  last_uid      INTEGER NOT NULL DEFAULT 0, -- 水位线
  uidnext_seen  INTEGER,
  messages_seen INTEGER,
  baseline_at   TEXT,
  last_ok_at    TEXT,
  UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS email (
  id            INTEGER PRIMARY KEY,
  folder_id     INTEGER NOT NULL REFERENCES folder(id),
  uid           INTEGER NOT NULL,
  uidvalidity   INTEGER NOT NULL,
  message_id    TEXT,
  subject       TEXT,
  from_addr     TEXT,
  to_addr       TEXT,
  internaldate  TEXT,                       -- UTC ISO8601
  size_bytes    INTEGER,
  origin        TEXT    NOT NULL DEFAULT 'incremental',  -- baseline | incremental
  fetched       INTEGER NOT NULL DEFAULT 0,
  notified_at   TEXT,
  first_seen_at TEXT    NOT NULL,
  UNIQUE(folder_id, uidvalidity, uid)       -- 幂等基石
);

CREATE INDEX IF NOT EXISTS ix_email_live ON email(folder_id, uidvalidity, uid);
CREATE INDEX IF NOT EXISTS ix_email_msgid ON email(message_id);

CREATE TABLE IF NOT EXISTS sync_state (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env(path):
    cfg = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def main():
    cfg = load_env(ENV)
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    print("连接 %s ..." % cfg.get("IMAP_HOST"))
    c = IMAPClient(cfg.get("IMAP_HOST", "imap.qq.com"),
                   port=int(cfg.get("IMAP_PORT", "993")), ssl=True, timeout=30)
    c.login(cfg["QQ_EMAIL"], cfg["QQ_AUTH_CODE"])
    print("登录成功 ✓\n")

    # 列出所有文件夹，确认哪些存在、哪些不能选
    listed = {name: flags for flags, _d, name in c.list_folders()}
    noselect = set()
    for name, flags in listed.items():
        for f in flags:
            key = f.decode() if isinstance(f, bytes) else str(f)
            if key.lower() == "\\noselect":
                noselect.add(name)

    t = now_iso()
    for name, role in FOLDERS:
        if name not in listed:
            print("跳过 %-16s （邮箱里不存在）" % name)
            continue
        if name in noselect:
            print("跳过 %-16s （是父节点，不能选）" % name)
            continue

        st = c.folder_status(name, ["MESSAGES", "UIDNEXT", "UIDVALIDITY"])
        uidval = int(st[b"UIDVALIDITY"])
        uidnext = int(st[b"UIDNEXT"])
        msgs = int(st[b"MESSAGES"])

        row = db.execute("SELECT id, uidvalidity, baseline_at FROM folder WHERE name=?", (name,)).fetchone()

        if row and row[1] == uidval and row[2]:
            print("跳过 %-16s （已做过基线，代际号未变）" % name)
            continue

        if row and row[1] != uidval:
            print("!! %-16s 代际号变了 (%s -> %s)，旧记录作废" % (name, row[1], uidval))
            db.execute("UPDATE email SET origin='stale' WHERE folder_id=? AND uidvalidity=?",
                       (row[0], row[1]))

        # 只选只读模式 + 取全部 UID
        c.select_folder(name, readonly=True)
        uids = c.search("ALL")
        print("%-16s 代际号=%-12s 邮件数=%-5s 取到 UID=%-5s 最大 UID=%s"
              % (name, uidval, msgs, len(uids), max(uids) if uids else 0))

        if len(uids) != msgs:
            print("   ⚠ UID 数与邮件数不一致（可能被服务端截断）")

        # 建 folder 记录
        db.execute("""INSERT INTO folder(name, role, uidvalidity, last_uid, uidnext_seen,
                                         messages_seen, baseline_at, last_ok_at)
                      VALUES(?,?,?,?,?,?,?,?)
                      ON CONFLICT(name) DO UPDATE SET
                        role=excluded.role, uidvalidity=excluded.uidvalidity,
                        last_uid=excluded.last_uid, uidnext_seen=excluded.uidnext_seen,
                        messages_seen=excluded.messages_seen,
                        baseline_at=excluded.baseline_at, last_ok_at=excluded.last_ok_at""",
                   (name, role, uidval, max(uids) if uids else 0, uidnext, msgs, t, t))
        fid = db.execute("SELECT id FROM folder WHERE name=?", (name,)).fetchone()[0]

        # 全部登记为 baseline，不推送
        db.executemany(
            """INSERT OR IGNORE INTO email(folder_id, uid, uidvalidity, origin,
                                           fetched, first_seen_at)
               VALUES(?,?,?, 'baseline', 0, ?)""",
            [(fid, u, uidval, t) for u in uids])

    db.execute("INSERT INTO sync_state(k, v) VALUES('baseline_done_at', ?) "
               "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (t,))
    db.commit()
    c.logout()

    print("\n=== 账本统计 ===")
    for name, role, uv, lu, cnt in db.execute(
            """SELECT f.name, f.role, f.uidvalidity, f.last_uid,
                      (SELECT COUNT(*) FROM email e WHERE e.folder_id=f.id AND e.origin='baseline')
               FROM folder f ORDER BY f.role"""):
        print("  %-16s 角色=%-8s 代际号=%-12s 水位线=%-6s 已登记=%s" % (name, role, uv, lu, cnt))
    total = db.execute("SELECT COUNT(*) FROM email").fetchone()[0]
    print("  合计登记 %d 封（全部标为 baseline，不会推送）" % total)
    db.close()
    print("\n完成（全程只读，邮箱状态未变）")


if __name__ == "__main__":
    main()
