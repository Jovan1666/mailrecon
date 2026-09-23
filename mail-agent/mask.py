#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""求职邮件助手 —— 送云端模型前的正文脱敏规则。

设计原则（全部来自 30 封真实邮件的实测）：

  1) 先保护、后脱敏。
     日期 / 时间 / 金额 / 订单号 / URL / 对方邮箱先换成哨兵占位符，彻底退出数字类规则的
     视野。否则 "2026-09-25" 会被当卡号、"253.00元" 会被当手机号、QQ 超大附件 URL 里
     的 k= 十六进制串会被当银行卡号、`app100001.example.com` 里的 100001 会被当
     手机号前缀。

  2) 关键词锚定 > 纯数字长度。
     裸数字一律不动。真实样本里的裸数字几乎全是订单号(E123456789)、车次(G1234)、
     座位(5车12C)、邮编(123456)、发票号(20位)、店铺 id(100000001)。按长度乱打，
     等于把 AI 判断截止时间、核对投递对象的依据一起打掉。

  3) 长规则先行。
     身份证(18) -> 银行卡(16-19) -> 关键词凭证 -> 验证码 -> 手机号(11) -> 已掩码手机
     -> 学号 -> QQ -> 微信 -> 姓名 -> 学校 -> 专业 -> 地址/邮编。
     顺序错了，11 位手机号会被短规则打成碎片。

  4) 域名必须活下来。
     URL 只清"带令牌的路径段"和 query 值，scheme://host 与普通路由段原样保留，
     否则公司配对（recruit.xingchen.example.com / yuntu.example.com）就废了。

  5) 正则打不动的走显式白名单：地址、专业名、拼音名、微信号、学号。
     这些没有稳定词形，硬做正则必然误伤，只能配置。
