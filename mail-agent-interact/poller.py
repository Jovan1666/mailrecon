#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指令泵：把用户在飞书里说的话变成台账动作，并刷新看板。

两条入口，同一套解析、同一个 message_id 去重：
  1) 实时通道 —— Hermes 的 pre_gateway_dispatch shell hook / plugin 把「被识别为
     指令」的消息写进 /home/hermes/.hermes/state/feishu_task_cmds.jsonl（root 可读）。
     延迟 < 10s，并且回 {"action":"skip"} 让 AI 不要跟着乱答。
  2) 兜底通道 —— root 用应用身份直接读飞书会话历史（im/v1/messages，只读）。
     延迟 ≤ POLL_SECONDS。hook 没装 / 网关重启 / hook 报错时仍然能用。

拿到指令 → 落库 → 原地刷新看板 → 回一条确认（done/drop/undo 带「反向按钮」）。

「点下去不能含糊」——每一次点击都要给一句**准确**的回执，不允许静默：
  * 真改动了 / 本来就是这个状态（重复点击）/ 台账里没这个编号 → 三种分开说（见 ack）；
  * 落库失败、刷新看板失败、确认卡发不出去 → 各有各的话，并且说清"到底落库没有"；
  * 卡片回调被去重丢弃 → 日志留痕（对用户安静，因为第一次投递已经回过话了）；
  * 撤销按钮过期 / 卡片点击被闸门拒掉 → 都回一句能照着做的提示。
  唯一保持安静的是"非白名单用户"和"压根不像本系统的按钮"（回话等于给陌生人回音）。

安全（两条入口的信任锚不一样，别混）：
  - 文字消息：JSONL 里除了 message_id 什么都不信，正文一律回飞书核对
    （见 authoritative()）——因为飞书存着用户的原话，可以核对。
  - 卡片点击：飞书**不保存点击记录**，做不到同样的核对。这一路的最强证据是
    「飞书认证过的长连接载荷里的 operator（点击者 open_id）」+ 动词白名单 +
    纯数字编号 + token 去重，见 admit() 的 src=="card" 分支。
    三个动词（完成/放弃/撤销）都是可逆的，结果会立刻反映在看板上，风险可接受。
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, "/root/mail-agent")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CST, DB, sanitize  # noqa: E402
import board  # noqa: E402
import cmds  # noqa: E402
import fsapi  # noqa: E402
import infer  # noqa: E402

_DEFAULT_STATE_DIR = "/var/lib/mail-agent-interact"


def _safe_state_dir(raw):
    """状态目录允许用环境变量覆盖，但必须规范化、不含 ..，且落在允许的根下。"""
    if ".." in raw.replace("\\", "/").split("/"):
        raise SystemExit("TASK_CMD_STATE 不允许包含 ..：%r" % raw)
    d = os.path.realpath(os.path.abspath(raw))
    if not (d == _DEFAULT_STATE_DIR or d.startswith(_DEFAULT_STATE_DIR + "/")
            or d.startswith("/tmp/")):
        raise SystemExit("TASK_CMD_STATE 必须位于 %s 或 /tmp 下：%r"
                         % (_DEFAULT_STATE_DIR, raw))
    return d


STATE_DIR = _safe_state_dir(os.environ.get("TASK_CMD_STATE", _DEFAULT_STATE_DIR))
CURSOR_FILE = os.path.join(STATE_DIR, "cursor.json")
SEEN_FILE = os.path.join(STATE_DIR, "seen.json")
HOOK_FILE = os.environ.get("TASK_CMD_HOOKFILE",
                           "/home/hermes/.hermes/state/feishu_task_cmds.jsonl")
POLL_SECONDS = int(os.environ.get("TASK_CMD_POLL", "60"))
HOOK_TICK = int(os.environ.get("TASK_CMD_TICK", "10"))
LOOKBACK_DAYS = 14
SEEN_KEEP = 500
DIGEST_HOUR = 8          # 早报（本地时间）
DRY = os.environ.get("TASK_CMD_DRYRUN") == "1"   # 演练：不真的发飞书

