#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""增量读取 + 回填邮件头。

做两件事：
  1. 取新邮件（编号 > 水位线）登记进账本
  2. 给账本里所有"还没取头"的邮件补取邮件头

只取邮件头（不取正文），又快又小。全程只读。
"""
import email
import email.header
import email.utils
import sqlite3
import sys
from datetime import datetime, timezone

from imapclient import IMAPClient

DB = "/root/mail-agent/mail.db"
ENV = "/root/mail-agent/.env"
BATCH = 50  # 每批取多少封（避免单次 FETCH 太大）


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


def dec(raw):
    """解码 MIME 编码的邮件头，任何异常都不能让整封失败。"""
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
        out = []
        for data, charset in parts:
            if isinstance(data, bytes):
                for cs in (charset, "utf-8", "gb18030"):
                    if not cs:
                        continue
                    try:
                        out.append(data.decode(cs, errors="strict"))
                        break
                    except (LookupError, UnicodeDecodeError):
                        continue
                else:
                    out.append(data.decode("utf-8", errors="replace"))
            else:
                out.append(data)
        return "".join(out).strip()
    except Exception:
        return str(raw)


def addr_of(raw):
    """从 From/To 头里取出纯地址。"""
    try:
        _name, addr = email.utils.parseaddr(raw or "")
        return (addr or "").strip().lower()
    except Exception:
        return ""


def domain_of(addr):
    return addr.split("@", 1)[1] if "@" in addr else ""


def parse_header(raw_header):
    m = email.message_from_bytes(raw_header)
    return {
        "message_id": (m.get("Message-ID") or "").strip(),
        "in_reply_to": (m.get("In-Reply-To") or "").strip(),
        "references": (m.get("References") or "").strip()[:1000],
        "subject": dec(m.get("Subject"))[:500],
        "from_addr": addr_of(m.get("From")),
        "to_addr": addr_of(m.get("To")),
    }


def main():
    cfg = load_env(ENV)
    db = sqlite3.connect(DB)
    db.execute("PRAGMA foreign_keys=ON")

    print("连接 %s ..." % cfg.get("IMAP_HOST"))
    c = IMAPClient(cfg.get("IMAP_HOST", "imap.qq.com"),
                   port=int(cfg.get("IMAP_PORT", "993")), ssl=True, timeout=30)
    c.login(cfg["QQ_EMAIL"], cfg["QQ_AUTH_CODE"])
    print("登录成功 ✓")

    max_sent = datetime.strptime("01-Jan-2000", "%d-%b-%Y")
    c.normalise_times = False  # INTERNALDATE 保留时区

    for name, role, uidval, last_uid in db.execute(
            "SELECT name, role, uidvalidity, last_uid FROM folder ORDER BY role").fetchall():

        st = c.folder_status(name, ["MESSAGES", "UIDNEXT", "UIDVALIDITY"])
        cur_uv = int(st[b"UIDVALIDITY"])

        if cur_uv != uidval:
            print("\n!! %s 代际号变了 (%s -> %s)，需要重新基线（本脚本先跳过）"
                  % (name, uidval, cur_uv))
            continue

        c.select_folder(name, readonly=True)
        t = now_iso()

        # --- 1) 取新邮件 ---
        cands = c.search(["UID", "%d:*" % (last_uid + 1)])
        new_uids = sorted(u for u in cands if u > last_uid)  # ★ 过滤 RFC3501 的 * 语义
        if new_uids:
            db.executemany(
                """INSERT OR IGNORE INTO email(folder_id, uid, uidvalidity, origin,
                                               fetched, first_seen_at)
                   SELECT id, ?, ?, 'incremental', 0, ? FROM folder WHERE name=?""",
                [(u, cur_uv, t, name) for u in new_uids])
            db.commit()
            print("\n%s：发现 %d 封新邮件" % (name, len(new_uids)))

        # --- 2) 回填邮件头 ---
        fid = db.execute("SELECT id FROM folder WHERE name=?", (name,)).fetchone()[0]
        todo = [r[0] for r in db.execute(
            """SELECT id FROM email WHERE folder_id=? AND uidvalidity=? AND fetched=0
               ORDER BY uid""", (fid, cur_uv)).fetchall()]

        if todo:
            print("%s：需要补取邮件头 %d 封" % (name, len(todo)))
            done = 0
            for i in range(0, len(todo), BATCH):
                chunk_ids = todo[i:i + BATCH]
                uids = [r[0] for r in db.execute(
                    "SELECT uid FROM email WHERE id IN (%s)"
                    % ",".join("?" * len(chunk_ids)), chunk_ids).fetchall()]

                got = c.fetch(uids, ["UID", "INTERNALDATE", "RFC822.SIZE",
                                     "BODY.PEEK[HEADER]"])
                for uid, d in got.items():
                    raw = d.get(b"BODY[HEADER]") or d.get(b"BODY.PEEK[HEADER]") or b""
                    h = parse_header(raw)
                    idt = d.get(b"INTERNALDATE")
                    idt_iso = idt.astimezone(timezone.utc).isoformat(timespec="seconds") \
                        if isinstance(idt, datetime) else None
                    db.execute(
                        """UPDATE email SET fetched=1, message_id=?, in_reply_to=?, subject=?,
                               from_addr=?, to_addr=?, internaldate=?, size_bytes=?
                           WHERE folder_id=? AND uidvalidity=? AND uid=?""",
                        (h["message_id"], h["in_reply_to"][:500], h["subject"],
                         h["from_addr"], h["to_addr"], idt_iso, d.get(b"RFC822.SIZE"),
                         fid, cur_uv, uid))
                db.commit()
                done += len(got)
                print("   已取 %d/%d" % (done, len(todo)))

        # --- 3) 推进水位线 ---
        if new_uids:
            db.execute("UPDATE folder SET last_uid=?, uidnext_seen=?, messages_seen=?, last_ok_at=?"
                       " WHERE name=?",
                       (max(new_uids), int(st[b"UIDNEXT"]), int(st[b"MESSAGES"]), t, name))
        else:
            db.execute("UPDATE folder SET uidnext_seen=?, messages_seen=?, last_ok_at=?"
                       " WHERE name=?", (int(st[b"UIDNEXT"]), int(st[b"MESSAGES"]), t, name))
        db.commit()

    c.logout()

    print("\n=== 账本现状 ===")
    for name, role, cnt, backfilled in db.execute(
            """SELECT f.name, f.role, COUNT(e.id), SUM(e.fetched)
               FROM folder f LEFT JOIN email e ON e.folder_id=f.id
               GROUP BY f.id ORDER BY f.role"""):
        print("  %-16s %-8s 共 %-5s 封，已取头 %s 封" % (name, role, cnt, backfilled))
    db.close()
    print("\n完成")


if __name__ == "__main__":
    main()
