#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 AI 判出的行动项推给用户（飞书卡片），同时写入台账。

设计要点：
  - 卡片呈现：一条任务 = 一行加粗标题 + 一行元信息 + 一个按钮。不再堆 URL 文本。
  - 台账：每件事都进 tasks 表，有状态（todo / done / expired / dismissed），
    以后可以随时看全貌、也可以标记完成。
  - 已过期的 deadline 不当作行动项推，单独提示。
  - 短链不给按钮（红队提醒：短链是"信任转移"最容易被利用的形态）。
  - 输出侧字符白名单：action 里不允许出现 shell 元字符/换行/MEDIA:（防将来有人拼接）。
"""
import re
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/root/mail-agent")
from common import (DB, CST, ensure_task_cols, sanitize,  # noqa: E402
                    task_lines)
import feishu

QUIET_START, QUIET_END = 8, 20
MAX_SHOW = 8
UNSAFE = re.compile(r"[\n\r`$|;&<>{}]|MEDIA:|\.\./|(^|\s)/|~")

TASKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id         INTEGER PRIMARY KEY,
  email_id   INTEGER UNIQUE,
  title      TEXT NOT NULL,
  due_utc    TEXT,
  link       TEXT,
  link_host  TEXT,
  link_short INTEGER DEFAULT 0,
  credential TEXT,
  state      TEXT NOT NULL DEFAULT 'todo',   -- todo | done | expired | dismissed
  created_at TEXT NOT NULL,
  done_at    TEXT,
  done_source TEXT,
  note       TEXT,
  org        TEXT,                            -- 主体（公司名/域名）
  situation  TEXT,                            -- ≤44 字，发生了什么
  where_hint TEXT                             -- ≤40 字，去哪做
);
"""


def clean(s):
    return re.sub(r"\s{2,}", " ", UNSAFE.sub("", sanitize(s, 80))).strip()


