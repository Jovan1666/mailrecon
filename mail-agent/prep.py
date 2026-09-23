#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""送模型前的完整预处理：剥 HTML -> 去引文 -> 去免责声明 -> 脱敏 -> 截断。

接口（任务书要求）：
    prepare_for_llm(raw_html, raw_text, subject, from_addr, self_name="") -> (str, dict)

实测依据（30 封真实邮件）：
  * 剥掉 HTML 与实体后，正文里"引文"和"免责声明"占了绝大部分体积，
    682/658 这类真人回复，新的内容只有 2~3 行，其余全是用户自己那封求职信的回引
    和一段 200 字的中英双语保密公告。
  * 313 / sent-15 / sent-21 / sent-32 这些邮件正文根本没有内容（只有附件或
    "从QQ邮箱发来的超大附件"的下载链接），必须判为不可用，直接不发模型。

不做的事：不猜测、不补全、不改写正文语义；除了四项归一化（CRLF、全角数字、
零宽字符、NBSP）之外，原文一字不改。
"""

import email
import email.header
import email.utils
import html as _html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mask as MK  # noqa: E402

# --------------------------------------------------------------------------
# 身份：显式配置，绝不去猜
# --------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_FILE = os.environ.get("MASK_IDENTITY") or os.path.join(_HERE, "identity.json")
if not os.path.exists(IDENTITY_FILE):
    # 兜底：开发期放在 /tmp 的那份。生产环境不该走到这里。
    IDENTITY_FILE = "/tmp/maskwork/identity.json"

DEFAULT_IDENTITY = {
    "name": "",
    "name_aliases": [],
    "emails": [],
    "qq": [],
    "phones": [],
    "schools": [],
    "majors": [],
    "stu_ids": [],
    "wechats": [],
    "addresses": [],
    "addresses_soft": [],
    "generic_school": True,
    "generic_address": True,
    "strict_orders": False,
}


def load_identity(path=IDENTITY_FILE):
    """从 JSON 载入身份白名单；文件不存在就返回默认值（不报错）。"""
    cfg = dict(DEFAULT_IDENTITY)
    try:
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if not cfg["emails"]:
        cfg["emails"] = _mailboxes_from_env()
    MK.set_identity(**{k: v for k, v in cfg.items() if k in MK._IDENT})
    return cfg


def _mailboxes_from_env(path="/root/mail-agent/.env"):
    out = []
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("QQ_EMAIL="):
                v = line.split("=", 1)[1].strip()
                if v:
                    out.append(v)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# 1) 剥 HTML
# --------------------------------------------------------------------------
_RE_SCRIPT = re.compile(r"(?is)<(script|style|head|title|noscript)[^>]*>.*?</\1\s*>")
_RE_COMMENT = re.compile(r"(?s)<!--.*?-->")
_RE_COND = re.compile(r"(?is)<!\[if.*?<!\[endif\]\s*>")
_RE_BLOCK = re.compile(r"(?i)<(br|/p|/div|/tr|/li|/h[1-6]|/table|/blockquote|hr)\s*/?>")
_RE_TD = re.compile(r"(?i)</t[dh]\s*>")
_RE_TAG = re.compile(r"(?s)<[^>]{0,2000}?>")


def strip_html(h):
    if not h:
        return ""
    h = _RE_COMMENT.sub(" ", h)
    h = _RE_COND.sub(" ", h)
    h = _RE_SCRIPT.sub(" ", h)
    h = _RE_BLOCK.sub("\n", h)
    h = _RE_TD.sub("\t", h)
    h = _RE_TAG.sub(" ", h)
    h = _html.unescape(h)
    h = h.replace("\u00a0", " ").replace("&nbsp;", " ")
    return h


def clean_ws(s):
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t\u3000]{2,}", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# --------------------------------------------------------------------------
# 2) 去引文
# --------------------------------------------------------------------------
QUOTE_MARKERS = [
    "(?im)^[ \\t]*[-_=]{2,}\\s*(?:原始邮件|原始郵件|转发邮件|Original\\s+Message|"
    "Forwarded\\s+message)\\s*[-_=]*",
    "(?im)^[ \\t]*[-_=]{4,}[ \\t]*(?:Original|原始邮件|转发)[ \\t]*[-_=]*[ \\t]*$",
    "(?im)^[ \\t]*在[^\\n]{0,60}写道[:：]",
    "(?im)^[ \\t]*On[^\\n]{0,80}\\bwrote:",
    "(?im)^[ \\t]*(?:发件人|寄件人|发信人)[:：]",
    "(?im)^[ \\t]*-{2,}\\s*(?:原始邮件|Original)[^\\n]*$",
    "(?im)^[ \\t]*From:[ \\t]",
    "(?m)^[ \\t]*>{1,}[ \\t]?",
]
_QUOTE_RX = [re.compile(p) for p in QUOTE_MARKERS]

# "From:" 单看太弱（正文里也可能出现英文 From），要求它后面 6 行里出现头部字段
_HEADER_HINT = re.compile(
    r"(?im)^[ \t]*(?:Sent|To|Cc|Subject|Date|时间|主题|收件人|抄送)[:：]")


def cut_quote(t):
    """返回 (新内容, 被砍掉多少字)。取最早出现的引文标记截断。"""
    best = None
    for rx in _QUOTE_RX:
        for m in rx.finditer(t):
            s = m.start()
            if s < 20:                     # 开头就是引文标记，多半不是引文
                continue
            if rx.pattern.startswith("(?im)^[ \\t]*From"):
                if not (_HEADER_HINT.search(t, m.end(), m.end() + 400)
                        or "mailto:" in t[m.start():m.start() + 200]):
                    continue
            if best is None or s < best:
                best = s
            break
    if best is None:
        return t, 0
    return t[:best].rstrip(), len(t) - best


# --------------------------------------------------------------------------
# 3) 去免责声明 / 页脚
# --------------------------------------------------------------------------
FOOTER_MARKERS = [
    r"(?im)^[ \t]*保密公告",
    r"(?i)CONFIDENTIALITY\s+NOTICE",
    r"(?i)本邮件含有保密信息",
    r"(?i)声明[:：][ \t]*本邮件",
    r"(?i)此邮件由企业操作",
    r"(?i)本邮件由系统自动发出",
    r"(?i)This is an automated email",
    r"(?i)This message was sent to\b",
    r"(?im)^[ \t]*(?:点击这里取消订阅|取消订阅|退订|unsubscribe)",
    r"(?i)保留所有权利|All [Rr]ights [Rr]eserved",
    r"(?im)^[ \t]*(?:/n)?\s*©",
    r"(?im)^[ \t]*\*{8,}[ \t]*$",
    r"(?i)Please do not reply|请勿直接回复|请勿回复",
]
_FOOTER_RX = [re.compile(p) for p in FOOTER_MARKERS]


def cut_footer(t):
    """页脚只砍后 60% 区域里的第一个标记，避免把正文中段误判成页脚。"""
    n = len(t)
    best = None
    for rx in _FOOTER_RX:
        for m in rx.finditer(t):
            if m.start() < 0.4 * n:
                continue
            if best is None or m.start() < best:
                best = m.start()
            break
    if best is None:
        return t, 0
    return t[:best].rstrip(), n - best


# --------------------------------------------------------------------------
# 4) 清噪
# --------------------------------------------------------------------------
_RE_IMGPH = re.compile(r"(?i)\[(?:image|cid|图片)\s*:[^\]]{0,300}\]")
_NOISE_LINE = re.compile(r"^[\s.·•\-_=*>|>]+$")


def denoise(t):
    t = _RE_IMGPH.sub("", t)
    out = []
    for ln in t.split("\n"):
        s = ln.strip()
        if not s:
            out.append("")
            continue
        if _NOISE_LINE.match(s):
            continue
        if s in ("&nbsp;", "&nbsp；"):
            continue
        out.append(ln.rstrip())
    t = "\n".join(out)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# --------------------------------------------------------------------------
# 5) 不可用正文识别
# --------------------------------------------------------------------------
NOTHING_BODY = re.compile(r"从QQ邮箱发来的超大附件|进入下载页面")
_URL_RX = re.compile(r"https?://\S+")
_MAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _informative(s):
    """只留"文字信息"：去掉 URL、邮箱、数字、空白、标点。
       用来判断这封邮件到底有没有可判断的内容。"""
    s = _URL_RX.sub("", s)
    s = _MAIL_RX.sub("", s)
    return re.sub(r"[\d\s\W_]+", "", s)


def usable_body(t, raw_len):
    """正文是不是"没有内容"（只有附件下载链接、只有签名、只有一句署名）。"""
    if NOTHING_BODY.search(t):
        return False, "只有 QQ 超大附件下载链接"
    core = _informative(t)
    if len(core) < 8:
        return False, "正文只剩链接/附件/签名，无有效文字"
    return True, ""


# --------------------------------------------------------------------------
# 6) 截断
# --------------------------------------------------------------------------
TRUNC = "\n\n…[中间省略 %d 字]…\n\n"


def truncate(t, max_chars=2000):
    if max_chars <= 0 or len(t) <= max_chars:
        return t, 0
    keep_head = int(max_chars * 0.65)
    keep_tail = max_chars - keep_head
    cut = len(t) - keep_head - keep_tail
    return t[:keep_head] + (TRUNC % cut) + t[-keep_tail:], cut


def pick_body(raw_text, raw_html):
    """取正文：优先 text/plain；它太短或压根没有时用剥过的 HTML。

    注意：真实样本里 159 的 text/plain 部分本身就带 `&nbsp;` 和 HTML 实体，
    所以 text 也要过一遍 strip_html —— 这一步顺带把它洗干净了。
    实测 720/641/672/546/664 是纯 HTML 邮件（text 部分为空），必须走 HTML 分支。
    """
    plain = strip_html(raw_text or "")
    if len(re.sub(r"\s", "", plain)) < 40 and raw_html:
        return clean_ws(strip_html(raw_html)), True
    return clean_ws(plain), False


# --------------------------------------------------------------------------
# 主函数
# --------------------------------------------------------------------------
_ID_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _from_display(addr, stats):
    a = (addr or "").strip()
    if not a:
        return "?"
    self_mails = {e.lower() for e in MK.get_identity()["emails"]}
    if a.lower() in self_mails:
        stats["self_sender"] = True
        return MK.MASK["email"]
    return a          # 对方地址保留：配对/展示都要用


def prepare_for_llm(raw_html: str, raw_text: str, subject: str, from_addr: str,
                    self_name: str = "") -> tuple:
    """剥 HTML -> 去引文 -> 去免责声明 -> 脱敏 -> 截断。返回 (待发送文本, 统计)。"""
    cfg = load_identity()
    stats = {
        "steps": {}, "usable": True, "reason": "",
        "self_sender": False, "truncated": 0, "quote_ratio": 0.0,
    }

    if self_name and self_name != cfg.get("name"):
        MK.set_identity(name=self_name)
        cfg = MK.get_identity()

    # --- 取正文：优先 text/plain，太短则剥 HTML ---
    t, used_html = pick_body(raw_text, raw_html)
    stats["steps"]["html_used"] = used_html
    stats["len_after_html"] = len(t)

    # --- 去引文 ---
    t, cutq = cut_quote(t)
    stats["steps"]["quote_cut"] = cutq
    stats["quote_ratio"] = round(cutq / max(1, stats["len_after_html"]), 3)

    # --- 去免责声明 ---
    t, cutf = cut_footer(t)
    stats["steps"]["footer_cut"] = cutf

    # --- 清噪 ---
    t = denoise(t)

    # --- 可用性 ---
    ok, why = usable_body(t, stats["len_after_html"])
    stats["usable"], stats["reason"] = ok, why
    if not ok:
        stats["len_final"] = 0
        stats["remove_ratio"] = 1.0
        stats["mask"] = {"total": 0, "by_rule": {}, "hits": [], "protected": {}}
        return "", stats

    # --- 脱敏 ---
    t, mstats = MK.mask_text(t, self_name=self_name or cfg.get("name") or "")
    stats["mask"] = mstats

    # --- 元信息头（主题也要脱敏）---
    subj_masked, _ = MK.mask_text(subject or "", self_name=self_name or cfg.get("name") or "")
    head = "[主题] %s\n[发件人] %s\n[正文]\n" % (subj_masked.strip(),
                                                _from_display(from_addr, stats))
    t = head + t

    # --- 截断 ---
    t, cut = truncate(t)
    stats["truncated"] = cut
    stats["len_final"] = len(t)
    stats["remove_ratio"] = round(1 - len(t) / max(1, stats["len_after_html"]), 3)
    return t, stats


# --------------------------------------------------------------------------
# 便于自测：把 .eml 拆成 (subject, from, text, html)
# --------------------------------------------------------------------------
def _dec_header(raw):
    try:
        out = []
        for data, cs in email.header.decode_header(raw or ""):
            if isinstance(data, bytes):
                for c in (cs, "utf-8", "gb18030"):
                    if not c:
                        continue
                    try:
                        out.append(data.decode(c))
                        break
                    except Exception:
                        continue
                else:
                    out.append(data.decode("utf-8", "replace"))
            else:
                out.append(data)
        return "".join(out).strip()
    except Exception:
        return str(raw or "")


def parse_mime(raw_bytes):
    """返回 (subject, from_addr, text, html)。仅用于本目录的自测脚本。"""
    m = email.message_from_bytes(raw_bytes)
    subj = _dec_header(m.get("Subject"))
    fa = ""
    try:
        fa = (email.utils.parseaddr(m.get("From"))[1] or "").lower()
    except Exception:
        pass
    txt, htm = [], []
    for p in m.walk():
        if p.get_content_maintype() == "multipart":
            continue
        ct = p.get_content_type()
        try:
            payload = p.get_payload(decode=True)
        except Exception:
            payload = None
        if not payload:
            continue
        cs = p.get_content_charset() or "utf-8"
        try:
            s = payload.decode(cs, "replace")
        except Exception:
            s = payload.decode("utf-8", "replace")
        (txt if ct == "text/plain" else htm if ct == "text/html" else []).append(s)
    return subj, fa, "\n".join(txt), "\n".join(htm)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    import glob
    load_identity()
    files = sorted(glob.glob("/tmp/maskwork/samples/*.eml"))
    if not files:
        print("没有样本周转；先跑 fetch.py")
        raise SystemExit(0)
    tot = 0
    print("%-16s %-6s %-7s %-7s %-6s %-6s %s" % (
        "邮件", "原始", "去HTML", "去引文", "截断", "去页脚", "可用"))
    for f in files:
        subj, fa, txt, htm = parse_mime(open(f, "rb").read())
        out, st = prepare_for_llm(htm, txt, subj, fa, self_name="张三")
        tot += 1
        print("%-16s %-6d %-7d %-7d %-6d %-6d %s  %s" % (
            os.path.basename(f).replace(".eml", ""), len(txt) + len(htm),
            st["len_after_html"], st["steps"]["quote_cut"], st["truncated"],
            st["steps"]["footer_cut"],
            "OK" if st["usable"] else "NO(" + st["reason"] + ")", ""))
