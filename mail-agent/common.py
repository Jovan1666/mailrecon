#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""各脚本共用的规则与工具。

放在这里是为了避免"同一个正则在三个文件里各写一遍、改了一处忘另一处"的规则漂移。
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import platform_cfg  # noqa: E402

DB = "/root/mail-agent/mail.db"
ENV = "/root/mail-agent/.env"
CST = timezone(timedelta(hours=8))

# 公共邮箱：必须按完整地址配对，不能按域名（否则腾讯营销邮件会被当成 HR 回复）
PUBLIC = {
    "qq.com", "vip.qq.com", "foxmail.com", "163.com", "126.com", "yeah.net",
    "gmail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "sina.com", "sina.cn", "sohu.com", "139.com", "189.cn", "21cn.com",
    "aliyun.com", "tom.com", "263.net", "wo.cn",
}

# 已发送里，哪些算"求职邮件"（决定了对账的锚点）
JOB_PAT = re.compile(
    r"应聘|简历|求职|面试|笔试|测评|录用|实习|校招|应届|管培|投递|招聘|"
    r"工程师|助理|专员|岗位|申请|秋招|春招|内推|offer|意向|职位|"
    r"resume|application|intern|engineer", re.I)

# 退信：说明信根本没送到
BOUNCE_PAT = re.compile(
    r"退信|未送达|发送失败|投递失败|Undelivered|Delivery Status|Mail Delivery|"
    r"failure notice|returned mail|无法投递", re.I)
BOUNCE_FROM = re.compile(r"^(postmaster|mailer-daemon|mail-daemon)@", re.I)

# 自动回复：送到了，但对方是机器回的（只按主题兜底，头部特征见 sync 的 Auto-Submitted）
AUTO_PAT = re.compile(
    r"auto.?reply|autoreply|自动回复|自动答复|系统自动|自动发送|"
    r"out of office|away from|已收到您的|感谢您的来信|自动确认", re.I)

# 平台群发 / 营销 / 通知（不该当成"公司回你"）
#
# 源码里**不写死任何具体服务名**：那串东西会暴露"这个用户常收到哪些服务的邮件"。
# 真实词表由使用者在 platform_domains.txt 的 [spam_brand] 节里自己填，
# 启动时合并进来；内置默认一个品牌词都没有。
_BRAND_ALT = "|".join(re.escape(b) for b in platform_cfg.spam_brands())
SPAM_PAT = re.compile(
    r"尽在QQ邮箱|邮箱APP|升级为|会员|订阅|job alert|职位推荐|邀请投递|"
    r"热招|抢面试先机|立即投递|newsletter|<广告>|no-?reply|"
    r"验证码|一次性代码|一次性密码|密码重置|安全提醒|登录提醒|"
    r"发票|账单|扣款|收据|订单|物流|退款|"
    r"活动邀请|优惠|折扣|限时" + (("|" + _BRAND_ALT) if _BRAND_ALT else ""), re.I)

# 机器人发件人（前缀级判定，最可靠）
NOREPLY_FROM = re.compile(
    r"^(no-?reply|noreply|donotreply|do-not-reply|notification|notifications|"
    r"mailer|postmaster|automated|system|service|support|info|news|newsletter|"
    r"marketing|billing|invoice|receipt|account|security|verify|10000)@", re.I)

# 正文引文分隔（取"新内容"用）
QUOTE_PAT = re.compile(
    r"\n\s*(?:-{3,}\s*)?(?:原始邮件|Original Message|From:|发件人[:：]|"
    r"在.*?写道[:：]|_{10,})", re.I)


def load_env(path=ENV):
    cfg = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def match_key(addr):
    """公司域名 -> 返回域名；公共邮箱 -> 返回完整地址。"""
    a = (addr or "").strip().lower()
    if "@" not in a:
        return ""
    d = a.split("@", 1)[1]
    return a if d in PUBLIC else d


def fmt(ts, with_date=False):
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%m-%d %H:%M" if not with_date else "%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def sanitize(s, limit=200):
    """清掉控制字符和换行 —— 邮件主题可能含折行头，会把飞书消息排版搞乱。"""
    s = (s or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\u2028\u2029\ufeff]", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s[:limit]