"""

import re
from urllib.parse import unquote

# --------------------------------------------------------------------------
# 占位符
# --------------------------------------------------------------------------
MASK = {
    "name":     "[本人姓名]",
    "school":   "[学校]",
    "major":    "[专业]",
    "stu_id":   "[学号]",
    "phone":    "[手机号]",
    "idcard":   "[身份证]",
    "bankcard": "[银行卡]",
    "email":    "[本人邮箱]",
    "qq":       "[QQ号]",
    "passcode": "[测评通行证]",
    "vcode":    "[验证码]",
    "address":  "[地址]",
    "postcode": "[邮编]",
    "wechat":   "[微信号]",
    "token":    "[已脱敏]",
}

LABEL = {
    "phone": "手机号", "idcard": "身份证", "bankcard": "银行卡", "email": "本人邮箱",
    "qq": "QQ号", "passcode": "测评通行证/密码", "vcode": "验证码", "stu_id": "学号",
    "name": "姓名", "school": "学校", "major": "专业", "address": "地址",
    "postcode": "邮编", "wechat": "微信号", "token": "URL令牌",
}

# --------------------------------------------------------------------------
# 身份配置
# --------------------------------------------------------------------------
_IDENT = {
    "name": "",            # 张三
    "name_aliases": [],    # San / SanZhang / SanZhang2027
    "emails": [],          # you@example.com / you-alias@example.com
    "qq": [],              # 100000000
    "phones": [],          # 13800138000
    "schools": [],         # 某某大学
    "majors": [],          # 某某专业
    "stu_ids": [],         # 2027000001
    "wechats": [],         # your-wechat-id
    "addresses": [],       # 某某街道 —— 无条件打（够独特）
    "addresses_soft": [],  # 某某市 —— 只在地址上下文附近才打（避免误伤"某某市XX公司"）
    "generic_school": True,    # 通用 "XX大学/学院" 规则（有误伤风险，可关）
    "generic_address": True,   # 通用 "地址：xxx" 规则
    "strict_orders": False,    # True 时连订单号/发票号也打掉（会伤截止时间推理）
}

# 没有配置文件时的示例身份 —— **全部是明显的假值**，只为让自测/demo 跑得起来。
# 真实身份永远从 identity.json / $MASK_IDENTITY 读（见 load_identity_file）。
DEMO_IDENTITY = {
    "name": "张三",
    "name_aliases": ["San", "SanZhang", "SanZhang2027"],
    "emails": ["you@example.com", "you-alias@example.com"],
    "qq": ["100000000"],
    "phones": ["13800138000"],
    "schools": ["某某大学", "某某大学某某学院", "某某学院"],
    "majors": ["某某专业"],
    "stu_ids": ["2027000001"],
    "wechats": ["your-wechat-id"],
    "addresses": ["某某街道"],
    "addresses_soft": ["某某市"],
}


def set_identity(**kw):
    for k, v in kw.items():
        if k not in _IDENT:
            raise KeyError("未知身份字段: %s" % k)
        _IDENT[k] = v
    return get_identity()


def get_identity():
    return dict(_IDENT)


def load_identity_file(path=None):
    """从 JSON 读取身份白名单并填入 _IDENT，返回实际生效的配置。

    路径优先级：显式 path > 环境变量 MASK_IDENTITY > 本目录 identity.json。
    文件不存在 / 读不了 / 字段不认识时**不报错**，回落到 inline 的示例身份 ——
    这样脚本在没配身份的环境里也能跑（只是打码覆盖面变小，绝不会报错退出）。

    为什么要这个函数：真实姓名/邮箱/学校这些值**只能来自配置**，
    绝不能写死在源码里。仓库里带的是 identity.example.json（全是假值）。
    """
    import json
    import os
    cand = [path, os.environ.get("MASK_IDENTITY"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "identity.json")]
    for p in cand:
        if not p or not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            continue
        set_identity(**{k: v for k, v in cfg.items() if k in _IDENT})
        return get_identity()
    set_identity(**DEMO_IDENTITY)
    return get_identity()


# --------------------------------------------------------------------------
# 哨兵：纯 PUA 字符，不含任何字母数字，绝不会被后续数字规则二次命中
# --------------------------------------------------------------------------
_PL, _PR = "\uE000", "\uE001"
_SENT_RE = re.compile(re.escape(_PL) + r"([\uE100-\uE8FF])" + re.escape(_PR))


def _mk_sent(i):
    return _PL + chr(0xE100 + i) + _PR


# 只归一化全角数字；绝对不能动全角冒号 —— 中文正文里"："满地都是，
# 全局替换等于给模型看一份被改过的原文（实测第一个版本就踩了这个坑）。
_FW = str.maketrans("０１２３４５６７８９", "0123456789")

# --------------------------------------------------------------------------
# 1) 保护：这些内容绝不能被碰
# --------------------------------------------------------------------------
_SENS_KEYS = {
    "secret", "token", "key", "code", "sid", "session", "ticket", "auth", "password",
    "passwd", "pwd", "sign", "signature", "accesstoken", "accesskey", "uid", "user",
    "userid", "id", "aid", "candidate", "mail", "name", "email", "icon", "redirect",
    "return", "callback", "state", "t", "k", "url", "target", "invite", "verify",
    "hash", "nonce", "q", "s",
}


def _is_token_seg(seg):
    """URL 的一个路径段/参数值是不是"令牌"。真实样本校准过：
       ding-pay-a1b2c3d4e5(17) / aBcDeFgH(8, 混合大小写) / 0Z-9yXwVuT3S2R1Q0P8NoM7~~ /
       deliver-query(13, 纯小写+连字符 -> 路由，保留) / ek_qqapp(9, 路由，保留)
    """
    if not seg:
        return False
    if len(seg) >= 16:
        return True
    if any(c in seg for c in "~="):
        return True
    if re.search(r"[A-Z]", seg) and re.search(r"[a-z]", seg) and len(seg) >= 8:
        return True
    return False


def _scrub_url(m):
    """URL 只保留 scheme://host + 路由段，令牌段与敏感 query 值换成 [已脱敏]。"""
    raw = m.group(0)
    tail = ""
    while raw and raw[-1] in ".,;:!?、。，；：！？":
        tail = raw[-1] + tail
        raw = raw[:-1]
    if "://" not in raw:
        return raw + tail
    scheme, rest = raw.split("://", 1)
    frag = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
    host, path = (rest.split("/", 1) if "/" in rest else (rest, None))
    out = "%s://%s" % (scheme, host)
    if path:
        out += "/" + _scrub_pathquery(path)
    if frag:
        out += "#" + _scrub_pathquery(frag)
    return _url_pii_pass(out) + tail


