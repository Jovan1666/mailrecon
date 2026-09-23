#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""root 直接调飞书 API 发消息（文本或卡片）。

为什么不用 `hermes send`：那条路径是 root -> `su - hermes -c ...`，
而 `su - user -c` 会执行该用户的 ~/.bashrc。如果 hermes 用户的家目录被写入恶意内容
（例如 AI 读了恶意邮件后被劫持），root 每次推送就会执行一次，等于提权。
这里改成 root 用飞书凭据直接调 API，完全不经过 hermes 用户的可写路径。

出站目标写死为飞书官方域名（字面量），不做任何动态拼接。
"""
import json
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

HERMES_ENV = "/home/hermes/.hermes/.env"

# 只允许这两个飞书官方端点（字面量，不拼接）
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
ALLOWED_HOSTS = ("open.feishu.cn", "open.larksuite.com")
USER_AGENT = "mail-agent/1.0"

# 不跟随重定向，避免被 302 带到别处
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def _load_env(path):
    cfg = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def sanitize(s, limit=200):
    """清掉控制字符和换行，防止邮件主题里的内容把排版搞乱。"""
    s = (s or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\u2028\u2029\ufeff]", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s[:limit]


def _assert_official(url):
    """出站前校验：必须 https + 飞书官方域名 + 解析后不是内网地址。"""
    p = urlparse(url)
    if p.scheme != "https":
        raise ValueError("只允许 https")
    if p.hostname not in ALLOWED_HOSTS:
        raise ValueError("非飞书官方域名: %s" % p.hostname)
    for info in socket.getaddrinfo(p.hostname, 443, proto=socket.IPPROTO_TCP):
        ip = info[4][0]
        if ip.startswith(("127.", "10.", "192.168.", "169.254.")) or ip.startswith("172."):
            raise ValueError("解析到内网地址，拒绝: %s" % ip)


def _token_and_chat():
    cfg = _load_env(HERMES_ENV)
    app_id = cfg.get("FEISHU_APP_ID")
    app_secret = cfg.get("FEISHU_APP_SECRET")
    chat = cfg.get("FEISHU_HOME_CHANNEL")
    if not (app_id and app_secret and chat):
        raise RuntimeError("飞书配置不全")
    _assert_official(TOKEN_URL)
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with OPENER.open(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8"))
    if d.get("code") != 0:
        raise RuntimeError("取 token 失败: %s" % d.get("msg"))
    return d["tenant_access_token"], chat


def _post(msg_type, content_text):
    """content_text 是已经序列化好的内容字符串。"""
    try:
        token, chat = _token_and_chat()
        _assert_official(MSG_URL)
        payload = json.dumps({
            "receive_id": chat,
            "msg_type": msg_type,
            "content": content_text,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            MSG_URL, data=payload,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": "Bearer " + token,
                     "User-Agent": USER_AGENT})
        with OPENER.open(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("code") != 0:
            return False, "发送失败: %s" % d.get("msg")
        return True, "ok"
    except urllib.error.HTTPError as e:
        return False, "HTTP %s: %s" % (e.code, e.read()[:200])
    except Exception as e:
        return False, "%r" % e


def send(text):
    """发纯文本。"""
    return _post("text", json.dumps({"text": text}, ensure_ascii=False))


def send_card(card):
    """发交互卡片（更美观：有标题、分隔线、按钮）。card 是 dict。"""
    return _post("interactive", json.dumps(card, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    ok, msg = send(sys.argv[1] if len(sys.argv) > 1 else "【测试】root 直调飞书 API")
    print("成功" if ok else "失败", msg)
