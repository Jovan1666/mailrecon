#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书 API 扩展层：只读拉会话历史 / 读 reaction / 原地更新卡片。

出站安全校验（https + 只允许飞书官方域名 + 反内网解析 + 不跟随 302）
直接复用 /root/mail-agent/feishu.py，不修改那边任何文件。
"""
import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/root/mail-agent")
from feishu import (HERMES_ENV, OPENER, USER_AGENT,  # noqa: E402
                    _assert_official, _load_env, _token_and_chat)

API = "https://open.feishu.cn/open-apis"
MSG_LIST = API + "/im/v1/messages"
MSG_ONE = API + "/im/v1/messages/%s"
REACTIONS = API + "/im/v1/messages/%s/reactions"


def allowed_user():
    """只接受这一个 open_id 发来的指令（和 Hermes 的 FEISHU_ALLOWED_USERS 对齐）。"""
    v = _load_env(HERMES_ENV).get("FEISHU_ALLOWED_USERS") or ""
    return [x.strip() for x in v.split(",") if x.strip()]


def _req(method, url, body=None, tok=None):
    _assert_official(url)
    tok = tok or token()          # 调用方忘了传 token 也不会发出空 Bearer 请求
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + (tok or ""),
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json; charset=utf-8",
    })
    try:
        with OPENER.open(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"code": e.code, "msg": "HTTP %s" % e.code}
    except Exception as e:
        return {"code": -1, "msg": repr(e)}


_TOK = {"v": None, "exp": 0.0}


def token():
    """tenant_access_token，带 20 分钟缓存（官方有效期 2h，留足余量）。"""
    now = time.time()
    if _TOK["v"] and now < _TOK["exp"]:
        return _TOK["v"]
    t = _token_and_chat()[0]
    _TOK.update(v=t, exp=now + 1200)
    return t


def chat_id():
    return _token_and_chat()[1]


def list_messages(chat, start_s, end_s, page_token=None, page_size=50,
                  tok=None, sort="ByCreateTimeAsc"):
    """拉会话历史消息（只读）。需要 im:message 权限，应用在会话里即可。"""
    url = ("%s?container_id_type=chat&container_id=%s&start_time=%d"
           "&end_time=%d&page_size=%d&sort_type=%s"
           % (MSG_LIST, chat, int(start_s), int(end_s), page_size, sort))
    if page_token:
        url += "&page_token=" + page_token
    return _req("GET", url, tok=tok)


def get_message(message_id, tok=None):
    return _req("GET", MSG_ONE % message_id, tok=tok)


def get_reactions(message_id, tok=None):
    """读某条消息上的表情回复。需要 im:message.reactions:readonly。"""
    return _req("GET", (REACTIONS % message_id) + "?page_size=50", tok=tok)


def patch_card(message_id, card, tok=None):
    """原地更新应用自己发出的交互卡片 —— 不产生新消息、不会刷屏。"""
    return _req("PATCH", MSG_ONE % message_id,
                {"content": json.dumps(card, ensure_ascii=False)}, tok=tok)


def send_card_to(chat, card, tok=None):
    return _req("POST", MSG_LIST + "?receive_id_type=chat_id",
                {"receive_id": chat, "msg_type": "interactive",
                 "content": json.dumps(card, ensure_ascii=False)}, tok=tok)


def send_text_to(chat, text, tok=None):
    return _req("POST", MSG_LIST + "?receive_id_type=chat_id",
                {"receive_id": chat, "msg_type": "text",
                 "content": json.dumps({"text": text}, ensure_ascii=False)}, tok=tok)


def iter_all_messages(chat, start_s, end_s, tok=None, max_pages=20):
    """翻页拉全量（只读）。"""
    out, page, pt = [], 0, None
    while page < max_pages:
        d = list_messages(chat, start_s, end_s, page_token=pt, tok=tok)
        if d.get("code") != 0:
            return out, d
        data = d.get("data") or {}
        out.extend(data.get("items") or [])
        pt = data.get("page_token")
        page += 1
        if not data.get("has_more"):
            break
    return out, {"code": 0, "msg": "ok"}


if __name__ == "__main__":
    t = token()
    c = chat_id()
    print("allowed_user =", allowed_user())
    print("chat_id      =", c)
    import time
    now = int(time.time())
    msgs, err = iter_all_messages(c, now - 604800, now, tok=t)
    print("list_messages:", err.get("code"), "->", len(msgs), "messages")
    card = [m for m in msgs if m.get("msg_type") == "interactive"]
    if card:
        r = get_reactions(card[-1]["message_id"], tok=t)
        print("get_reactions:", r.get("code"), r.get("msg"))