def _url_pii_pass(s):
    """URL 里可能直接嵌了本人标识（实测 github.com/example-user、
       ...&name=%E5%BC%A0%E4%B8%89 这种）。这一步把它们清掉。"""
    for a in sorted(list(_IDENT["name_aliases"]) + [_IDENT["name"]], key=len, reverse=True):
        if a and len(a) >= 2:
            s = re.sub(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])",
                       MASK["name"], s, flags=re.I)
    for e in _IDENT["emails"]:
        s = s.replace(e, MASK["email"]).replace(e.lower(), MASK["email"])
    for q in _IDENT["qq"]:
        s = re.sub(r"(?<![\dA-Za-z])" + re.escape(str(q)) + r"(?![\dA-Za-z])",
                   MASK["qq"], s)
    for field, key in (("schools", "school"), ("majors", "major"),
                       ("wechats", "wechat"), ("addresses", "address"),
                       ("addresses_soft", "address")):
        for v in _IDENT[field]:
            if v:
                s = s.replace(v, MASK[key])
    return s


def _scrub_pathquery(pq):
    """把 "path?query"（或光 path）里的令牌段、敏感 query 值换掉；路由段保留。"""
    if not pq:
        return ""
    query = ""
    if "?" in pq:
        pq, query = pq.split("?", 1)
    out = ""
    if pq:
        out = "/".join(MASK["token"] if (s and _is_token_seg(unquote(s))) else s
                       for s in pq.split("/"))
    if query:
        kept = []
        for kv in query.split("&"):
            if "=" not in kv:
                kept.append(MASK["token"] if _is_token_seg(unquote(kv)) else kv)
                continue
            k, v = kv.split("=", 1)
            dv = unquote(v)
            if k.lower().replace("-", "").replace("_", "") in _SENS_KEYS \
                    or _is_token_seg(dv) or _PHONE_BARE.search(dv) or _IDCARD_BARE.search(dv):
                kept.append("%s=%s" % (k, MASK["token"]))
            else:
                kept.append(kv)
        if kept:
            out += "?" + "&".join(kept)
    return out


# 结束符里排除中文与全角括号，避免把后面的中文句子吞进 URL
_URL_RE = re.compile(
    r"(?:https?|ftp)://[^\s<>\"'，。；、）)（(】】\[\]\u4e00-\u9fff]+", re.I)

_AMOUNT_RE = re.compile(
    r"[¥￥$]\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?"
    r"|\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\s*元"
    r"|\d+(?:\.\d{1,2})?\s*(?:万元|元整)")

_DATE_RE = re.compile(
    r"(?:19|20)\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}(?:\s*日)?"
    r"|(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+(?:19|20)\d{2}"
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+(?:19|20)\d{2}"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日")

_TIME_RE = re.compile(r"(?<![\d:：])\d{1,2}[:：]\d{2}(?::\d{2})?(?![\d:：])")

_ORDER_RE = re.compile(
    r"(?:订单号码|订单号|订单编号|发票号码|发票代码|文稿编号|文稿|序列号|流水号|"
    r"运单号|快递单号|交易号|商户订单号|取票号|booking|order\s*(?:no|number|id))"
    r"[\s:：]{0,10}([A-Za-z0-9][A-Za-z0-9\-]{3,40})", re.I)

_MAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_PROTECT_LABEL = {"url": "URL", "amount": "金额", "date": "日期", "time": "时间",
                  "order": "订单号", "ext_mail": "对方邮箱"}


def _protect(text, stats):
    box = []

    def put(value):
        box.append(value)
        return _mk_sent(len(box) - 1)

    rules = [(_URL_RE, "url"), (_AMOUNT_RE, "amount"), (_DATE_RE, "date"),
             (_TIME_RE, "time")]
    if not _IDENT["strict_orders"]:
        # 订单号/发票号默认当"结构值"保护起来；开了 strict_orders 就不保护，
        # 让它被后面的长数字规则打掉（代价见报告：截止时间推理会一起废掉）
        rules.append((_ORDER_RE, "order"))
    for rx, kind in rules:
        def rep(m, kind=kind):
            stats["protected"][_PROTECT_LABEL[kind]] += 1
            return put(_scrub_url(m) if kind == "url" else m.group(0))
        text = rx.sub(rep, text)

    self_mails = {e.lower() for e in _IDENT["emails"]}
    self_alias = {e.split("@", 1)[0] for e in self_mails}
    self_alias |= {str(q).lower() for q in _IDENT["qq"]}
    self_alias |= _pinyin_forms()

    def mail_rep(m):
        addr = m.group(0)
        local = addr.split("@", 1)[0].lower()
        if addr.lower() in self_mails or local in self_alias:
            stats["hits"].append(_hit("email", addr, MASK["email"], text, m))
            return MASK["email"]
        stats["protected"]["对方邮箱"] += 1
        return put(addr)
    text = _MAIL_RE.sub(mail_rep, text)
    return text, box