def fmt_due(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone(CST).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def btn_label(action):
    """按钮文字：从动作里挑一个动词，让用户一眼知道点它干嘛。"""
    a = action or ""
    for kw, label in (("测评", "去做测评"), ("笔试", "去做笔试"), ("测试", "去测试"),
                      ("面试", "去确认面试"), ("简历", "去完善简历"),
                      ("验证", "去验证"), ("确认", "去确认"), ("重发", "去重发")):
        if kw in a:
            return label
    return "去处理"


def done_action(tid):
    """「✅ 完成」按钮。

    飞书卡片按钮：带 url 是跳转，带 value 才是回调 —— 这里只做回调，
    点击后由 ~/.hermes/plugins/task-cmd-bridge 翻译成「完成 <编号>」，
    走和文字指令完全相同的一条链路（解析 → 落库 → 刷新看板 → 撤销按钮）。
    """
    return {"tag": "action", "actions": [{
        "tag": "button", "type": "default",
        "text": {"tag": "plain_text", "content": "✅ 完成"},
        "value": {"k": "done", "id": tid}}]}


def main():
    db = sqlite3.connect(DB)
    db.executescript(TASKS_SCHEMA)
    # 已有库补列（幂等，列名写死）
    tcols = [r[1] for r in db.execute("PRAGMA table_info(tasks)")]
    if "credential" not in tcols:
        db.execute("ALTER TABLE tasks ADD COLUMN credential TEXT")
    db.commit()
    ensure_task_cols(db)          # org / situation / where_hint（board 和 taskboard 也要读）
    db.row_factory = sqlite3.Row
    now = datetime.now(CST)
    stamp = now.isoformat(timespec="seconds")

    # 先把过期的标出来
    db.execute("UPDATE tasks SET state='expired' WHERE state='todo' "
               "AND due_utc IS NOT NULL AND due_utc < ?", (now.astimezone(timezone.utc).isoformat(),))
    db.commit()

    # email_verdict 的 org/situation/where_hint 由 analyze.py 幂等 ALTER 加上，
    # 它可能还没跑到（列还没建）。缺列就按空值取 —— 推送卡不能因为"分析脚本还没跑"崩掉。
    vcols = [r[1] for r in db.execute("PRAGMA table_info(email_verdict)")]
    v_org = "v.org" if "org" in vcols else "''"
    v_sit = "v.situation" if "situation" in vcols else "''"
    v_whr = "v.where_hint" if "where_hint" in vcols else "''"

    rows = db.execute(
        """SELECT v.email_id, v.kind, v.action, v.due_utc, v.expired, v.need_reply,
                  v.reply_ask, v.red_flags, v.link, v.link_host, v.link_short,
                  v.credential, e.subject, e.from_addr,
                  %s AS org, %s AS situation, %s AS where_hint
             FROM email_verdict v JOIN email e ON e.id = v.email_id
            WHERE v.pushed_at IS NULL AND v.expired = 0
              AND v.kind IN ('action','question')
              AND v.action IS NOT NULL AND v.action <> ''
            ORDER BY v.due_utc IS NULL, v.due_utc""" % (v_org, v_sit, v_whr)).fetchall()

    if not rows:
        print("没有待推送的行动项")
        db.close()
        return

    if not (QUIET_START <= now.hour < QUIET_END):
        print("%d 点不在推送时段，%d 条行动项留到下次" % (now.hour, len(rows)))
        db.close()
        return

    use = rows[:MAX_SHOW]

    # ---------- 写入台账 ----------
    for r in use:
        db.execute("""INSERT INTO tasks(email_id, title, due_utc, link, link_host,
                                        link_short, credential, created_at,
                                        org, situation, where_hint)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(email_id) DO UPDATE SET
                        title=excluded.title, due_utc=excluded.due_utc,
                        link=excluded.link, link_host=excluded.link_host,
                        link_short=excluded.link_short,
                        credential=excluded.credential,
                        org=excluded.org, situation=excluded.situation,
                        where_hint=excluded.where_hint""",
                   (r["email_id"], clean(r["action"]), r["due_utc"],
                    r["link"], r["link_host"], r["link_short"],
                    r["credential"], stamp,
                    # 和 email_verdict 同一套白名单清洗（clean 内部就是 sanitize+UNSAFE），
                    # 入库先截到和分析层一样的长度；卡片渲染时再按版面容长。
                    clean(r["org"] or ""), clean(r["situation"] or ""),
                    clean(r["where_hint"] or "")))
    db.commit()

    # 取回刚写进台账的编号 —— 按钮的 value 要用它（email_id 不是任务编号）
    tid_of = {}
    for r in use:
        row = db.execute("SELECT id FROM tasks WHERE email_id=?", (r["email_id"],)).fetchone()
        if row:
            tid_of[r["email_id"]] = row[0]

    # ---------- 组装卡片（一行一条，链接内联，按紧急度分组）----------
    from datetime import timezone as _tz
    today = now.date()
    LOW = re.compile(r"邮箱验证|注册确认|绑定手机|订阅确认|激活账号")

    def due_dt(iso):
        if not iso:
            return None
        try:
            d = datetime.fromisoformat(iso)
            return d if d.tzinfo else d.replace(tzinfo=_tz.utc)
        except Exception:
            return None

    groups = {"today": [], "later": [], "none": [], "low": []}
    for r in use:
        act = clean(r["action"])
        if LOW.search(act):
            groups["low"].append(r)
            continue
        d = due_dt(r["due_utc"])
        if d and d.astimezone(CST).date() == today:
            groups["today"].append(r)
        elif d:
            groups["later"].append(r)
        else:
            groups["none"].append(r)

    elements, creds = [], []
    n = 0
    for key, title in (("today", "🔥 今天截止"), ("later", "📌 之后"),
                       ("none", "⚪ 没有截止时间")):
        items = groups[key]
        if not items:
            continue
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": "**%s**（%d）" % (title, len(items))}})
        for r in items:
            n += 1
            due = fmt_due(r["due_utc"])
            # 一条任务说清三件事（三张卡共用 common.task_lines，排版一字不差）：
            #   要做什么 → 标题（有可用链接就内联，点标题直接跳）
            #   发生了什么 → situation（只在有内容时出现，空着不占行）
            #   去哪做/截止/有没有链接 → 元信息行（同一行里说完，不额外占块）
            head, sit, meta = task_lines(
                clean(r["action"]), r["link"], r["link_short"],
                org=r["org"] or "", situation=r["situation"] or "",
                where=r["where_hint"] or "",
                due_txt=("⏰ %s" % due) if due else "",
                host=r["link_host"], limit=80)
            body = "**%d** %s" % (n, head)
            if sit:
                body += "\n　%s" % sit
            body += "\n　%s" % meta
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})
            # 一键完成：点一下就把这条标掉（可撤销，撤销按钮在确认消息里）
            tid = tid_of.get(r["email_id"])
            if tid:
                elements.append(done_action(tid))
            if r["credential"]:
                creds.append("%d. %s" % (n, sanitize(r["credential"], 40)))
            # 短链的告警已经在元信息行里（common.link_meta），不再单开一行重复说

    if groups["low"]:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "**⚪ 可忽略**（%d）：%s" % (
                len(groups["low"]),
                "、".join(sanitize(clean(r["action"]), 16) for r in groups["low"][:3]))}})

    if creds:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "**🔑 登录凭据**\n" + "\n".join(creds)}})

    if len(rows) > MAX_SHOW:
        elements.append({"tag": "note", "elements": [{
            "tag": "plain_text", "content": "还有 %d 条，下一轮继续" % (len(rows) - MAX_SHOW)}]})

    open_n = db.execute("SELECT COUNT(*) FROM tasks WHERE state='todo'").fetchone()[0]
    done_n = db.execute("SELECT COUNT(*) FROM tasks WHERE state='done'").fetchone()[0]
    elements.append({"tag": "note", "elements": [{
        "tag": "plain_text",
        "content": "台账 %d 待办 · %d 已完成｜回复「完成 编号」标记，说「状态」看全貌"
                   % (open_n, done_n)}]})

    head = "📋 %d 件事待处理" % len(use)
    if groups["today"]:
        head += "　🔥 今天 %d 件" % len(groups["today"])
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red" if groups["today"] else "blue",
                   "title": {"tag": "plain_text", "content": head}},
        "elements": elements,
    }

    ok, msg = feishu.send_card(card)
    print("推送: %s（%d 条）" % ("成功" if ok else "失败 " + msg, len(use)))
    if ok:
        for r in use:
            db.execute("UPDATE email_verdict SET pushed_at=? WHERE email_id=?",
                       (stamp, r["email_id"]))
        db.commit()
    db.close()


if __name__ == "__main__":
    main()