# 卡片按钮允许的动作（只这三个，且都可逆）
CARD_OPS = ("done", "drop", "reopen")
# 撤销按钮有效期：卡片上的按钮会一直留着，过期后再点会很突兀，
# 所以给个窗口；过期后文字指令「撤销 3」永远可用（文字路没有窗口）。
UNDO_WINDOW = 1800
ACK_HEAD = {"done": "✅ 标记完成", "accept": "✅ 采纳建议", "drop": "🚫 已忽略",
            "reject": "🚫 已驳回", "reopen": "↩️ 已恢复"}
ACK_TMPL = {"done": "green", "accept": "green", "drop": "grey",
            "reject": "grey", "reopen": "blue"}


def _say(chat, text, tok):
    if DRY:
        print("  [DRY 回话] %s" % text.replace("\n", " ⏎ "))
        return {"code": 0}
    d = fsapi.send_text_to(chat, text, tok=tok)
    if d.get("code") != 0:
        _log("纯文本回话失败: %s %s" % (d.get("code"), d.get("msg")))
    return d


def _say_card(chat, card, tok):
    """发一张交互卡片（确认消息用）。"""
    if DRY:
        print("  [DRY 回话卡] %s" % json.dumps(card, ensure_ascii=False)[:260])
        return {"code": 0}
    d = fsapi.send_card_to(chat, card, tok=tok)
    if d.get("code") != 0:
        _log("确认卡片发送失败: %s %s" % (d.get("code"), d.get("msg")))
    return d


def _deliver(chat, card, text, tok):
    """确认消息必须送到用户眼前：卡片发不出去就退纯文本。

    为什么较真：点完按钮什么都没收到，用户分不清"我没点中"还是"系统坏了"——
    这是最糟的一种静默（用户原话："点下去不能含糊"）。
    """
    d = _say_card(chat, card, tok)
    if d.get("code") == 0:
        return True
    _log("确认卡片发失败（%s），退回纯文本再发一次" % d.get("msg"))
    d2 = _say(chat, text, tok)
    return d2.get("code") == 0


def _brief(e, limit=60):
    return sanitize("%s: %s" % (type(e).__name__, e), limit)


def _publish(db, tok, new=False):
    if DRY:
        rows = board.load(db)
        print("  [DRY 看板] %s" % json.dumps(board.render(rows), ensure_ascii=False)[:160])
        return True, "DRY"
    return board.publish(db, new=new, tok=tok)


# ----------------------------------------------------------------- 小工具

def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


_SAFE_BASE = os.path.realpath(STATE_DIR)


def _save(path, obj):
    """原子写状态文件。

    这份代码里唯一的调用点是 CURSOR_FILE（由已校验的 STATE_DIR 拼出），
    但仍然在写入点再断言一次：目标必须落在状态目录内，否则直接拒绝。
    """
    rp = os.path.realpath(os.path.abspath(path))
    if not (rp == _SAFE_BASE or rp.startswith(_SAFE_BASE + os.sep)):
        raise SystemExit("拒绝写入状态目录之外的文件：%r" % path)
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    tmp = rp + ".tmp"
    fh = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fh, json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(fh)
    os.replace(tmp, rp)


def _log(msg):
    line = "%s %s" % (datetime.now(CST).strftime("%F %T"), msg)
    print(line, flush=True)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "poller.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------- 指令来源

def authoritative(mid, tok):
    """按 message_id 去飞书取回原文 —— 文字消息**唯一可信**的来源。

    为什么必须这么做：hook 那个 JSONL 是 hermes 用户写的，hermes 跟 AI 同权限，
    被劫持时可以写任意内容。所以那份文件里除了 message_id，别的字段一个都不信；
    拿着 id 回飞书查一次，发送者/消息类型/正文全部以飞书返回的为准。
    伪造的 id 查不到 → 丢弃（历史兜底通道随后也不会认它）。
    """
    if not mid:
        return None
    d = fsapi.get_message(mid, tok=tok)
    if d.get("code") != 0:
        return None
    items = (d.get("data") or {}).get("items") or []
    if not items:
        return None
    m = items[0]
    sender = m.get("sender") or {}
    if sender.get("sender_type") != "user":       # 只认真人发的
        return None
    if m.get("msg_type") != "text":
        return None
    try:
        text = json.loads((m.get("body") or {}).get("content") or "{}").get("text", "")
    except Exception:
        return None
    return (text[:200], m.get("message_id") or mid, str(sender.get("id") or "")[:64])