def _pinyin_forms():
    out = set()
    for a in list(_IDENT["name_aliases"]) + [_IDENT["name"]]:
        if a:
            k = re.sub(r"[^a-z0-9]", "", a.lower())
            if k:
                out.add(k)
    return out


# --------------------------------------------------------------------------
# 2) 裸数字类规则
# --------------------------------------------------------------------------
_IDCARD_BARE = re.compile(
    r"(?<![\dA-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![\dA-Za-z])")
_PHONE_BARE = re.compile(r"(?<![\dA-Za-z])(?:\+?86[\s\-]?)?1[3-9]\d{9}(?![\dA-Za-z])")
_BANK_BARE = re.compile(r"(?<![\dA-Za-z])\d{16,19}(?![\dA-Za-z])")
_MASKED_PHONE = re.compile(
    r"(?<![\dA-Za-z])1[3-9]\d[\*\u2022\uFF0A\u00D7xX]{2,9}\d{2,4}(?![\dA-Za-z])")
_BANK_KW = re.compile(r"(?:银行卡号?|账号|卡号|储蓄卡|借记卡|信用卡)\s*[:：]?\s*(\d[\d\s\-]{10,25})")

# --------------------------------------------------------------------------
# 3) 关键词锚定规则
# --------------------------------------------------------------------------
_PASSCODE_KW = re.compile(
    r"(?:通行证|测评密码|考试密码|作答密码|登录密码|笔试密码|测评码|邀请码|准考证号|"
    r"pass\s*code|passcode|password)"
    r"\s*(?:是|为|[:：])?\s*([A-Za-z0-9]{4,32})", re.I)

_VCODE_KW = re.compile(
    r"(?:验证码|校验码|动态码|动态密码|短信码|一次性代码|"
    r"verification\s*code|security\s*code)"
    r"\s*(?:是|为|[:：])?\s*([0-9]{4,8})(?![\d])", re.I)
_VCODE_STANDALONE = re.compile(r"(?m)^[ \t]*([0-9]{4,8})[ \t]*$")
_VCODE_CTX = re.compile(r"验证码|校验码|动态码|动态密码|verification\s*code", re.I)

_STUID_KW = re.compile(r"(?:学\s*号|student\s*id|学籍号)\s*[:：]?\s*([A-Za-z0-9]{4,16})", re.I)

_QQ_KW = re.compile(r"(?:QQ|qq|扣扣)\s*(?:号|号码|账号)?\s*[:：]?\s*([0-9]{5,12})")
_WECHAT_KW = re.compile(
    r"(?:微信|WeChat|weixin|WX|VX)\s*(?:号|号码|账号)?\s*[:：]\s*([A-Za-z][\w\-. ]{3,29})")
_ADDR_KW = re.compile(r"(?:收货地址|联系地址|家庭住址|住址|地址)\s*[:：]\s*([^\n]{4,60})")
_POSTCODE = re.compile(r"(?<![\dA-Za-z.])\d{6}(?![\dA-Za-z])")
# 邮编要"强上下文"才敢打：正文里 6 位数字太常见，实测踩过 123456.SH（科创板股票代码）
_POSTCODE_CTX_HARD = re.compile(r"邮编|邮政编码|CHN|中国|address", re.I)
_POSTCODE_CTX_SOFT = re.compile(r"省|市|区|县|街道|镇|路")
_POSTCODE_BAD_AFTER = re.compile(r"\s*\.\s*(?:SH|SZ|HK|BJ|SS|OF|US|CN)\b", re.I)

