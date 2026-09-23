#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二批：取正文 -> 分诊 -> 脱敏 -> AI 分析 -> 落库。

默认 dry-run（只落库，不推送）。--push 才生成待推送的行动项。

红队必修项的落地：
  1. 分诊不阻断分析 —— 判据看"正文/主题里有没有行动项"，不看发件人是不是 noreply
  2. "分析失败" 与 "无行动项" 在数据上分开 —— status='error' vs kind='info'
  3. 单封正文字节上限 + 每日模型调用硬上限
  4. 输出侧字符白名单 —— 防止将来有人把 action 拼进 shell/路径
  5. 过期 deadline 单独标记，不当作行动项推
  6. 输出侧**语义**校验（v5）—— org 为空 / 退信类说不清"没送到" 一律打标记录，
     不允许静默通过（见 validate_record）。模型不返回新字段时走降级路径，不整体失败。
"""
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes

sys.path.insert(0, "/root/mail-agent")
from common import DB, CST, load_env, sanitize, BOUNCE_PAT, BOUNCE_FROM
import triage
import prep
import links
from prompt_final import analyze_email, EmptyContentError, ParseError, LLMError

# ---------------- 护栏 ----------------
MAX_CALLS_PER_DAY = 50          # 每日模型调用硬上限（防邮件洪峰烧钱）
MAX_BODY_BYTES = 3_000_000      # 单封正文字节上限，超过就只用主题
BATCH_LIMIT = 20                # 单轮最多处理几封
RECENT_DAYS = 10                # 除了增量，还回头看最近这么多天的邮件
PROMPT_VERSION = "v2"           # v2 = 输出 schema 增加 org/situation/where

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_run (
  id             INTEGER PRIMARY KEY,
  email_id       INTEGER NOT NULL,
  prompt_version TEXT NOT NULL,
  model          TEXT,
  status         TEXT NOT NULL,      -- ok | error
  kind           TEXT,
  action         TEXT,
  deadline_raw   TEXT,
  due_utc        TEXT,
  need_reply     INTEGER,
  reply_ask      TEXT,
  red_flags      TEXT,
  confidence     REAL,
  evidence       TEXT,
  truncated      INTEGER DEFAULT 0,
  raw_json       TEXT,
  error          TEXT,
  in_tokens      INTEGER,
  out_tokens     INTEGER,
  created_at     TEXT NOT NULL,
  UNIQUE(email_id, prompt_version)
);
CREATE TABLE IF NOT EXISTS email_verdict (
  email_id   INTEGER PRIMARY KEY,
  run_id     INTEGER NOT NULL,
  kind       TEXT,
  action     TEXT,
  due_utc    TEXT,
  expired    INTEGER DEFAULT 0,
  need_reply INTEGER,
  reply_ask  TEXT,
  red_flags  TEXT,
  confidence REAL,
  link       TEXT,
  link_host  TEXT,
  link_short INTEGER DEFAULT 0,
  credential TEXT,
  pushed_at  TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyze_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

# v5 加列：老列一个都不改名、不删除（push_actions.py 还在读 v.action）。
# 列名写死，不做任何字符串拼接；ALTER 前先 PRAGMA 查一遍，幂等可重复执行。
NEW_COLUMNS = (("email_verdict", "org", "TEXT"),
               ("email_verdict", "situation", "TEXT"),
               ("email_verdict", "where_hint", "TEXT"),   # LLM 字段名是 where，列名避开 SQL 关键字
               ("email_verdict", "qa_flags", "TEXT"),
               ("analysis_run", "org", "TEXT"),
               ("analysis_run", "situation", "TEXT"),
               ("analysis_run", "where_hint", "TEXT"),
               ("analysis_run", "qa_flags", "TEXT"))

# 输出侧白名单：action 里不允许出现的字符/模式
UNSAFE = re.compile(r"[\n\r`$|;&<>{}]|MEDIA:|\.\./|(^|\s)/|~")

# 登录凭据：测评/笔试系统会在正文里给"通行证/密码"，用户登录时要用。
# 从**原始**正文提取（脱敏后的那份已被替换成 [测评通行证]），存本地、只在给用户看的卡片里展示。
CRED_PAT = re.compile(
    r"(通行证|准考证号|登录密码|考试密码|测评密码|笔试密码|作答密码)"
    r"\s*[:：]\s*([A-Za-z0-9@._-]{4,40})")


def extract_credential(plain_text):
    """从剥掉标签的正文里找登录凭据，返回 '通行证 123456' 这样的字符串（找不到返回空）。"""
    if not plain_text:
        return ""
    hits = CRED_PAT.findall(plain_text)
    if not hits:
        return ""
    seen, out = set(), []
    for label, val in hits:
        key = (label, val)
        if key in seen:
            continue
        seen.add(key)
        out.append("%s %s" % (label, val))
    return " / ".join(out[:3])


def html_plain(raw_html):
    """把 HTML 剥成纯文本（只用来找凭据，不做别的）。"""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw_html or "")
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|td)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"[ \t]{2,}", " ", s)


def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def clean_text(s, limit=80):
    """输出侧白名单清洗（唯一入口）：控制字符/换行 -> 去 shell 元字符与路径特征。

    action 和新加的 org/situation/where 都走这里 —— 新字段不开新口子。
    """
    if not s:
        return ""
    s = sanitize(s, limit)
    s = UNSAFE.sub("", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def clean_action(s):
    return clean_text(s, 80)


# ---------------------------------------------------------------- 输出侧语义校验
# 目的：**不能只靠模型自觉**。org 丢了 = 主语丢了 = 句子会被读成"对方要求我做事"，
# 这是已经真实发生过的生产事故（#5/#7 两封退信），必须在落库侧被发现并记录。
BOUNCE_SIGNAL = re.compile(
    r"未送达|没送到|没有送到|无法送达|送达失败|投递失败|发送失败|被退回|退回|退信|"
    r"地址不存在|收件人不存在|邮箱不存在|不存在|drop(?:ped)?|undeliverable|"
    r"not delivered|delivery fail|bounce", re.I)

QA_ORG_MISSING = "org_missing"                  # action/question 却没有主体
QA_BOUNCE_UNCLEAR = "bounce_situation_unclear"  # 退信类没写清"没送到"
QA_LEGACY = "schema_legacy"                     # 模型没返回新字段（旧格式/降级）
QA_ACTION_OVER = "action_over_limit"
QA_SITUATION_OVER = "situation_over_limit"
QA_WHERE_OVER = "where_over_limit"


def is_bounce_email(from_addr, subject, rule=""):
    """是不是"我自己发出去的邮件没送到"的告警。

    判据用 triage 的规则名（权威），再加 common.py 里那两条久经考验的模式兜底
    —— 两处都命中说明毫无争议；只命中一处（如 mailbox full）也照退信处理。
    """
    if rule == "bounce.delivery_failed":
        return True
    return bool(BOUNCE_PAT.search(subject or "") or BOUNCE_FROM.match(from_addr or ""))


def validate_record(rec, is_bounce=False):
    """对模型的输出做落库前的语义体检，返回问题码列表（空列表 = 通过）。

    调用方必须把非空结果**落库 + 打日志**，不许静默通过。
    这里只判断"有没有问题"，不修正内容、不改 confidence —— 语义由人看。
    """
    qa = []
    kind = rec.get("kind")
    org = (rec.get("org") or "").strip()
    situation = (rec.get("situation") or "").strip()

    # 1) 有行动项的记录必须有主体。取不到公司名必须退到域名/发件方，绝不能留空。
    if kind in ("action", "question") and not org:
        qa.append(QA_ORG_MISSING)

    # 2) 退信类：situation 必须能看出"未送达/退回/地址不存在"
    if is_bounce and not BOUNCE_SIGNAL.search(situation):
        qa.append(QA_BOUNCE_UNCLEAR)

    # 3) 降级：模型没给新字段（旧响应 / 降级 / 老缓存）
    if rec.get("legacy_schema"):
        qa.append(QA_LEGACY)

    # 4) 长度超限（模型没数够字数）
    if rec.get("action_over_limit"):
        qa.append(QA_ACTION_OVER)
    if rec.get("situation_over_limit"):
        qa.append(QA_SITUATION_OVER)
    if rec.get("where_over_limit"):
        qa.append(QA_WHERE_OVER)
    return qa


def parse_deadline(s):
    """模型给的 deadline -> UTC。解析不了就返回 None（宁可没有截止时间）。"""
    if not s:
        return None
    s = str(s).strip()
    for fmt, pad in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if pad:
            dt = dt.replace(hour=23, minute=59)
        return dt.replace(tzinfo=CST).astimezone(timezone.utc)
    return None


def extract_parts(msg):
    """从邮件里取出 text/plain 和 text/html 正文（跳过附件）。"""
    text, html = "", ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if "attachment" in str(part.get("Content-Disposition") or "").lower():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue
        cs = part.get_content_charset() or "utf-8"
        try:
            s = payload.decode(cs, errors="replace")
        except (LookupError, UnicodeDecodeError):
            s = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain":
            text += s + "\n"
        else:
            html += s + "\n"
    return text.strip(), html.strip()


def today_calls(db):
    key = "calls:" + datetime.now(CST).strftime("%Y-%m-%d")
    r = db.execute("SELECT v FROM analyze_state WHERE k=?", (key,)).fetchone()
    return int(r[0]) if r else 0


def bump_calls(db, n=1):
    key = "calls:" + datetime.now(CST).strftime("%Y-%m-%d")
    cur = today_calls(db)
    db.execute("INSERT INTO analyze_state(k,v) VALUES(?,?) "
               "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(cur + n)))
    db.commit()


def main():
    push = "--push" in sys.argv
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    # 已有库补列（幂等）。列名写死，不做任何字符串拼接；只 ADD，不改名不删除。
    cols = [r[1] for r in db.execute("PRAGMA table_info(email_verdict)")]
    if "link" not in cols:
        db.execute("ALTER TABLE email_verdict ADD COLUMN link TEXT")
    if "link_host" not in cols:
        db.execute("ALTER TABLE email_verdict ADD COLUMN link_host TEXT")
    if "link_short" not in cols:
        db.execute("ALTER TABLE email_verdict ADD COLUMN link_short INTEGER DEFAULT 0")
    if "credential" not in cols:
        db.execute("ALTER TABLE email_verdict ADD COLUMN credential TEXT")
    for table, col, typ in NEW_COLUMNS:               # v5：org/situation/where_hint/qa_flags
        have = [r[1] for r in db.execute("PRAGMA table_info(%s)" % table)]
        if col not in have:
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
    db.commit()
    db.execute("PRAGMA busy_timeout=15000")

    # 隐私护栏：身份配置没加载成功就拒绝送模型。
    # 宁可这轮不分析（漏），也不能把姓名/学校/手机号原样发给第三方端点。
    try:
        ident = prep.load_identity()
        ok_ident = bool((ident or {}).get("name"))
    except Exception as e:
        ident, ok_ident = None, False
        print("  身份读取异常: %r" % e)
    if not ok_ident:
        print("！！身份配置未加载（姓名等无法脱敏），本轮拒绝送模型。")
        print("   期望文件：%s" % prep.IDENTITY_FILE)
        db.close()
        return
    print("身份已加载：%s（用于脱敏）" % ident.get("name"))

    since = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")
    rows = db.execute(
        """SELECT e.id, f.name, f.role, e.uid, e.from_addr, e.subject,
                  e.internaldate, e.size_bytes
             FROM email e JOIN folder f ON f.id=e.folder_id
            WHERE f.role IN ('inbox','junk') AND e.fetched=1 AND e.origin<>'stale'
              AND (e.origin='incremental' OR e.internaldate >= ?)
              AND e.id NOT IN (SELECT email_id FROM email_verdict)
            ORDER BY e.internaldate DESC
            LIMIT ?""", (since, BATCH_LIMIT)).fetchall()

    if not rows:
        print("没有待分析的邮件")
        db.close()
        return

    calls = today_calls(db)
    print("待分析 %d 封（今日已调用 %d/%d）\n" % (len(rows), calls, MAX_CALLS_PER_DAY))

    from imapclient import IMAPClient
    cfg = load_env()
    c = IMAPClient(cfg.get("IMAP_HOST", "imap.qq.com"),
                   port=int(cfg.get("IMAP_PORT", "993")), ssl=True, timeout=30)
    c.login(cfg["QQ_EMAIL"], cfg["QQ_AUTH_CODE"])
    c.normalise_times = False

    stats = {"drop": 0, "template": 0, "analyzed": 0, "error": 0, "skipped": 0, "flagged": 0}
    for eid, fname, role, uid, frm, subj, idt, size in rows:
        bucket, rule = triage.classify(frm or "", subj or "")
        is_bounce = is_bounce_email(frm or "", subj or "", rule)

        if bucket == "drop":
            stats["drop"] += 1
            print("  [跳过] %-22s %s" % (sanitize(frm, 22), sanitize(subj, 40)))
            db.execute("INSERT OR REPLACE INTO email_verdict"
                       "(email_id, run_id, kind, updated_at) VALUES(?,0,'skipped',?)",
                       (eid, now_iso()))
            db.commit()
            continue

        if stats["analyzed"] >= MAX_CALLS_PER_DAY - calls:
            print("  [停止] 已达每日调用上限")
            break

        # ---- 取正文（只读）----
        body_text = ""
        truncated = 0
        link_url, link_host, link_short = None, None, 0
        credential = ""
        if size and size > MAX_BODY_BYTES:
            truncated = 1
            print("  [大邮件] %s（%d MB），只用主题" % (sanitize(subj, 30), size // 1048576))
        else:
            try:
                c.select_folder(fname, readonly=True)
                got = c.fetch([uid], ["BODY.PEEK[]"])
                raw = (got.get(uid, {}) or {}).get(b"BODY[]", b"") or b""
                if raw:
                    t, h = extract_parts(message_from_bytes(raw))
                    # 链接和凭据都从**原始**正文里取
                    # （脱敏后的那份里，URL 令牌和通行证都已被替换成标记）
                    link_url, link_host, link_short = links.best_link(t, h, frm or "")
                    credential = extract_credential(html_plain(h) or t)
                    body_text, pstats = prep.prepare_for_llm(
                        h, t, subj or "", frm or "")
                    truncated = 1 if pstats.get("truncated") else 0
            except Exception as e:
                print("  [取信失败] %s: %r" % (sanitize(subj, 30), e))
                stats["skipped"] += 1
                continue

        # ---- 调模型 ----
        try:
            rec = analyze_email(from_addr=frm or "", subject=subj or "",
                                internaldate=idt or "", body=body_text or subj or "")
        except EmptyContentError as e:
            stats["error"] += 1
            db.execute("""INSERT OR REPLACE INTO analysis_run
                (email_id, prompt_version, status, error, truncated, created_at)
                VALUES(?,?,'error',?,?,?)""",
                (eid, PROMPT_VERSION, "空返回: %s" % e, truncated, now_iso()))
            db.commit(); bump_calls(db)
            print("  [分析失败·空返回] %s" % sanitize(subj, 36))
            continue
        except (ParseError, LLMError) as e:
            stats["error"] += 1
            db.execute("""INSERT OR REPLACE INTO analysis_run
                (email_id, prompt_version, status, error, truncated, created_at)
                VALUES(?,?,'error',?,?,?)""",
                (eid, PROMPT_VERSION, str(e)[:300], truncated, now_iso()))
            db.commit(); bump_calls(db)
            print("  [分析失败] %s: %s" % (sanitize(subj, 30), str(e)[:60]))
            continue

        bump_calls(db)
        action = clean_action(rec.get("action") or "")
        # v5 新字段：和 action 走**同一个**白名单清洗函数，不开新口子
        org = clean_text(rec.get("org"), 60)
        situation = clean_text(rec.get("situation"), 80)
        where_hint = clean_text(rec.get("where"), 60)
        due = parse_deadline(rec.get("deadline"))
        expired = 1 if (due and due < datetime.now(timezone.utc)) else 0
        flags = rec.get("red_flags") or []
        # 输出侧语义体检 —— 非空即打标，落库 + 打日志，绝不静默通过
        qa = validate_record(rec, is_bounce=is_bounce)
        qa_str = ",".join(qa)
        if qa:
            stats["flagged"] += 1

        cur = db.execute("""INSERT OR REPLACE INTO analysis_run
            (email_id, prompt_version, model, status, kind, action, deadline_raw,
             due_utc, need_reply, reply_ask, red_flags, confidence, evidence,
             truncated, raw_json, in_tokens, out_tokens, created_at,
             org, situation, where_hint, qa_flags)
            VALUES(?,?,?,'ok',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, PROMPT_VERSION, rec.get("meta", {}).get("model", ""),
             rec.get("kind"), action, rec.get("deadline"), 
             due.isoformat(timespec="seconds") if due else None,
             1 if rec.get("need_reply") else 0, rec.get("reply_ask"),
             ",".join(flags) if isinstance(flags, list) else str(flags),
             rec.get("confidence"), rec.get("evidence"), truncated,
             str(rec.get("raw_json") or "")[:2000],
             (rec.get("meta", {}).get("usage") or {}).get("prompt_tokens"),
             (rec.get("meta", {}).get("usage") or {}).get("completion_tokens"),
             now_iso(), org, situation, where_hint, qa_str))
        db.execute("""INSERT OR REPLACE INTO email_verdict
            (email_id, run_id, kind, action, due_utc, expired, need_reply,
             reply_ask, red_flags, confidence, link, link_host, link_short,
             credential, updated_at, org, situation, where_hint, qa_flags)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, cur.lastrowid, rec.get("kind"), action,
             due.isoformat(timespec="seconds") if due else None, expired,
             1 if rec.get("need_reply") else 0, rec.get("reply_ask"),
             ",".join(flags) if isinstance(flags, list) else "", 
             rec.get("confidence"), link_url, link_host, link_short,
             credential, now_iso(), org, situation, where_hint, qa_str))
        db.commit()

        stats["analyzed"] += 1
        mark = ""
        if expired:
            mark = " 〔已过期〕"
        elif due:
            mark = " 〔截止 %s〕" % due.astimezone(CST).strftime("%m-%d %H:%M")
        print("  [%s]%s %s" % (rec.get("kind"), mark, action))
        print("      主体=%s｜情境=%s｜去哪=%s" % (org or "(空)", situation or "-",
                                                  where_hint or "-"))
        if qa:
            print("      ⚠️ 校验打标 %s：%s" % (qa_str, sanitize(situation or action, 50)))

    c.logout()
    used = today_calls(db)
    db.close()
    print("\n=== 统计 ===")
    print("  分析成功 %d / 失败 %d / 跳过(无关) %d / 取信失败 %d"
          % (stats["analyzed"], stats["error"], stats["drop"], stats["skipped"]))
    print("  校验打标 %d 条（见 email_verdict.qa_flags）" % stats["flagged"])
    print("  今日模型调用累计 %d 次（上限 %d）" % (used, MAX_CALLS_PER_DAY))
    if not push:
        print("\n（dry-run：只落库，未生成推送）")


if __name__ == "__main__":
    main()