VERIFY = authoritative          # 测试可替换


def from_hook_file(state):
    """消费 Hermes 写的 JSONL。返回 dict 列表。

    字段：text / mid / uid / ts / src(text|card) / operator / card_mid / btn_t
    除了 message_id，其它都当**不可信**（src/operator 只用于分流和审计）。
    """
    out = []
    off = state.get("hook_offset", 0)
    try:
        size = os.path.getsize(HOOK_FILE)
    except OSError:
        return out
    if size < off:                      # 文件被截断/轮转
        off = 0
    if size == off:
        return out
    try:
        with open(HOOK_FILE, encoding="utf-8", errors="replace") as f:
            f.seek(off)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue          # 坏行直接跳过，不猜
                if not isinstance(d, dict):
                    continue
                text = d.get("text")
                mid = d.get("message_id")
                if not (isinstance(text, str) and isinstance(mid, str)):
                    continue
                bt = d.get("btn_t")
                out.append({
                    "text": text[:200],
                    "mid": mid[:120],
                    "uid": str(d.get("user_id") or "")[:64],
                    "ts": float(d.get("ts") or 0),
                    "src": str(d.get("src") or "text")[:8],
                    "operator": str(d.get("operator") or "")[:64],
                    "card_mid": str(d.get("card_mid") or "")[:120],
                    "btn_t": int(bt) if isinstance(bt, int) and not isinstance(bt, bool)
                    else (int(bt) if isinstance(bt, str) and bt.isdigit() else None),
                })
            new_off = f.tell()
    except OSError:
        return out
    state["hook_offset"] = new_off
    return out


def from_history(state, tok, chat):
    """只读拉会话历史里的用户消息。返回 [(text, message_id, user_id, ts)]"""
    now = time.time()
    # 只从上次水位往前 5 分钟开始拉，别每次翻 14 天（省接口调用 + 少翻页）
    start = max(float(state.get("history_cursor") or 0) - 300, now - 86400)
    msgs, err = fsapi.iter_all_messages(chat, int(start), int(now) + 60, tok=tok, max_pages=4)
    if err.get("code") != 0:
        _log("拉历史失败: %s %s" % (err.get("code"), err.get("msg")))
        return []
    out = []
    for m in msgs:
        if (m.get("sender") or {}).get("sender_type") != "user":
            continue
        mid = m.get("message_id") or ""
        ct = int(m.get("create_time") or 0) / 1000.0
        if ct <= float(state.get("history_cursor") or 0):
            continue
        if m.get("msg_type") != "text":
            continue
        try:
            text = json.loads((m.get("body") or {}).get("content") or "{}").get("text", "")
        except Exception:
            continue
        out.append((text[:200], mid, str((m.get("sender") or {}).get("id") or "")[:64], ct))
    if out:
        state["history_cursor"] = max(x[3] for x in out)
    return out


def admit(rec, tok, allow):
    """把关：一条 JSONL 记录能不能变成一次操作。返回 (text, mid, uid, via, btn_t) 或 None。"""
    if rec.get("src") == "card":
        # —— 卡片按钮：飞书不存点击记录，做不到回飞书核对。
        #    能拿到的最强证据 = 飞书认证过的载荷里的 user_id（点击者）+ 下面的白名单/白名单动词/纯数字编号。
        p = cmds.parse(rec["text"])
        if not p or p["op"] not in CARD_OPS or not p["ids"] or p["ids"] == ["all"]:
            _log("卡片指令不合法，忽略: %r" % rec["text"][:60])
            return None
        if allow and rec["uid"] not in allow:
            _log("卡片点击者不在白名单，忽略: uid=%s" % rec["uid"])
            return None
        return (rec["text"], rec["mid"], rec["uid"], "card", rec.get("btn_t"))
    got = VERIFY(rec["mid"], tok)
    if not got:
        _log("hook 给的 mid=%s 在飞书查不到/不可信，丢弃（等历史兜底）" % rec["mid"])
        return None
    via = "hook" if (rec["ts"] and time.time() - rec["ts"] < 300) else "history"
    return (got[0], got[1], got[2], via, None)


# ----------------------------------------------------------------- 处理