# 通用 "XX大学/XX学院"：靠"词尾锚点 + 向左收字 + 砍功能前缀 + 合并重叠"实现
# 左边界排除 科：中国科学院/社会科学院 不是学校（"中国科学院大学"仍会被正确命中）
# 右边界排除 生校士…：大学生/大学校/学院士 不是校名
_SCHOOL_TAIL = re.compile(r"(?<!科)(?:大学|学院)")
_SCHOOL_BAD_FOLLOW = set("生校士者长部区潮友刊报历风习费子员路街巷号奖派问术前中后内外")
_SCHOOL_MAX_LEFT = 10
_STOP_PREFIX = ["我是", "我在", "就读于", "毕业于", "本人", "该校", "贵校", "本校",
                "学校", "目前", "现在", "我们", "咱们", "于", "在", "是", "的", "了",
                "我", "您", "你", "和", "与", "对", "把", "被", "这", "那", "其",
                "该", "此", "为", "有", "到", "向", "给", "请", "将", "及", "等",
                "并", "或", "从", "以", "就", "也", "都", "还", "另", "如", "若"]


def _is_cjk(ch):
    return "\u3400" <= ch <= "\u9fff"


def _mask_schools(text, stats):
    spans = []
    for m in _SCHOOL_TAIL.finditer(text):
        end = m.end()
        if end < len(text) and text[end] in _SCHOOL_BAD_FOLLOW:
            continue
        i = m.start()
        j = i
        lim = max(0, i - _SCHOOL_MAX_LEFT)
        while j > lim and _is_cjk(text[j - 1]):
            j -= 1
        cand_start = j
        # 砍掉左侧功能字前缀
        seg = text[j:i]
        for sp in _STOP_PREFIX:
            if seg.startswith(sp) and len(seg) > len(sp):
                j += len(sp)
                seg = seg[len(sp):]
                break
        if not seg:
            j = cand_start
        spans.append((j, end))
    if not spans:
        return text
    # 合并重叠/相邻
    spans.sort()
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    for s, e in reversed(merged):
        raw = text[s:e]
        stats["hits"].append({"rule": "school", "label": LABEL["school"], "raw": raw,
                              "repl": MASK["school"],
                              "context": text[max(0, s - 20):e + 20].replace("\n", "\u23CE")})
    for s, e in reversed(merged):
        text = text[:s] + MASK["school"] + text[e:]
    return text


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _luhn(s):
    tot, alt = 0, False
    for ch in reversed(s):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        tot += d
        alt = not alt
    return tot % 10 == 0


def _idcard_ok(s):
    y, mo, d = int(s[6:10]), int(s[10:12]), int(s[12:14])
    return 1900 <= y <= 2026 and 1 <= mo <= 12 and 1 <= d <= 31


def _idcard_checksum_ok(s):
    w = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    tbl = "10X98765432"
    try:
        return tbl[sum(int(s[i]) * w[i] for i in range(17)) % 11] == s[17].upper()
    except Exception:
        return False


def _hit(rule, raw, repl, text, m, tag=""):
    a, b = max(0, m.start() - 24), min(len(text), m.end() + 24)
    return {"rule": rule, "label": LABEL.get(rule, rule), "raw": raw, "repl": repl,
            "tag": tag, "context": text[a:b].replace("\n", "\u23CE")}


def _sub(text, rx, rule, repl, stats, group=0, guard=None):
    def rep(m):
        val = m.group(group)
        if guard and not guard(val):
            return m.group(0)
        stats["hits"].append(_hit(rule, val, repl, text, m))
        if group == 0:
            return repl
        s, e = m.span(group)
        return m.group(0)[:s - m.start()] + repl + m.group(0)[e - m.start():]
    return rx.sub(rep, text)


def _sub_literal(text, lit, rule, stats):
    if not lit or lit not in text:
        return text
    rx = (re.compile(r"(?<![\dA-Za-z])" + re.escape(str(lit)) + r"(?![\dA-Za-z])")
          if re.fullmatch(r"[0-9]+", str(lit)) else re.compile(re.escape(str(lit))))
    return _sub(text, rx, rule, MASK[rule], stats)


