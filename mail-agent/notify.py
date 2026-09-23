#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变化检测 + 推送飞书（v2）。

修掉的三个致命问题：
  1. 加"相关性判断" —— 只有「我投过的对象的来信」和「招聘相关通知」才推，
     否则各类平台的通知会被当成"公司回你邮件了"（实测误报率 94%）。
  2. 首次基线只吞"基线之前就在"的邮件，不再吞掉之后到达的真实邮件。
  3. 只标记"真正推送过"的条目；被截断的留到下一轮，不会永久消失。

发送走 root 直调飞书 API（不经 hermes 用户），消除提权链。
只处理 origin='incremental' 的邮件 —— 历史邮件永不推送。
"""
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, "/root/mail-agent")
from common import (DB, CST, BOUNCE_PAT, BOUNCE_FROM, AUTO_PAT, NOREPLY_FROM,
                    SPAM_PAT, fmt, match_key, sanitize, job_targets)
import feishu

QUIET_START, QUIET_END = 8, 20     # 只在 8:00–20:00 推送
MAX_SHOW = 8                       # 单条消息最多展示几条事件

# 招聘相关通知（可能是网申渠道来的，不在"我投过的对象"里，但同样重要）
RECRUIT_PAT = re.compile(
    r"应聘|简历|面试|笔试|测评|录用|offer|实习|校招|秋招|春招|内推|招聘|岗位|"
    r"感谢.{0,6}投递|投递成功|申请.{0,4}已|邀请.{0,6}(参加|面试|测评)|"
    r"申请进度|申请状态|人才|校招组|campus", re.I)


def main():
    db = sqlite3.connect(DB)
    now = datetime.now(CST)
    stamp = now.isoformat(timespec="seconds")

    # ---------- 相关性锚点：我投过哪些对象 ----------
    targets = job_targets(db)
    target_keys = set(targets)

    rows = db.execute(
        """SELECT e.id, f.role, e.internaldate, e.from_addr, e.subject
           FROM email e JOIN folder f ON f.id=e.folder_id
           WHERE e.fetched=1 AND e.notified_at IS NULL AND e.origin='incremental'
           ORDER BY e.internaldate""").fetchall()

    # ---------- 首次运行：只吞基线时刻之前就存在的 ----------
    first_run = not db.execute(
        "SELECT 1 FROM sync_state WHERE k='notify_baseline_at'").fetchone()
    if first_run:
        base = db.execute("SELECT MIN(baseline_at) FROM folder WHERE baseline_at").fetchone()[0]
        n = db.execute(
            """UPDATE email SET notified_at=?
               WHERE origin='incremental' AND notified_at IS NULL
                 AND (internaldate IS NULL OR internaldate <= ?)""",
            (stamp, base)).rowcount
        db.execute("INSERT INTO sync_state(k,v) VALUES('notify_baseline_at',?)", (stamp,))
        db.commit()
        left = db.execute(
            """SELECT COUNT(*) FROM email WHERE origin='incremental'
               AND notified_at IS NULL AND fetched=1""").fetchone()[0]
        print("首次运行：吞掉基线前的 %d 封，保留 %d 封待推" % (n, left))
        if not left:
            db.close()
            return

    # ---------- 分类 ----------
    replies, recruit, bounces, others = [], [], [], []
    for eid, role, ts, frm, subj in rows:
        subj = subj or ""
        key = match_key(frm)

        if BOUNCE_PAT.search(subj) or BOUNCE_FROM.match(frm or ""):
            bounces.append((eid, key or frm, frm, subj, ts))
        elif key and key in target_keys and not NOREPLY_FROM.match(frm or "") \
                and not AUTO_PAT.search(subj) and not SPAM_PAT.search(subj):
            replies.append((eid, key, frm, subj, ts))
        elif RECRUIT_PAT.search(subj) and not SPAM_PAT.search(subj):
            recruit.append((eid, key or frm, frm, subj, ts))
        else:
            others.append((eid, key or frm, frm, subj, ts))

    if not (replies or recruit or bounces):
        # 不推的也要标记，否则每轮都会重新扫（但留个计数在日志里）
        for eid, *_ in others:
            db.execute("UPDATE email SET notified_at=? WHERE id=?", (stamp, eid))
        db.commit()
        print("无可推送事件（另有 %d 封与求职无关，已跳过）" % len(others))
        db.close()
        return

    if not (QUIET_START <= now.hour < QUIET_END):
        db.commit()
        print("%d 点不在推送时段，%d 条事件留到下次"
              % (now.hour, len(replies) + len(recruit) + len(bounces)))
        db.close()
        return

    # ---------- 组装消息（只展示前 MAX_SHOW 条，其余的下一轮继续） ----------
    shown, lines = [], []
    budget = MAX_SHOW

    def take(lst):
        nonlocal budget
        got = lst[:budget]
        budget -= len(got)
        return got

    use_replies = take(replies) if budget else []
    use_recruit = take(recruit) if budget else []
    use_bounce = take(bounces) if budget else []

    if use_replies:
        lines.append("📬 %s" % ("有 %d 家公司回你邮件了" % len(use_replies)
                                if len(use_replies) > 1 else "有公司回你邮件了"))
        for eid, key, frm, subj, ts in use_replies:
            lines.append("")
            lines.append("● %s" % key)
            lines.append("  %s  %s" % (fmt(ts), sanitize(subj, 60)))
            shown.append(eid)

    if use_recruit:
        if lines:
            lines.append("")
        lines.append("📋 %s" % ("有 %d 封招聘相关邮件" % len(use_recruit)
                                if len(use_recruit) > 1 else "有招聘相关邮件"))
        for eid, key, frm, subj, ts in use_recruit:
            lines.append("")
            lines.append("● %s" % key)
            lines.append("  %s  %s" % (fmt(ts), sanitize(subj, 60)))
            shown.append(eid)

    if use_bounce:
        if lines:
            lines.append("")
        lines.append("⚠️ %d 封邮件被退回（地址不对，建议换邮箱重投）" % len(use_bounce))
        for eid, key, frm, subj, ts in use_bounce:
            lines.append("● %s  %s" % (key, fmt(ts)))
            shown.append(eid)

    total = len(replies) + len(recruit) + len(bounces)
    if total > len(shown):
        lines.append("")
        lines.append("（本轮先推 %d 条，还有 %d 条下一轮继续）" % (len(shown), total - len(shown)))

    text = "\n".join(lines)
    ok, msg = feishu.send(text)
    print("推送: %s（%d 条）" % ("成功" if ok else "失败 " + msg, len(shown)))

    if ok:
        for eid in shown:
            db.execute("UPDATE email SET notified_at=? WHERE id=?", (stamp, eid))
        # 与求职无关的也标记，避免每轮重复扫
        for eid, *_ in others:
            db.execute("UPDATE email SET notified_at=? WHERE id=?", (stamp, eid))
        db.commit()
    db.close()


if __name__ == "__main__":
    main()