# ---------------------------------------------------------------- 卡片渲染规则
#
# 一条任务怎么排版（标题 / 为什么有这件事 / 去哪做 / 有没有链接）在**三张卡**里
# 必须长得一样：看板卡(board.py)、推送卡(push_actions.py)、台账卡(taskboard.py)。
# 规则只写一份放在这里 —— 和 title_md 同一个理由，改了这边三处一起变。

# tasks 表为"主体/发生了什么/去哪做"加的三列。幂等 ALTER，列名写死不做拼接。
TASK_NEW_COLS = (("tasks", "org", "TEXT"),
                 ("tasks", "situation", "TEXT"),
                 ("tasks", "where_hint", "TEXT"))

# 每条渲染出来的元的顺序：去哪做 → 截止 → 链接情况
LIMIT_SIT, LIMIT_WHERE, LIMIT_ORG = 44, 40, 24


def ensure_task_cols(db):
    """给 tasks 补 org / situation / where_hint（幂等，可重复调用）。

    为什么不让 push_actions 一个脚本负责建列：board.py 和 taskboard.py 也要读这几列，
    谁先跑谁建，就不会出现"分析脚本还没跑、看板先崩在 no such column"。

    为什么加新列而不是塞进现成的 note：
      * note 是一个槽位，而 situation（一句话情境）和 where_hint（去哪做）的渲染
        规则完全不同（前者独占一行，后者并进元信息行），塞一起就得每个读者自己拆；
      * note 的语义应该留给"用户自己的备注"，和"从邮件里抽出来的事实"混在一起，
        以后想加备注就没地方放了；
      * email_verdict 里本来就叫 org/situation/where_hint，tasks 用同名 = 一条直线
        搬过来，不用做名字翻译。
    """
    for table, col, typ in TASK_NEW_COLS:
        have = [r[1] for r in db.execute("PRAGMA table_info(%s)" % table)]
        if col not in have:
            # 补列是**写**操作：整点那轮推送可能正拿着写锁，先等一会儿再动
            # （和 analyze.py 一个路子），别让"列还没建好"变成看板打不开。
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
    db.commit()


def link_ok(link, link_short=False):
    """邮件里的链接能不能做成可点入口？能就返回 url，不能返回 ""。

    两条规矩（三张卡 + 看板底部按钮共用同一个函数）：
      * 只认 http/https —— 邮件正文是外部输入，可能夹带 javascript: / data: 伪协议；
      * 短链不做链接 —— 短链是"信任转移"最容易被利用的形态（红队提醒）。
      另外 url 里出现空白/控制字符一律拒绝：渲染是字符串拼接，换行会把卡片搞乱。
    """
    u = (link or "").strip()
    if not u or link_short:
        return ""
    if not u.lower().startswith(("http://", "https://")):
        return ""
    if re.search(r"[\s\x00-\x1f\x7f]", u):
        return ""
    return u


def md_text(s):
    """markdown 正文里的安全文本：挡掉"用文本拼出一个链接"的注入。

    卡片是 markdown 渲染的，而标题/情境/去哪做都可能间接来自邮件正文。
    形如 `x](https://evil)` 的文本拼进 `[标题](url)` 之后，会被渲染成一个
    指向 evil 的链接 —— 绕过了 link_ok 的 http/https + 短链判定。
    正常文字里的中括号（"【】"、"（）"）都保留，只在中括号+圆括号同时出现时才替换。
    """
    s = s or ""
    if "](" in s or ")[" in s:
        s = s.replace("[", "［").replace("]", "］")
    return s


def title_md(text, link=None, link_short=False, limit=60):
    """任务标题 → 飞书 markdown：有可用链接就内联链接，否则纯文本。

    看板卡(board.py) / 推送卡(push_actions.py) / 台账卡(taskboard.py) 共用这一条规则。
    之前这条规则在 push_actions 和 board 里各写一份，结果 board 那份漏了内联链接，
    用户在看板上点不到任务链接（同一个问题犯了第二次），所以收到这里来。

    短链一律不做链接：短链是"信任转移"最容易被利用的形态（红队提醒）。
    """
    t = md_text(sanitize(text, limit))
    u = link_ok(link, link_short)
    return "[%s](%s)" % (t, u) if u else t


