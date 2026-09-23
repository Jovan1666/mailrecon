#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读重新分析：把指定邮件用当前 prompt 重跑一遍，结果只打终端 —— **绝不写库**。

为什么要它：analyze.py 的取件窗口（RECENT_DAYS / BATCH_LIMIT）和
`e.id NOT IN (SELECT email_id FROM email_verdict)` 会让"已经分析过的邮件"
永远跑不到，而复盘/回归恰恰需要把这些邮件再跑一遍。
本脚本复用 analyze.py 完全相同的取信 + 预处理链路（IMAP 只读、
prep.prepare_for_llm 脱敏、triage 分诊），只把"落库"换成了"打到终端"。

安全：
  * IMAP 一律 select_folder(readonly=True) + BODY.PEEK[]，不改任何邮件状态
  * 不执行任何 INSERT/UPDATE/DELETE（只做 SELECT）
  * 身份没加载 → 拒绝调模型（和 analyze.py 同一条护栏）

用法：
    python reanalyze_readonly.py 673 644
    python reanalyze_readonly.py --prompt /tmp/bouncework/prompt_old.py 673   # 用旧 prompt 对照
    python reanalyze_readonly.py --json 673 > out.json
"""
import argparse
import importlib.util
import json
import sqlite3
import sys
from email import message_from_bytes

sys.path.insert(0, "/root/mail-agent")
from common import DB, load_env                              # noqa: E402
import analyze                                               # noqa: E402  (只用工具函数)
import prep                                                  # noqa: E402
import triage                                                # noqa: E402
import links                                                 # noqa: E402


def load_prompt(path):
    """按路径加载 prompt 模块（用于新旧 prompt 对照）。默认用生产 prompt_final。"""
    if not path:
        import prompt_final
        return prompt_final
    spec = importlib.util.spec_from_file_location("prompt_dyn", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fetch_raw(db, imap, eid):
    """按 email_id 从 IMAP 只读取原始邮件（readonly + BODY.PEEK）。"""
    row = db.execute(
        """SELECT f.name, e.uid FROM email e JOIN folder f ON f.id=e.folder_id
            WHERE e.id=?""", (eid,)).fetchone()
    if not row:
        return None
    fname, uid = row
    imap.select_folder(fname, readonly=True)
    got = imap.fetch([uid], ["BODY.PEEK[]"])
    return (got.get(uid, {}) or {}).get(b"BODY[]", b"") or b""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+", type=int)
    ap.add_argument("--prompt", help="prompt 模块路径（默认生产 prompt_final.py）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    # ---- 护栏 1：身份必须已加载（否则拒绝送模型）----
    ident = prep.load_identity() or {}
    if not ident.get("name"):
        print("！！身份配置未加载（姓名等无法脱敏），拒绝调模型。")
        print("   期望文件：%s" % prep.IDENTITY_FILE)
        return 2

    pf = load_prompt(args.prompt)

    db = sqlite3.connect(DB)          # 只读用途：本脚本全程只 SELECT
    cfg = load_env()
    from imapclient import IMAPClient
    imap = IMAPClient(cfg.get("IMAP_HOST", "imap.qq.com"),
                      port=int(cfg.get("IMAP_PORT", "993")), ssl=True, timeout=30)
    imap.login(cfg["QQ_EMAIL"], cfg["QQ_AUTH_CODE"])
    imap.normalise_times = False

    out = []
    for eid in args.ids:
        row = db.execute(
            """SELECT e.from_addr, e.subject, e.internaldate
                 FROM email e WHERE e.id=?""", (eid,)).fetchone()
        if not row:
            print("找不到 email_id=%s" % eid)
            continue
        frm, subj, idt = row
        raw = fetch_raw(db, imap, eid)
        t, h = analyze.extract_parts(message_from_bytes(raw))
        link_url, link_host, link_short = links.best_link(t, h, frm or "")
        body, pstats = prep.prepare_for_llm(h, t, subj or "", frm or "")
        bucket, rule = triage.classify(frm or "", subj or "")

        try:
            rec = pf.analyze_email(from_addr=frm or "", subject=subj or "",
                                   internaldate=idt or "", body=body or subj or "")
            err = None
        except Exception as e:                             # noqa: BLE001
            rec, err = {}, "%s: %s" % (type(e).__name__, e)

        is_bounce = analyze.is_bounce_email(frm or "", subj or "", rule)
        flags = analyze.validate_record(rec, is_bounce=is_bounce) if rec else []

        item = {
            "email_id": eid, "from": frm, "subject": subj, "rule": rule,
            "is_bounce": is_bounce, "usable": pstats.get("usable"),
            "link": link_url,
            "rec": {k: rec.get(k) for k in
                    ("kind", "action", "org", "situation", "where", "deadline",
                     "need_reply", "reply_ask", "confidence", "red_flags", "evidence")},
            "action_len": len(rec.get("action") or ""),
            "over_limit": {k: rec.get(k + "_over_limit") for k in FIELDS_LEN
                           if (k + "_over_limit") in rec},
            "missing_fields": rec.get("missing_fields"),
            "qa_flags": flags, "error": err,
            "body_sent": body,
        }
        out.append(item)

        if not args.json:
            print("=" * 78)
            print("email #%s  %s  <%s>" % (eid, subj, frm))
            print("  triage: %s / %s   is_bounce=%s" % (bucket, rule, is_bounce))
            print("  送模型正文 %d 字" % len(body or ""))
            if err:
                print("  ★调用失败: %s" % err)
                continue
            for k in ("kind", "action", "org", "situation", "where"):
                v = rec.get(k)
                n = len(v) if isinstance(v, str) else 0
                print("  %-10s %s%s" % (k + ":", v, ("   (%d 字)" % n) if v else ""))
            print("  %-10s %s" % ("deadline:", rec.get("deadline")))
            print("  %-10s %s" % ("confidence:", rec.get("confidence")))
            print("  %-10s %s" % ("evidence:", rec.get("evidence")))
            if rec.get("missing_fields"):
                print("  %-10s %s" % ("缺失字段:", rec["missing_fields"]))
            print("  %-10s %s" % ("校验:", flags or "[无问题]"))
    imap.logout()
    db.close()

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


# 长度上限字段（analyze 的 parse 会给出 xxx_over_limit 标记）
FIELDS_LEN = ("action", "situation", "where", "reply_ask", "evidence")

if __name__ == "__main__":
    sys.exit(main())