def _sub_soft(text, lit, rule, stats, ctx_rx, window=25):
    """弱标识（如"某某市"这种城市名）只在地址上下文附近才打，避免误伤公司名。"""
    if not lit or lit not in text:
        return text
    rx = re.compile(re.escape(lit))

    def rep(m):
        a, b = max(0, m.start() - window), min(len(text), m.end() + window)
        if not ctx_rx.search(text[a:b]):
            return m.group(0)
        stats["hits"].append(_hit(rule, lit, MASK[rule], text, m, tag="弱"))
        return MASK[rule]
    return rx.sub(rep, text)


# 弱地址上下文：刻意不含"市/区/县" —— 公司名里满地都是（"某某市XX有限公司"）
_ADDR_SOFT_CTX = re.compile(r"地址|住址|街道|镇|村|CHN|邮编|省|中国")


def _sub_postcode(text, stats):
    """邮编：必须有硬上下文(邮编/CHN/中国)，或紧邻软上下文(省市区街道镇路)，
       且后面不能跟 .SH/.SZ 这类股票/域名后缀。"""
    def rep(m):
        if _POSTCODE_BAD_AFTER.match(text[m.end():m.end() + 6]):
            return m.group(0)
        a, b = max(0, m.start() - 40), min(len(text), m.end() + 40)
        hard = _POSTCODE_CTX_HARD.search(text[a:b])
        c, d = max(0, m.start() - 12), min(len(text), m.end() + 12)
        soft = _POSTCODE_CTX_SOFT.search(text[c:d])
        if not (hard or soft):
            return m.group(0)
        stats["hits"].append(_hit("postcode", m.group(0), MASK["postcode"], text, m,
                                  tag="硬" if hard else "软"))
        return MASK["postcode"]
    return _POSTCODE.sub(rep, text)


def _sub_vcode_standalone(text, stats):
    """只有正文里确实提到"验证码"时，才把附近的独立数字行当验证码打掉。"""
    if not _VCODE_CTX.search(text):
        return text
    zones = [(m.start(), m.end()) for m in _VCODE_CTX.finditer(text)]

    def rep(m):
        s, e = m.start(1), m.end(1)
        if not any(abs(s - zs) <= 300 or abs(s - ze) <= 300 for zs, ze in zones):
            return m.group(0)
        stats["hits"].append(_hit("vcode", m.group(1), MASK["vcode"], text, m, tag="独立行"))
        return MASK["vcode"]
    return _VCODE_STANDALONE.sub(rep, text)


