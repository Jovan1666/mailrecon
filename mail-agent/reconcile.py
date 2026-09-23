#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对账 v3：只看求职邮件，配对并分类。

配对规则（关键）：
  - 公司自有域名  -> 按「域名」配对（发给 hr@x.example.com，回复可能来自 campus@x.example.com）
  - 公共邮箱      -> 按「完整地址」配对（发给 100000003@qq.com，只有它本人回的才算）

分类：真人回复 / 自动回复 / 退信 / 没回音。
只读账本，不碰邮箱。
"""
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta

DB = "/root/mail-agent/mail.db"
CST = timezone(timedelta(hours=8))

# 已发送里，哪些算"求职邮件"
JOB_PAT = re.compile(
    r"应聘|简历|求职|面试|笔试|测评|录用|实习|校招|应届|管培|投递|招聘|工程师|助理|专员|岗位")

# 公共邮箱：必须按完整地址配对，不能按域名
PUBLIC = {
    "qq.com", "vip.qq.com", "foxmail.com", "163.com", "126.com", "yeah.net",
    "gmail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "sina.com", "sina.cn", "sohu.com", "139.com", "189.cn", "21cn.com",
    "aliyun.com", "tom.com", "263.net", "wo.cn",
}

BOUNCE_PAT = re.compile(
    r"退信|未送达|发送失败|投递失败|Undelivered|Delivery Status|Mail Delivery|"
    r"failure notice|returned mail|无法投递", re.I)
BOUNCE_FROM = re.compile(r"^(postmaster|mailer-daemon|mail-daemon)@", re.I)
AUTO_PAT = re.compile(
    r"auto.?reply|自动回复|自动答复|系统自动|out of office|away from|"
    r"已收到您的|感谢您的来信|自动确认", re.I)
# 平台群发/营销（防止误判成回复）
SPAM_PAT = re.compile(
    r"尽在QQ邮箱|邮箱APP|升级为|会员|订阅|job alert|职位推荐|邀请投递|"
    r"热招|抢面试先机|立即投递|newsletter", re.I)


def match_key(addr):
    """公司域名 -> 返回域名；公共邮箱 -> 返回完整地址。"""
    a = (addr or "").strip().lower()
    if "@" not in a:
        return ""
    d = a.split("@", 1)[1]
    return a if d in PUBLIC else d


def fmt(ts):
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def main():
    db = sqlite3.connect(DB)
    rows = db.execute(
        """SELECT f.role, e.internaldate, e.from_addr, e.to_addr, e.subject
           FROM email e JOIN folder f ON f.id=e.folder_id
           WHERE e.fetched=1 ORDER BY e.internaldate""").fetchall()

    sent_job, sent_other = defaultdict(list), []
    recv = defaultdict(list)
    for role, ts, frm, to, subj in rows:
        subj = subj or ""
        if role == "sent":
            k = match_key(to) or match_key(frm)
            if not k:
                continue
            if JOB_PAT.search(subj):
                sent_job[k].append((ts, to or frm, subj))
            else:
                sent_other.append((ts, to or frm, subj))
        else:
            k = match_key(frm)
            if k:
                recv[k].append((ts, frm or "", subj))

    targets = set(sent_job)
    print("=" * 66)
    print("求职邮件对账报告")
    print("=" * 66)
    print("发出的求职邮件：%d 封，涉及 %d 个收件对象" % (
        sum(len(v) for v in sent_job.values()), len(targets)))
    print("（另有 %d 封已发送邮件与求职无关，已排除）" % len(sent_other))
    print()

    got_reply, got_auto, got_bounce, got_nothing, got_spam = [], [], [], [], []
    for k in targets:
        msgs = recv.get(k, [])
        bounces = [m for m in msgs if BOUNCE_PAT.search(m[2]) or BOUNCE_FROM.match(m[1])]
        rest = [m for m in msgs if m not in bounces]
        autos = [m for m in rest if AUTO_PAT.search(m[2])]
        spam = [m for m in rest if SPAM_PAT.search(m[2]) and m not in autos]
        human = [m for m in rest if m not in autos and m not in spam]

        if human:
            got_reply.append((k, human, sent_job[k]))
        elif autos:
            got_auto.append((k, autos, sent_job[k]))
        elif bounces:
            got_bounce.append((k, bounces, sent_job[k]))
        else:
            got_nothing.append((k, sent_job[k]))
        if spam:
            got_spam.append((k, spam))

    total = len(targets) or 1
    print("  ✅ 真人回复       ：%2d 个" % len(got_reply))
    print("  📮 只收到自动回复 ：%2d 个" % len(got_auto))
    print("  ⚠️  退信（没送到）  ：%2d 个" % len(got_bounce))
    print("  ❌ 完全没回音     ：%2d 个" % len(got_nothing))
    print("  → 真人回复率 %.0f%%" % (100.0 * len(got_reply) / total))

    print()
    print("=" * 66)
    print("✅ 真人回复（%d 个）" % len(got_reply))
    print("=" * 66)
    for k, human, sentl in sorted(got_reply, key=lambda x: max(m[0] or "" for m in x[1]), reverse=True):
        h = sorted(human)[-1]
        s = sorted(sentl)[0]
        print("  %s" % k)
        print("     我发出 %s  %s" % (fmt(s[0]), s[2][:46]))
        print("     收到   %s  %s" % (fmt(h[0]), h[2][:46]))

    if got_auto:
        print()
        print("=" * 66)
        print("📮 只收到自动回复（%d 个）" % len(got_auto))
        print("=" * 66)
        for k, autos, sentl in got_auto:
            h = sorted(autos)[-1]
            s = sorted(sentl)[0]
            print("  %-32s 发出 %s -> 自动回复 %s" % (k, fmt(s[0]), fmt(h[0])))

    if got_bounce:
        print()
        print("=" * 66)
        print("⚠️  退信：地址不对，需要换邮箱重投（%d 个）" % len(got_bounce))
        print("=" * 66)
        for k, b, sentl in got_bounce:
            s = sorted(sentl)[0]
            print("  %-32s 发给 %s" % (k, s[1]))

    print()
    print("=" * 66)
    print("❌ 完全没回音（%d 个，按投递时间从早到晚）" % len(got_nothing))
    print("=" * 66)
    now = datetime.now(timezone.utc)
    for k, sentl in sorted(got_nothing, key=lambda x: min(t for t, _, _ in x[1])):
        s = sorted(sentl)[0]
        try:
            dt = datetime.fromisoformat((s[0] or "").replace("Z", "+00:00"))
            days = "%2d 天" % (now - dt).days
        except Exception:
            days = " ?"
        print("  %-34s 发出 %s （%s前）" % (k, fmt(s[0]), days))

    # ---------- 自检 ----------
    print()
    print("=" * 66)
    print("自检")
    print("=" * 66)
    bad = []
    for k, human, sentl in got_reply:
        first_sent = min(t or "" for t, _, _ in sentl)
        if not any((h[0] or "") >= first_sent for h in human):
            bad.append(("时间顺序", k))
        if k in PUBLIC:
            bad.append(("公共邮箱按地址匹配-OK(仅提示)", k))
    print("  真人回复中时间顺序异常的：%d %s" % (
        len([1 for t, _ in bad if t == "时间顺序"]),
        [k for t, k in bad if t == "时间顺序"] or ""))
    print("  真人回复中含公共邮箱的：%s" % ([k for t, k in bad if t != "时间顺序"] or "无"))
    print("  被排除的疑似营销/群发：%d 个域名 %s" % (
        len(got_spam), [k for k, _ in got_spam][:5]))
    print("  被排除的非求职已发送：%d 封" % len(sent_other))
    for t, to, subj in sorted(sent_other)[-6:]:
        print("      %s  %-26s %s" % (fmt(t), (to or "?")[:26], (subj or "")[:38]))
    db.close()


if __name__ == "__main__":
    main()
