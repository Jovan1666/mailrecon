#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给已分析的 verdict 补链接（不调用模型，只读邮箱）。

场景：analyze.py 之前没做链接提取，已经判过的邮件缺 link。
本脚本只重新取一次正文、抽链接、回填，不重复调用模型。
"""
import sqlite3
import sys
from email import message_from_bytes

sys.path.insert(0, "/root/mail-agent")
from common import DB, load_env, sanitize
import links
from analyze import extract_parts

MAX_BODY_BYTES = 3_000_000


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT v.email_id, e.uid, e.size_bytes, e.from_addr, e.subject, f.name AS fname
             FROM email_verdict v
             JOIN email e ON e.id = v.email_id
             JOIN folder f ON f.id = e.folder_id
            WHERE v.link IS NULL AND v.kind IN ('action','question')
            ORDER BY e.internaldate DESC""").fetchall()

    if not rows:
        print("没有需要补链接的邮件")
        db.close()
        return
    print("需要补链接：%d 封" % len(rows))

    from imapclient import IMAPClient
    cfg = load_env()
    c = IMAPClient(cfg.get("IMAP_HOST", "imap.qq.com"),
                   port=int(cfg.get("IMAP_PORT", "993")), ssl=True, timeout=30)
    c.login(cfg["QQ_EMAIL"], cfg["QQ_AUTH_CODE"])

    fixed = 0
    for r in rows:
        if r["size_bytes"] and r["size_bytes"] > MAX_BODY_BYTES:
            print("  [太大跳过] %s" % sanitize(r["subject"], 36))
            continue
        try:
            c.select_folder(r["fname"], readonly=True)
            got = c.fetch([r["uid"]], ["BODY.PEEK[]"])
            raw = (got.get(r["uid"], {}) or {}).get(b"BODY[]", b"") or b""
            if not raw:
                print("  [取不到] %s" % sanitize(r["subject"], 36))
                continue
            t, h = extract_parts(message_from_bytes(raw))
            url, host, short = links.best_link(t, h, r["from_addr"] or "")
            db.execute("UPDATE email_verdict SET link=?, link_host=?, link_short=? "
                       "WHERE email_id=?", (url, host, 1 if short else 0, r["email_id"]))
            db.commit()
            if url:
                fixed += 1
                print("  ✓ %-34s -> %s" % (sanitize(r["subject"], 34), sanitize(host, 30)))
            else:
                print("  – %-34s （没有可用的链接）" % sanitize(r["subject"], 34))
        except Exception as e:
            print("  [异常] %s: %r" % (sanitize(r["subject"], 30), e))

    c.logout()
    db.close()
    print("\n补上链接 %d 条" % fixed)


if __name__ == "__main__":
    main()