def ack(db, chat, op, changed, tok, warn=""):
    """操作后的确认消息 —— 点了按钮必须一眼看出**成功没成功**。

    三类结果分开说，绝不合并成一句含糊的"已完成"：

      * 真改了的        → 逐条列出来 + 「↩️ 撤销」
      * 本来就是这个状态 → 明说"这次没有改动，可能重复点了一下"，**不给撤销按钮**
      * 台账里没这个编号 → 明说没有这条，并指向「状态」卡（别让用户以为自己没点中）

    只在**真有改动**时才给反向按钮：重复点击若也挂一个「撤销 #N」，
    用户很容易手滑把自己刚标好的成果撤回去（结果和意图正好相反）。
    done / drop 回带反向按钮的卡片；board / help 之类仍回纯文本。
    """
    n_done, n_open = cmds.progress(db)
    ok_items = [(t, ti, st) for t, ti, st, same in changed if st and not same]
    same_items = [(t, ti, st) for t, ti, st, same in changed if st and same]
    miss_ids = [t for t, ti, st, same in changed if not st]

    L = []
    if ok_items:
        L.append(cmds.describe([(t, ti, st, False) for t, ti, st in ok_items]))
    if same_items:
        L.append("ℹ️ 这次没有任何改动：" + "；".join(
            "#%d %s 早就是「%s」了（可能重复点了一下）"
            % (t, sanitize(ti, 18), cmds.STATE_LABEL.get(st, st)) for t, ti, st in same_items))
    if miss_ids:
        L.append("⚠️ 台账里没有 %s 这条（可能已经删了，或者你看的是旧卡片）。\n"
                 "　回一句「状态」重发一张，编号以新卡片为准。"
                 % " ".join("#%d" % i for i in miss_ids))
    if warn:
        L.append(warn)
    body = "%s\n%s\n\n📋 进度 %d/%d（还剩 %d 条）" % (
        ACK_HEAD.get(op, op), "\n".join(L) if L else "（没有变化）",
        n_done, n_done + n_open, n_open)

    now = int(time.time())
    actions, rest, has_undo, has_done = [], [], False, False
    for tid, _title, st in ok_items:
        if st == "todo":                       # 刚撤销完 → 给「再标回来」
            label, value = "✅ 完成 #%d" % tid, {"k": "done", "id": tid}
            has_done = True
        else:                                   # done / dismissed → 给「撤销」
            label = "↩️ 撤销 #%d" % tid
            value = {"k": "undo", "id": tid, "t": now}   # t = 按钮发出时刻，用于有效期
            has_undo = True
        if len(actions) < 3:
            actions.append({"tag": "button", "type": "default",
                            "text": {"tag": "plain_text", "content": label},
                            "value": value})
        else:
            rest.append(tid)

    tips = []
    if has_undo:
        tips.append("撤销按钮 %d 分钟内有效，过期后回「撤销 N」同样能改" % (UNDO_WINDOW // 60))
    if has_done:
        tips.append("想反悔就回「完成 N」")
    if same_items and not ok_items:
        tips.append("要改状态就回「完成 N」或「撤销 N」")
    if rest:
        tips.append("其余编号 %s 请直接回复" % " ".join("#%d" % i for i in rest))
    if not tips:
        tips.append("回「状态」看全貌")
    tip = "　·　".join(tips)

    if not actions:
        # 没有任何可点的东西（编号不存在 / 全部没改动）→ 一条纯文本就说明白了
        _say(chat, body + "\n\n" + tip, tok)
        return

    card = {"config": {"wide_screen_mode": True},
            "header": {"template": ACK_TMPL.get(op, "blue"),
                       "title": {"tag": "plain_text", "content": ACK_HEAD.get(op, op)}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                {"tag": "action", "actions": actions},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": tip}]},
            ]}
    # 卡片发不出去就退纯文本 —— 用户点完必须收到点什么
    _deliver(chat, card, body + "\n\n" + tip, tok)


def handle(db, text, mid, uid, via, allow, tok, chat, btn_t=None):
    """处理一条消息。返回是否产生了动作。

    全程不允许"静默失败"：任何一步出错都要给用户一句**准确**的话 ——
    是"没落库，请重来"还是"已经落库了，只是看板/回执没刷新"，两者不能含糊成一句。
    """
    if allow and uid not in allow:
        _log("忽略非白名单消息 mid=%s uid=%s" % (mid, uid))
        return False
    p = cmds.parse(text)
    if not p:
        return False                                   # 普通聊天，完全不理

    op = p["op"]
    if op == "help":
        _say(chat, cmds.usage(), tok)
        return True
    if op == "board":
        ok, msg = _publish(db, tok, new=True)
        _log("用户要台账 -> %s" % msg)
        if not ok:
            _say(chat, "⚠️ 看板没发出来：%s\n过一会儿再回一句「状态」试试。" % msg, tok)
        return True
    if p.get("need_id"):
        _say(chat, "没看懂要操作哪条。\n" + cmds.usage(), tok)
        return True

    # 撤销按钮的有效期（只管卡片点击；文字「撤销 3」永远可用）
    if via == "card" and op == "reopen":
        if btn_t:
            age = time.time() - btn_t
            if age > UNDO_WINDOW:
                _say(chat, "⚠️ 这个撤销按钮过期了（发出已 %.0f 分钟）。\n"
                           "直接回一句「撤销 %s」照样能改。" % (
                               age / 60.0, " ".join(str(i) for i in p["ids"])), tok)
                _log("撤销按钮过期 %.0fs，拒绝" % age)
                return True
        else:
            # 桥没把按钮时间戳带过来：按"不过期"处理（宁可让用户改得动，
            # 也不要按钮点了没反应），但要在日志里留痕，便于发现桥的升级问题
            _log("撤销按钮没有时间戳（btn_t 缺失），按不过期处理 mid=%s" % mid)

    applied, stage = False, "解析"
    try:
        stage = "落库"
        changed, err = cmds.apply(db, op, p["ids"],
                                  source=("按钮" if via == "card" else via))
        applied = True
        if err:
            _say(chat, "⚠️ %s" % err, tok)
            return True
        stage = "刷新看板"
        okp, msgp = _publish(db, tok)
        warn = "" if okp else ("⚠️ 看板没刷新成功（%s）—— 你的操作已经记下了，"
                               "回一句「状态」可以重发看板。" % msgp)
        stage = "回确认"
        ack(db, chat, op, changed, tok, warn=warn)
        _log("%s <- %r %s" % (via, text[:40], cmds.describe(changed).replace("\n", " / ")))
    except Exception as e:                             # noqa: BLE001
        try:
            db.rollback()                              # 别把半截事务留给下一次 commit
        except Exception:
            pass
        _log("处理 %r 在第[%s]步出错: %r" % (text[:30], stage, e))
        if applied:
            _say(chat, "⚠️ 你的操作**已经记进台账**了，但[%s]这一步出了问题（%s）。\n"
                       "回一句「状态」看一眼实际结果。" % (stage, _brief(e)), tok)
        else:
            _say(chat, "❌ 这次操作**没写进台账**（[%s]出错：%s）。\n"
                       "请再点/再发一次；要是连着失败，把这个截图发我。"
                       % (stage, _brief(e)), tok)
    return True


def _card_reject_note(rec, allow, chat, tok):
    """卡片点击被 admit 拒了 → 给一句明确反馈。

    只在两种情况回话：白名单用户点的 + 内容长得像我们自己的按钮（指令解析得出来，
    只是动作/编号不在白名单里）。别的（非白名单、压根不认识的 payload）保持安静 ——
    那可能是别人家的卡片或伪造的 JSONL，回话等于给陌生人回音。

    为什么不能静默：用户点了按钮什么都没发生，只会以为是自己没点中，然后反复点。
    """
    if rec.get("src") != "card":
        return
    if allow and rec.get("uid") not in allow:
        return
    if not cmds.parse(rec.get("text") or ""):
        return
    _say(chat, "⚠️ 这个按钮我没法处理（动作或编号不认识，可能是旧版本卡片）。\n"
               "直接回一句对应指令就行（例如「完成 3」），回「帮助」看全部指令。", tok)


def tick(db, tok, chat, allow, state, use_history=True):
    seen = state.setdefault("seen", [])
    seen_set = set(seen)
    items = []
    for rec in from_hook_file(state):
        got = admit(rec, tok, allow)
        if got:
            items.append(got)
        else:
            _card_reject_note(rec, allow, chat, tok)
    # 历史兜底不用每 10s 拉一次：默认 60s 一次；hook 通道每 10s 检查一次，够快
    if use_history and time.time() - float(state.get("last_history") or 0) >= POLL_SECONDS:
        for text, mid, uid, _ts in from_history(state, tok, chat):
            items.append((text, mid, uid, "history", None))
        state["last_history"] = time.time()
    n = 0
    for text, mid, uid, via, btn_t in items:
        if not mid:
            _log("记录没有 message_id，无法去重也无法回话，丢弃: %r" % text[:30])
            continue
        if mid in seen_set:
            # 同一条事件被投递两次（hook 文件 + 历史兜底会看到同一条消息）。
            # 第一次已经回过话了，这里再回一句只会让用户看到两张一模一样的确认 ——
            # 所以对用户安静，但日志里必须留下"我看过这条、是我主动跳过的"。
            _log("重复投递，跳过 mid=%s（%s，此前已处理）" % (mid, via))
            continue
        seen_set.add(mid)
        seen.append(mid)
        try:
            if handle(db, text, mid, uid, via, allow, tok, chat, btn_t=btn_t):
                n += 1
        except Exception as e:                         # handle 内部已经兜了一层，这是最后一道网
            _log("处理 %r 出错: %r" % (text[:30], e))
            try:
                _say(chat, "⚠️ 这条我没处理成（%s）—— 请再发/再点一次。" % _brief(e), tok)
            except Exception as e2:
                _log("出错后的回话也失败: %r" % e2)
    state["seen"] = seen[-SEEN_KEEP:]
    return n


def daily_digest(db, state, tok, chat):
    """每天 DIGEST_HOUR 点推一条早报（同一天只推一次）。"""
    lt = datetime.now(CST)
    if lt.hour != DIGEST_HOUR:
        return False
    if state.get("digest_date") == lt.strftime("%F"):
        return False
    state["digest_date"] = lt.strftime("%F")
    infer.suggest(db)
    rows = board.load(db)
    card = board.render(rows, digest=True)
    if DRY:
        print("  [DRY 早报] %s" % json.dumps(card, ensure_ascii=False)[:160])
        return True
    d = fsapi.send_card_to(chat, card, tok=tok)
    _log("早报: %s" % ("成功" if d.get("code") == 0 else d.get("msg")))
    return True


# ----------------------------------------------------------------- 主循环

def main():
    once = "--once" in sys.argv
    daemon = "--daemon" in sys.argv
    allow = set(fsapi.allowed_user())
    chat = fsapi.chat_id()
    state = _load(CURSOR_FILE, {})
    # 只在**首次**运行时把水位设到"现在"，避免把历史消息当成新指令；
    # 用 inited 标记而不是 "seen 是否为空" —— seen 在第一条指令来之前一直是空的，
    # 用它判断会导致每次重启都重置水位、丢掉停机期间的消息。
    if daemon and not state.get("inited"):
        state["history_cursor"] = time.time()
        state["digest_date"] = datetime.now(CST).strftime("%F")
        try:
            state["hook_offset"] = os.path.getsize(HOOK_FILE)
        except OSError:
            state["hook_offset"] = 0
        state["inited"] = True
    tok = None
    last = 0.0
    while True:
        try:
            if daemon and time.time() - last > 1800:
                tok = fsapi.token()
                last = time.time()
            tok = tok or fsapi.token()
            db = sqlite3.connect(DB)
            state.setdefault("seen", [])
            if daemon:
                infer.suggest(db)
                daily_digest(db, state, tok, chat)
                n = tick(db, tok, chat, allow, state)
                _save(CURSOR_FILE, state)
                db.close()
                if n:
                    _log("本轮处理 %d 条指令" % n)
            else:
                n = tick(db, tok, chat, allow, state, use_history=("--no-history" not in sys.argv))
                _save(CURSOR_FILE, state)
                db.close()
                print("处理了 %d 条指令" % n)
        except Exception as e:
            _log("tick 异常: %r" % e)
        if once or not daemon:
            return
        time.sleep(HOOK_TICK)


if __name__ == "__main__":
    sys.exit(main() or 0)