# --------------------------------------------------------------------------
# 主函数
# --------------------------------------------------------------------------
def mask_text(text: str, self_name: str = "") -> tuple:
    """返回 (脱敏后文本, 替换统计)。

    self_name 临时覆盖身份配置里的姓名；其余身份项用 set_identity() 配置。
    """
    if text is None:
        text = ""
    saved_name = _IDENT["name"]
    if self_name:
        _IDENT["name"] = self_name
    try:
        stats = {"hits": [], "protected": {}, "input_len": len(text),
                 "output_len": 0, "checksum_ok": 0}
        for k in ("URL", "金额", "日期", "时间", "订单号", "对方邮箱"):
            stats["protected"][k] = 0
        orig = text

        # 0) 外科手术式归一化
        #    a. 统一换行 —— 邮件正文是 CRLF，不统一的话 "^数字$" 这类行锚点全部失效
        #       （实测 inbox/589 的验证码 221593 就是这样漏掉的）
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        #    b. 全角数字 -> 半角（全角冒号绝不能动）
        text = text.translate(_FW)
        #    c. 去零宽字符与 NBSP
        for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
            text = text.replace(ch, "")
        text = text.replace("\xa0", " ")

        # 1) 保护
        text, box = _protect(text, stats)

        # 2) 长 -> 短
        def idc_guard(v):
            if not _idcard_ok(v):
                return False
            if _idcard_checksum_ok(v):
                stats["checksum_ok"] += 1
            return True
        text = _sub(text, _IDCARD_BARE, "idcard", MASK["idcard"], stats, guard=idc_guard)
        text = _sub(text, _BANK_BARE, "bankcard", MASK["bankcard"], stats,
                    guard=lambda v: _luhn(v))
        text = _sub(text, _BANK_KW, "bankcard", MASK["bankcard"], stats, group=1)
        if _IDENT["strict_orders"]:
            text = _sub(text, re.compile(r"(?<![\dA-Za-z])\d{12,}(?![\dA-Za-z])"),
                        "token", MASK["token"], stats)
        text = _sub(text, _PASSCODE_KW, "passcode", MASK["passcode"], stats, group=1)
        text = _sub(text, _VCODE_KW, "vcode", MASK["vcode"], stats, group=1)
        text = _sub_vcode_standalone(text, stats)
        text = _sub(text, _MASKED_PHONE, "phone", MASK["phone"], stats)
        text = _sub(text, _PHONE_BARE, "phone", MASK["phone"], stats)
        for p in _IDENT["phones"]:
            text = _sub_literal(text, p, "phone", stats)
        text = _sub(text, _STUID_KW, "stu_id", MASK["stu_id"], stats, group=1)
        for s in _IDENT["stu_ids"]:
            text = _sub_literal(text, s, "stu_id", stats)
        text = _sub(text, _QQ_KW, "qq", MASK["qq"], stats, group=1)
        for q in _IDENT["qq"]:
            text = _sub_literal(text, q, "qq", stats)
        text = _sub(text, _WECHAT_KW, "wechat", MASK["wechat"], stats, group=1)
        for w in _IDENT["wechats"]:
            text = _sub_literal(text, w, "wechat", stats)
        if _IDENT["name"]:
            pat = r"\s*".join(re.escape(c) for c in _IDENT["name"])
            text = _sub(text, re.compile(pat), "name", MASK["name"], stats)
        # 别名要按长度降序，否则 "SanZhang" 会先把 "SanZhang2027" 打掉一半只剩 "1666"
        for a in sorted(_IDENT["name_aliases"], key=len, reverse=True):
            if a and len(a) >= 3:
                text = _sub(text, re.compile(
                    r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])", re.I),
                    "name", MASK["name"], stats)
        for s in _IDENT["schools"]:
            text = _sub_literal(text, s, "school", stats)
        if _IDENT["generic_school"]:
            text = _mask_schools(text, stats)
        for mj in _IDENT["majors"]:
            text = _sub_literal(text, mj, "major", stats)
        if _IDENT["generic_address"]:
            text = _sub(text, _ADDR_KW, "address", MASK["address"], stats, group=1)
        for a in _IDENT["addresses"]:
            text = _sub_literal(text, a, "address", stats)
        for a in _IDENT["addresses_soft"]:
            text = _sub_soft(text, a, "address", stats, _ADDR_SOFT_CTX)
        text = _sub_postcode(text, stats)

        # 3) 还原哨兵
        def back(m):
            i = ord(m.group(1)) - 0xE100
            return box[i] if 0 <= i < len(box) else m.group(0)
        text = _SENT_RE.sub(back, text)

        cnt = {}
        for h in stats["hits"]:
            cnt[h["label"]] = cnt.get(h["label"], 0) + 1
        stats["by_rule"] = cnt
        stats["total"] = len(stats["hits"])
        stats["changed"] = text != orig
        stats["output_len"] = len(text)
        return text, stats
    finally:
        _IDENT["name"] = saved_name


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    # 身份来自配置（$MASK_IDENTITY / identity.json），读不到才用内联示例值。
    load_identity_file()
    cases = [
        "面试时间：2026-09-22 17:51 至 2026-09-25 17:51，手机 13800138000",
        "身份证 110101199001011234",
        "通行证： 12345678901234",
        "验证码 8888",
        "金额 3,842.00 元",
        "订单号 20260925123456",
        "张三同学，请张 三 本人确认。",
        "+86 13800138000 / your-wechat-id",
        "Dear San, 学号：2027000001",
        "欢迎报考某某大学。中国大学生服务外包创新创业大赛",
        "https://recruit.xingchen.example.com/eap/#/assessment?candidate=a-00000000-0000-0000-0000-000000000000",
        "https://yuntu.example.com/pc?aId=AbCdEfGhIjKlMnOpQrStUv==",
        "104992元",
    ]
    for s in cases:
        out, st = mask_text(s, self_name="张三")
        print("IN : %s" % s)
        print("OUT: %s" % out)
        print("     %s\n" % st["by_rule"])
