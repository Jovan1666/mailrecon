#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从邮件原始正文里挑选"该点的那个链接"。

为什么要单独做：脱敏模块为了保护隐私会把 URL 里的令牌打码（aId=xxx → [已脱敏]），
所以送模型的那份文本里链接是坏的。要展示给用户点，必须从**原始正文**里单独抽。

安全考虑：链接来自任何人可发的邮件，属于不可信内容。所以
  - 只接受 http/https
  - 短链（bit.ly 之类）不给点，只显示域名并告警
  - 展示时一并给出域名，让用户自己判断
"""
import os
import re
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import platform_cfg  # noqa: E402

# 只接受这两种协议
ALLOWED_SCHEMES = ("http", "https")

# 短链：不给可点击形态（红队提醒：这是"信任转移"最容易被利用的形态）
SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "shorturl.at", "rebrand.ly",
    "cutt.ly", "sourl.cn", "url.cn", "dwz.cn", "suo.im", "mrw.so",
}

# 噪音链接：退订、隐私政策、社交账号等，不是"要你点的那个"
NOISE = re.compile(
    r"unsubscribe|退订|取消订阅|隐私|privacy|policy|terms|服务条款|"
    r"weibo|weixin|twitter|facebook|linkedin|youtube|instagram|"
    r"\.png|\.jpg|\.jpeg|\.gif|\.css|\.js$|/logo|/banner|/pixel|open\.feishu|"
    r"mail\.qq\.com|service\.mail\.qq\.com|qq\.com/help", re.I)

# 像是"要你操作"的链接特征。
# 源码里只留**通用动作词**。具体平台名不写死：它们会暴露"这个用户
# 用过哪些服务"。真实词表由使用者在 platform_domains.txt 的 [action_hint]
# 节里自己填，启动时合并进来。
_HINT_ALT = "|".join(re.escape(h) for h in platform_cfg.action_hints())
ACTION_HINT = re.compile(
    r"ceping|assess|exam|test|survey|questionnaire|aId=|candidate|"
    r"confirm|apply|interview|schedule|booking|appoint|verify|activate|"
    r"login|signin|form|shl|hackerrank|"
    r"测评|笔试|面试|问卷|确认|预约|投递|申请"
    + (("|" + _HINT_ALT) if _HINT_ALT else ""), re.I)

URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]，。、；：）】\x00-\x1f]+")


def _root_domain(host):
    """取注册域（粗略）：a.b.example.com -> example.com"""
    parts = (host or "").lower().split(".")
    if len(parts) <= 2:
        return ".".join(parts)
    return ".".join(parts[-2:])


def _clean(u):
    u = u.rstrip(".,;:!?)]}>\"'").replace("&amp;", "&")
    return u[:400]


def extract(raw_text, raw_html, from_addr=""):
    """返回候选链接列表（已按"最可能是要点的那个"排序）。"""
    found = []
    for m in URL_RE.findall(raw_text or ""):
        found.append(m)
    for m in re.findall(r'(?i)href=["\'](https?://[^"\']+)', raw_html or ""):
        found.append(m)

    sender_host = (from_addr or "").split("@")[-1].lower()
    sender_root = _root_domain(sender_host)

    seen, cands = set(), []
    for raw in found:
        u = _clean(raw)
        if u in seen:
            continue
        seen.add(u)
        try:
            p = urlparse(u)
        except Exception:
            continue
        if p.scheme not in ALLOWED_SCHEMES or not p.hostname:
            continue
        host = p.hostname.lower()
        if NOISE.search(u) or NOISE.search(host):
            continue

        score = 0
        if ACTION_HINT.search(u):
            score += 2
        # 同源链接：发件人和链接同一个注册域，强烈说明是正规入口
        if sender_root and _root_domain(host) == sender_root:
            score += 3
        if p.path not in ("", "/"):
            score += 1
        if host in SHORTENERS:
            score -= 5
        if len(u) > 200:          # 超长链接往往是追踪像素
            score -= 2
        cands.append((score, u, host))

    cands.sort(key=lambda x: -x[0])
    return cands


def best_link(raw_text, raw_html, from_addr=""):
    """挑一个最像"该点的那个"返回 (url, host, is_shortener)；没有就 (None, None, False)。

    分数 <= 0 的一律不要 —— 宁可不给链接，也不给一个可疑的。
    """
    cands = extract(raw_text, raw_html, from_addr)
    if not cands:
        return None, None, False
    score, url, host = cands[0]
    if score <= 0:
        return None, None, False
    return url, host, host in SHORTENERS