def org_tag(org, *seen, limit=LIMIT_ORG):
    """标题后面挂主体（`@ 公司名/域名`）—— 标题/情境里已经出现过的就不重复挂。

    用户反馈原话："我还以为是哪个公司让我重发简历"。主语必须看得见，
    但"完成星辰科技在线测评 @ 星辰科技"这种重复也没必要，所以先查一遍。
    """
    o = md_text(sanitize(org or "", limit))
    if not o:
        return ""
    low = o.lower()
    for h in seen:
        if h and low in h.lower():
            return ""
    return " @ %s" % o


def link_meta(link=None, link_short=False, host=None, limit=26):
    """『有没有跳转链接』的统一回答（三张卡一字不差地说同一句话）。

    四种情况都说清楚，省得用户以为是卡片没显示出来：
      能点   → 报域名
      短链   → 明说没给跳转、要自行核对来源
      有链接但不是 http/https（邮件里塞了伪协议）→ 明说这条链接不可点
      压根没有 → 明说"邮件里没给链接"
    """
    h = md_text(sanitize(host or "", limit))
    if link_ok(link, link_short):
        return "🔗 %s" % h if h else "🔗 可点链接"
    if not (link or "").strip():
        return "🔗 邮件里没给链接"
    if link_short:
        return "⚠️ 短链%s，请自行核对来源" % ("（%s）" % h if h else "")
    return "⚠️ 邮件里给的链接不是 http/https，没给跳转"


def task_lines(title, link=None, link_short=False, org="", situation="", where="",
               due_txt="", host=None, extras=(), limit=60, link_info=True):
    """一条任务 → 1~3 行 markdown（三张卡的唯一排版入口）。

    返回 (head, sit, meta)，空串表示这一行不渲染：

      head  标题（有可用链接就内联）＋ 主体 @org
      sit   「发生了什么」——只在有内容时出现。用户原话："我还以为是哪个公司让我
            重发简历，原来是邮箱不用了" —— 这行就是回答它的。
      meta  『📍 去哪做 · ⏰ 截止 · 🔗 链接情况 · 调用方追加项』

    排版取舍（用户抱怨过"7 条任务每条占 4 行太挤"）：
      * 这条任务的"要做什么"就是标题本身，不另起一行；
      * situation 是句子，独占一行，但**只在有内容时才出现** —— 没有情境的任务
        仍然只有"标题 + 元信息"两行；
      * where 是短语（≤40 字），并进元信息行，不额外占行；
      * 三行都在同一个 div 里用 \\n 分隔，飞书的块间距不会因此变多。
    """
    sit = md_text(sanitize(situation or "", LIMIT_SIT))
    whe = md_text(sanitize(where or "", LIMIT_WHERE))
    head = title_md(title, link, link_short, limit) + org_tag(org, title, sit, whe)
    parts = []
    if whe:
        parts.append("📍 %s" % whe)
    if due_txt:
        parts.append(due_txt)
    for x in extras:
        if x:
            parts.append(x)
    if link_info:
        parts.append(link_meta(link, link_short, host))
    return head, sit, " · ".join(parts)


def task_body(tid, title, **kw):
    """board / taskboard 的心跳格式：`**5. 标题**` + 情境行 + 元信息行。"""
    head, sit, meta = task_lines(title, **kw)
    body = "**%s. %s**" % (tid, head)
    if sit:
        body += "\n　%s" % sit
    if meta:
        body += "\n　%s" % meta
    return body


def job_targets(db):
    """从已发送里取出"我投过的对象"（公司域名 或 公共邮箱完整地址）。"""
    from collections import defaultdict
    sent = defaultdict(list)
    for to, ts, subj in db.execute(
            """SELECT e.to_addr, e.internaldate, e.subject
               FROM email e JOIN folder f ON f.id=e.folder_id
               WHERE f.role='sent' AND e.fetched=1 AND e.origin<>'stale'"""):
        subj = subj or ""
        k = match_key(to)
        if k and JOB_PAT.search(subj):
            sent[k].append((ts, to, subj))
    return sent
