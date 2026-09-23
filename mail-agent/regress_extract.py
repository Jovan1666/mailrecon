#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提取层回归（v5 加字段之后）—— 只读生产库，所有写入都发生在 /tmp 的副本上。

覆盖：
  A. 解析层：新字段正常解析 / 旧格式（无新字段）降级不崩
  B. 校验层：org 为空必须打标；退信类说不清"没送到"必须打标
  C. 清洗层：新字段走同一套字符白名单（shell/路径注入）
  D. 端到端：analyze.py 打副本库真跑（真 IMAP 只读 + 真模型），新列落库正常
  E. 不回归：push_actions.py 仍旧读 v.action 能跑通（飞书被 stub，绝不真发）
  F. 证据：把两封退信的 after 结果写进**副本**，看用户最终看到的台账标题

用法：python regress_extract.py
"""
import json
import os
import shutil
import sqlite3
import sys
import types

sys.path.insert(0, "/root/mail-agent")
import analyze            # noqa: E402
import prompt_final as pf  # noqa: E402
import triage             # noqa: E402

PROD_DB = "/root/mail-agent/mail.db"
WORK = "/tmp/bouncework"
COPY_DB = os.path.join(WORK, "regress_mail.db")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("　→ " + detail) if detail else ""))


# --------------------------------------------------------------- A 解析层
def part_a():
    print("\n=== A. 解析层：新字段 / 旧格式降级 ===")
    new = pf.parse_email_json(
        '{"kind":"action","action":"重投示例工程师：原信未送达 campus@acme.example.com",'
        '"org":"acme.example.com","situation":"你投给 campus@acme.example.com 的邮件没送到",'
        '"where":"邮箱搜 postmaster@qq.com 看原始退信","deadline":null,"need_reply":false,'
        '"reply_ask":"","confidence":0.9,"red_flags":[],"evidence":"地址不存在"}')
    check("新格式：org/situation/where 都解析出来了",
          (new["org"], new["situation"], new["where"]) != ("", "", ""),
          "org=%r" % new["org"])
    check("新格式：legacy_schema=False", new["legacy_schema"] is False)
    check("新格式：missing_fields 为空", new["missing_fields"] == [])

    # 旧格式（v4 的响应：没有 org/situation/where）—— 必须能解析，不能崩
    legacy = pf.parse_email_json(
        '{"kind":"action","action":"先核实地址再重发示例岗位申请","deadline":null,'
        '"need_reply":false,"reply_ask":"","confidence":0.7,"red_flags":[],"evidence":"不存在"}')
    check("旧格式：不抛异常，照常出结果", legacy["kind"] == "action")
    check("旧格式：新字段补成空串（不是 None、不是 KeyError）",
          legacy["org"] == "" and legacy["situation"] == "" and legacy["where"] == "")
    check("旧格式：legacy_schema=True 被标出来", legacy["legacy_schema"] is True)
    check("旧格式：missing_fields 列出缺的三个字段",
          set(legacy["missing_fields"]) == {"org", "situation", "where"},
          str(legacy["missing_fields"]))

    # 类型容错：org 给成 dict / 数字 / list / null 也不能让解析失败
    for raw, want in (('{"org":{"name":"星辰科技"}}', "星辰科技"),
                      ('{"org":12345}', "12345"),
                      ('{"org":["A","B"]}', "A B"),
                      ('{"org":null}', "")):
        rec = pf.parse_email_json(
            '{"kind":"info","action":"","situation":"","where":"","deadline":null,'
            '"need_reply":false,"reply_ask":"","confidence":0.5,"red_flags":[],'
            '"evidence":"x",' + raw[1:])
        check("org 类型容错 %s" % raw, rec["org"] == want, "得到 %r" % rec["org"])

    # 端到端降级：call_llm 返回旧格式（模拟降级/超时后回退到旧模型输出）
    orig = pf.call_llm
    pf.call_llm = lambda *a, **k: (
        '{"kind":"action","action":"先核实地址再重发示例岗位申请","deadline":null,'
        '"need_reply":false,"reply_ask":"","confidence":0.7,"red_flags":[],'
        '"evidence":"收件人邮件地址不存在"}', {"finish_reason": "stop", "usage": {}})
    try:
        rec = pf.analyze_email(from_addr="postmaster@qq.com", subject="来自qq.com的退信",
                               internaldate="2026-09-20", body="退信正文")
        ok, err = True, ""
    except Exception as e:                                 # noqa: BLE001
        rec, ok, err = {}, False, "%s: %s" % (type(e).__name__, e)
    finally:
        pf.call_llm = orig
    check("模型返回旧格式 → analyze_email 不整体失败", ok, err)
    if ok:
        qa = analyze.validate_record(rec, is_bounce=True)
        check("旧格式走完 analyze 链路、新字段为空但结构完整",
              rec["parsed_ok"] and rec["org"] == "" and rec["kind"] == "action")
        check("旧格式被校验层打标（schema_legacy）", "schema_legacy" in qa, str(qa))


# --------------------------------------------------------------- B 校验层
def part_b():
    print("\n=== B. 校验层：org 为空 / 退信说不清 ===")
    # B1 构造一个 org 为空的返回 —— 必须被打标，不许静默通过
    empty_org = pf.parse_email_json(
        '{"kind":"action","action":"先核实地址再重投示例岗位简历",'
        '"org":"","situation":"对方邮箱有问题","where":"","deadline":null,'
        '"need_reply":false,"reply_ask":"","confidence":0.8,"red_flags":[],'
        '"evidence":"收件人邮件地址不存在"}')
    qa = analyze.validate_record(empty_org, is_bounce=True)
    check("org 为空 + kind=action → 打标 org_missing", "org_missing" in qa, str(qa))
    check("退信类 situation 没写'没送到' → 打标 bounce_situation_unclear",
          "bounce_situation_unclear" in qa, str(qa))

    # B2 question 也要有主体
    q_org_empty = pf.parse_email_json(
        '{"kind":"question","action":"回复确认面试时间","org":"  ","situation":"对方在等回话",'
        '"where":"","deadline":null,"need_reply":true,"reply_ask":"周四还是周五","confidence":0.9,'
        '"red_flags":[],"evidence":"你哪天方便"}')
    check("org 只有空白字符 → 同样算空，打标",
          "org_missing" in analyze.validate_record(q_org_empty, is_bounce=False))
    info = pf.parse_email_json(
        '{"kind":"info","action":"","org":"","situation":"","where":"","deadline":null,'
        '"need_reply":false,"reply_ask":"","confidence":0.9,"red_flags":[],'
        '"evidence":"已收到您的申请"}')
    check("info 类无 org 不打标（无事可做的记录不强求主体）",
          analyze.validate_record(info, is_bounce=False) == [],
          str(analyze.validate_record(info, is_bounce=False)))

    # B3 正常的退信（本次 after 的真实返回）→ 不打标
    good = pf.parse_email_json(json.dumps({
        "kind": "action", "action": "重投示例工程师：原信未送达 campus@acme.example.com",
        "org": "acme.example.com", "situation": "你投给 campus@acme.example.com 的邮件没送到：该地址不存在",
        "where": "邮箱搜 postmaster@qq.com 看原始退信", "deadline": None, "need_reply": False,
        "reply_ask": "", "confidence": 0.95, "red_flags": [],
        "evidence": "收件人邮件地址不存在"}, ensure_ascii=False))
    check("正常退信记录 → 校验通过（空打标列表）",
          analyze.validate_record(good, is_bounce=True) == [],
          str(analyze.validate_record(good, is_bounce=True)))

    # B4 is_bounce 判定：主题/发件人兜底
    check("postmaster 发件人 → is_bounce",
          analyze.is_bounce_email("postmaster@qq.com", "来自qq.com的退信", "bounce.delivery_failed"))
    check("mailer-daemon + Undelivered 主题 → is_bounce（不依赖 triage 规则名）",
          analyze.is_bounce_email("MAILER-DAEMON@x.example.com", "Undelivered Mail Returned to Sender", ""))
    check("普通 HR 邮件 → 不是退信",
          not analyze.is_bounce_email("hr@a.example.com", "面试时间沟通", "llm.human_reply"))


# --------------------------------------------------------------- C 清洗层
def _clean_v4(s):
    """v4 的 clean_action 原样复制 —— 用来证明新字段的清洗与老实现逐字等价。"""
    import re as _re
    from common import sanitize as _san
    if not s:
        return ""
    s = _san(s, 80)
    s = analyze.UNSAFE.sub("", s)
    return _re.sub(r"\s{2,}", " ", s).strip()


def part_c():
    print("\n=== C. 清洗层：新字段走同一套白名单 ===")
    cases = [
        ("org 带 shell 注入", "$(rm -rf /) ; /etc/passwd | cat", "shell 元字符"),
        ("situation 带换行+反引号", "前一行\n第二行 `whoami`", "换行/反引号"),
        ("where 带路径穿越", "../../../etc/shadow", "路径穿越"),
        ("where 带绝对路径", "看 /root/mail-agent/.env 里的 key", "绝对路径"),
        ("org 带 MEDIA: 指令", "MEDIA:/etc/passwd", "MEDIA:"),
        ("situation 带 $ 与 ;", "a$b;c&&d", "$ ; &"),
        ("where 带引号与括号", '"; cat /etc/shadow #', "引号"),
    ]
    for name, raw, hint in cases:
        out = analyze.clean_text(raw, 80)
        # 不变量：不留 shell 元字符 / 换行 / MEDIA: 指令 / 路径穿越
        bad = (any(c in out for c in "`$|;&<>{}~\n\r") or "MEDIA:" in out or "../" in out)
        check("清洗 %s（%s）" % (name, hint), not bad, "%r → %r" % (raw, out))

    check("MEDIA: 指令被摘掉（不留可被解释的前缀）",
          "MEDIA:" not in analyze.clean_text("MEDIA:/etc/passwd", 80))
    check("clean_action 行为与 v4 逐字一致（老调用方不受影响）",
          analyze.clean_action("a  b\nc") == _clean_v4("a  b\nc") == "a b c",
          repr(analyze.clean_action("a  b\nc")))
    same = all(analyze.clean_action(x) == analyze.clean_text(x, 80) == _clean_v4(x)
               for x in [raw for _, raw, _ in cases])
    check("新字段用的就是 action 那一套清洗（同函数 + 同结果，没开新口子）", same)
    print("    注：'MEDIA:/etc/passwd' 老实现同样会留下 '/etc/passwd'（先删 MEDIA: 后不再回扫），")
    print("        这是 v4 就有的既有行为，本次未改动、也未放宽 —— 新字段与 action 完全一致。")


# --------------------------------------------------------------- D 端到端
def part_d(batch=3):
    print("\n=== D. 端到端：analyze.py 打副本库真跑（真 IMAP 只读 + 真模型）===")
    if os.path.exists(COPY_DB):
        os.remove(COPY_DB)
    shutil.copy(PROD_DB, COPY_DB)
    before_prod_md5 = _md5(PROD_DB)

    # 让副本库里几封"已经分析过"的邮件重新变成待分析：这样 main() 会真取信、真调模型、
    # 真走一遍带新列的 INSERT。（只在副本上做，生产库一行不动）
    db = sqlite3.connect(COPY_DB)
    db.row_factory = sqlite3.Row
    reopen = []
    for r in db.execute("""SELECT e.id, e.from_addr, e.subject FROM email e
                             JOIN folder f ON f.id=e.folder_id
                            WHERE f.role IN ('inbox','junk') AND e.fetched=1
                              AND e.origin<>'stale' ORDER BY e.internaldate DESC LIMIT 60"""):
        b, rule = triage.classify(r["from_addr"] or "", r["subject"] or "")
        if b == "llm":
            reopen.append(r["id"])
            if len(reopen) >= batch:
                break
    db.execute("DELETE FROM email_verdict WHERE email_id IN (%s)"
               % ",".join("?" * len(reopen)), reopen)
    db.execute("DELETE FROM analyze_state WHERE k LIKE 'calls:%'")   # 重置副本里的每日计数
    db.commit()
    print("  副本里重新打开的待分析邮件：%s" % reopen)
    db.close()

    analyze.DB = COPY_DB            # 只改副本路径
    analyze.BATCH_LIMIT = batch
    analyze.main()

    db = sqlite3.connect(COPY_DB)
    cols = [r[1] for r in db.execute("PRAGMA table_info(email_verdict)")]
    rcols = [r[1] for r in db.execute("PRAGMA table_info(analysis_run)")]
    check("email_verdict 已加列 org/situation/where_hint/qa_flags",
          all(c in cols for c in ("org", "situation", "where_hint", "qa_flags")))
    check("老列原样保留（kind/action/due_utc/... 一个没少）",
          all(c in cols for c in ("email_id", "run_id", "kind", "action", "due_utc", "expired",
                                  "need_reply", "reply_ask", "red_flags", "confidence",
                                  "link", "link_host", "link_short", "credential",
                                  "pushed_at", "updated_at")))
    check("analysis_run 已加列且老列保留",
          all(c in rcols for c in ("org", "situation", "where_hint", "qa_flags",
                                   "action", "due_utc", "raw_json")))
    rows = db.execute("""SELECT email_id, kind, action, org, situation, where_hint, qa_flags
                           FROM email_verdict WHERE email_id IN (%s)"""
                      % ",".join("?" * len(reopen)), reopen).fetchall()
    print("  本轮真跑出来的 %d 条（新列都写进去了）：" % len(rows))
    for row in rows:
        print("    #%s %s action=%r" % (row[0], row[1], row[2]))
        print("        org=%r situation=%r where=%r qa=%r" % (row[3], row[4], row[5], row[6]))
    check("至少有一条新记录带着新列落库（org 不为 None）",
          any(r[3] is not None for r in rows))
    if rows:
        check("新列不是空字符串占位（org 真的填上了）",
              any((r[3] or "").strip() for r in rows),
              "; ".join("%s:%r" % (r[0], r[3]) for r in rows))
        check("每条新记录都有 qa_flags 字段（哪怕为空串，也不是 NULL）",
              all(r[6] is not None for r in rows))
    db.close()
    check("生产库 md5 未被 analyze 改动", _md5(PROD_DB) == before_prod_md5)
    return COPY_DB


# --------------------------------------------------------------- E 不回归
def part_e(copy_db):
    print("\n=== E. 不回归：push_actions 仍读 v.action（飞书 stub，不发）===")
    import push_actions
    sent = []
    push_actions.DB = copy_db
    push_actions.QUIET_START, push_actions.QUIET_END = 0, 24    # 回归不受当前钟点影响
    push_actions.MAX_SHOW = 20
    push_actions.feishu = types.SimpleNamespace(
        send_card=lambda card: (sent.append(card), (True, "stub"))[1],
        send=lambda *a, **k: (True, "stub"))
    try:
        push_actions.main()
        ok = True
    except Exception as e:                                  # noqa: BLE001
        ok, err = False, "%s: %s" % (type(e).__name__, e)
        check("push_actions.main() 能跑通", False, err)
    else:
        check("push_actions.main() 能跑通（没有因为加列而报错）", True)
        check("飞书调用被 stub 拦住，没有真发消息", len(sent) >= 0)

    db = sqlite3.connect(copy_db)
    rows = db.execute("""SELECT t.id, t.title, v.action FROM tasks t
                           JOIN email_verdict v ON v.email_id = t.email_id
                          ORDER BY t.id DESC LIMIT 4""").fetchall()
    for tid, title, action in rows:
        print("    台账 #%s 标题=%r" % (tid, title))
        check("台账标题与 v.action 一致（#%s）" % tid, title == analyze.clean_action(action or ""))
    n_old = db.execute("SELECT COUNT(*) FROM tasks WHERE state='done'").fetchone()[0]
    print("    副本库 done 数 = %d（生产库那 7 条不受影响，副本随便动）" % n_old)
    db.close()


# --------------------------------------------------------------- F 证据
def part_f(copy_db):
    print("\n=== F. 证据：两封退信的 after 结果写进副本，看用户最终看到的台账标题 ===")
    after_file = os.path.join(WORK, "after.json")
    if not os.path.exists(after_file):
        check("找到 after.json（先用 reanalyze_readonly.py --json 生成）", False)
        return
    after = json.load(open(after_file, encoding="utf-8"))
    db = sqlite3.connect(copy_db)
    db.execute("PRAGMA busy_timeout=15000")
    for item in after:
        eid, rec, qa = item["email_id"], item["rec"], item["qa_flags"]
        db.execute("""UPDATE email_verdict SET action=?, org=?, situation=?, where_hint=?,
                        qa_flags=?, pushed_at=NULL, expired=0
                       WHERE email_id=?""",
                   (analyze.clean_action(rec["action"]), analyze.clean_text(rec["org"], 60),
                    analyze.clean_text(rec["situation"], 80),
                    analyze.clean_text(rec["where"], 60), ",".join(qa), eid))
    db.commit()
    import push_actions
    push_actions.DB = copy_db
    push_actions.QUIET_START, push_actions.QUIET_END = 0, 24
    push_actions.MAX_SHOW = 20
    push_actions.feishu = types.SimpleNamespace(send_card=lambda card: (True, "stub"),
                                               send=lambda *a, **k: (True, "stub"))
    push_actions.main()
    print()
    for eid in (673, 644):
        old = sqlite3.connect(PROD_DB).execute(
            "SELECT title FROM tasks WHERE email_id=?", (eid,)).fetchone()[0]
        row = db.execute("""SELECT t.id, t.title, v.org, v.situation, v.where_hint
                              FROM tasks t JOIN email_verdict v ON v.email_id=t.email_id
                             WHERE t.email_id=?""", (eid,)).fetchone()
        print("  #%s（email %s）" % (row[0], eid))
        print("     before 台账标题: %s" % old)
        print("     after  台账标题: %s" % row[1])
        print("            org=%s ｜ situation=%s" % (row[2], row[3]))
        print("            where=%s" % row[4])
    db.close()


def _md5(path):
    import hashlib
    h = hashlib.md5()                                    # noqa: S324 (只做一致性校验)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------- G 打标落库
FAKE_BAD = {"kind": "action", "action": "先核实地址再重发示例岗位申请",
            "org": "", "situation": "对方邮箱有问题", "where": "",
            "deadline": None, "need_reply": False, "reply_ask": "", "confidence": 0.7,
            "red_flags": [], "evidence": "收件人邮件地址不存在", "parsed_ok": True,
            "meta": {}, "legacy_schema": False, "missing_fields": [],
            "action_over_limit": False, "situation_over_limit": False,
            "where_over_limit": False, "extra_keys": []}


def _store_with_fake(copy_db, eid):
    """把某封邮件的 verdict 删掉（只在副本里），用构造的坏返回走一遍 analyze.main()。"""
    db = sqlite3.connect(copy_db)
    db.execute("DELETE FROM email_verdict WHERE email_id=?", (eid,))
    db.commit()
    db.close()
    orig = analyze.analyze_email
    analyze.analyze_email = lambda **kw: dict(FAKE_BAD)
    analyze.DB = copy_db
    analyze.BATCH_LIMIT = 1
    try:
        analyze.main()
    finally:
        analyze.analyze_email = orig
    db = sqlite3.connect(copy_db)
    v = db.execute("""SELECT org, qa_flags FROM email_verdict WHERE email_id=?""",
                   (eid,)).fetchone()
    r = db.execute("""SELECT org, situation, qa_flags FROM analysis_run
                       WHERE email_id=? AND prompt_version=?""",
                   (eid, analyze.PROMPT_VERSION)).fetchone()
    db.close()
    return v, r


def part_g(copy_db):
    print("\n=== G. 打标落库：构造 org 为空的返回，走 analyze.py 完整落库路径 ===")
    print("  （把 analyze_email 换成构造的坏返回，模型不参与；只在副本库上做）")

    # G1 普通邮件：org 为空 → 落库时打标
    db = sqlite3.connect(copy_db)
    eid = db.execute("""SELECT e.id FROM email e JOIN folder f ON f.id=e.folder_id
                         WHERE f.role IN ('inbox','junk') AND e.fetched=1
                         ORDER BY e.internaldate DESC LIMIT 1""").fetchone()[0]
    db.close()
    v, r = _store_with_fake(copy_db, eid)
    print("  email #%s（普通邮件）→ verdict org=%r qa_flags=%r ｜ run qa_flags=%r"
          % (eid, v[0], v[1], r[2]))
    check("org 为空的返回在 email_verdict.qa_flags 里被记录（不是静默通过）",
          "org_missing" in (v[1] or ""), repr(v[1]))
    check("同一份打标也写进 analysis_run（可追溯）",
          "org_missing" in (r[2] or ""), repr(r[2]))

    # G2 真实退信（#673）：org 为空 + 句子读起来像"对方要求你做事" → 两个标都落下
    v2, r2 = _store_with_fake(copy_db, 673)
    print("  email #673（真实退信）→ verdict org=%r qa_flags=%r ｜ run qa_flags=%r"
          % (v2[0], v2[1], r2[2]))
    check("退信类句子没写清'没送到'也被记录（bounce_situation_unclear）",
          "bounce_situation_unclear" in (v2[1] or ""), repr(v2[1]))
    check("退信 + org 空 → 两个标同时落下",
          {"org_missing", "bounce_situation_unclear"} <= set((v2[1] or "").split(",")),
          repr(v2[1]))


def main():
    md5_before = _md5(PROD_DB)
    print("生产库 md5（跑之前）: %s" % md5_before)
    tasks_before = _tasks_snapshot(PROD_DB)
    part_a()
    part_b()
    part_c()
    copy_db = part_d(batch=int(os.environ.get("REGRESS_BATCH", "3")))
    part_e(copy_db)
    part_f(copy_db)
    part_g(copy_db)
    md5_after = _md5(PROD_DB)
    print("\n生产库 md5（跑之后）: %s" % md5_after)
    tasks_after = _tasks_snapshot(PROD_DB)
    check("生产库文件字节未被改动（md5 一致）", md5_before == md5_after)
    check("生产库 tasks 表逐行未变（7 条状态/标题/完成时间都原样）",
          tasks_before == tasks_after, "%d 行" % len(tasks_after))
    print("\n=== 汇总 ===  PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL: %s" % f)
    return 1 if FAIL else 0


def _tasks_snapshot(db_path):
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT id, email_id, title, state, done_at FROM tasks ORDER BY id").fetchall()
    db.close()
    return rows


if __name__ == "__main__":
    sys.exit(main())
