# -*- coding: utf-8 -*-
"""task-cmd-bridge：把飞书来的「求职台账指令」实时抄给 root，并让 AI 不要重复回答。

两条输入：
  1) 用户在飞书里发的文字指令（"完成 3"）
  2) **卡片按钮点击** —— 飞书把它转成合成指令，形如
         /card button {"k": "done", "id": 3}
     （见 plugins/platforms/feishu/adapter.py::_handle_card_action_event：
        synthetic_text = f"/card {action_tag}" + " " + json.dumps(action_value)）
     这里把它**翻译回等价文字指令**（"完成 3"），复用 root 侧同一条解析链路。

为什么是 plugin 而不是 shell hook：
  shell hook 的 stdout 会被 agent/shell_hooks._parse_response() 过滤，它只认
  pre_tool_call / pre_verify / pre_llm_call 三种事件，pre_gateway_dispatch 的
  {"action":"skip"} 会被丢掉（实测 `hermes hooks test` 显示 parsed: <none>）。
  而 plugin 的 hook 回调返回值会原样进入 invoke_hook() 的结果列表，网关
  gateway/run.py 的 pre_gateway_dispatch 分支能直接看到 {"action":"skip"}。

落点：~/.hermes/plugins/task-cmd-bridge/（user plugin，registry key = task-cmd-bridge）
启用：config.yaml 里 plugins.enabled 必须包含 task-cmd-bridge

安全：本文件可能被 hermes 用户改写（它跟 AI 同权限），所以 root 侧
      /opt/mail-agent-interact/poller.py 把这份 JSONL 当作**不可信输入**：
      文字消息只取 message_id、正文回飞书核对；
      卡片点击走单独一条闸门（见 poller.admit），只认白名单用户 + 白名单动词 + 纯数字编号。
"""
import json
import os
import re
import time

OUT = os.path.expanduser("~/.hermes/state/feishu_task_cmds.jsonl")
ENV = os.path.expanduser("~/.hermes/.env")
# 不认识的 /card 文本记在这里：第一次真点击就能拿到 ground truth
DBG = os.path.expanduser("~/.hermes/state/feishu_card_debug.log")
DBG_MAX = 65536

_DONE = r"完成|做完|做好|搞定|好了|已做|已办|finished|finish|done|okay|ok"
_DROP = r"放弃|忽略|不做|不用做|取消|drop|skip|cancel"
_REOPEN = r"恢复|撤销|重开|undo|restore|reopen"
_ACCEPT = r"确认|采纳|接受|accept|yes"
_REJECT = r"驳回|不对|不是|拒绝|reject|no"
_VERBS = "|".join((_DONE, _DROP, _REOPEN, _ACCEPT, _REJECT))

# 编号必须用空白/逗号/顿号隔开，且单个不超过 5 位 —— 否则 "999999999999" 会被
# 当成多个编号拼出来，桥会说 skip 而 root 侧不认，用户就"发了消息却没人回应"。
_IDS = r"[#＃]?\d{1,5}[#＃]?"
_SEP = r"(?:[,，、/]\s*|\s+)"
RE_ACTION = re.compile(
    r"^(?:"
    r"(?:" + _VERBS + r")[了啦吧]?\s*[:：]?\s*"
    r"(?:" + _IDS + r"(?:" + _SEP + _IDS + r")*|(?:全部|所有|全都|都|全))"
    r"|(?:全部|所有|全都|都|全)[了]?(?:" + _VERBS + r")[了啦吧]?"
    r")$")
RE_BOARD = re.compile(r"^[#/!]?\s*(?:状态|台账|进度|看板|列表|清单|board|status|list|\?)$", re.I)
RE_HELP = re.compile(r"^[#/!]?\s*(?:帮助|说明|怎么用|help)$", re.I)


def _norm(t):
    t = (t or "").strip()
    t = "".join(chr(ord(c) - 0xFEE0) if 0xFF10 <= ord(c) <= 0xFF19 else c for c in t)
    t = t.replace("，", ",").replace("、", ",")
    return re.sub(r"\s+", " ", re.sub(r"[\u200b-\u200f\ufeff\u2028\u2029]", "", t)).strip()


def is_command(text):
    t = _norm(text)
    if not t or len(t) > 120:
        return False
    return bool(RE_ACTION.match(t) or RE_BOARD.match(t) or RE_HELP.match(t))


# ---- 卡片按钮点击 → 文字指令 ---------------------------------------------
# 只认这三个动词，别的一律忽略（不放过任何自由文本）
CARD_VERB = {"done": "完成", "drop": "放弃", "undo": "撤销"}
RE_ID = re.compile(r"^[1-9]\d{0,4}$")     # 1..99999，纯数字，不接受 0/负数/超长


def _pick(obj, key):
    """对象或 dict 都取得到值（websocket 路径给对象，webhook 路径给 dict）。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def card_value(ev, text):
    """拿按钮的 value：优先 raw_message（飞书认证过的载荷），退回从 text 抠 JSON。"""
    evt = _pick(getattr(ev, "raw_message", None), "event")
    v = _pick(_pick(evt, "action"), "value")
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.lstrip().startswith("{"):
        try:
            d = json.loads(v)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    i = text.find("{")            # 从 "/card <tag> {json}" 里抠
    if i >= 0:
        try:
            d = json.loads(text[i:])
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return None


def card_operator(ev):
    evt = _pick(getattr(ev, "raw_message", None), "event")
    return str(_pick(_pick(evt, "operator"), "open_id") or "")


def card_msg_id(ev):
    evt = _pick(getattr(ev, "raw_message", None), "event")
    return str(_pick(_pick(evt, "context"), "open_message_id") or "")


def translate_card(text, value):
    """按钮点击 → 等价文字指令。任何不合规一律返回 None。"""
    if not text.startswith("/card "):
        return None
    if not isinstance(value, dict):
        return None
    k = value.get("k")
    if not isinstance(k, str) or k not in CARD_VERB:
        return None
    raw = value.get("id")
    sid = str(raw).strip() if isinstance(raw, (int, str)) and not isinstance(raw, bool) else ""
    if not RE_ID.match(sid):
        return None
    return "%s %s" % (CARD_VERB[k], sid)


def _dbg(line):
    try:
        if os.path.exists(DBG) and os.path.getsize(DBG) > DBG_MAX:
            os.replace(DBG, DBG + ".1")
        os.makedirs(os.path.dirname(DBG), exist_ok=True)
        with open(DBG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%F %T"), line[:400]))
    except Exception:
        pass


def _allowed():
    """读 FEISHU_ALLOWED_USERS（env 优先，退回 .env），只对白名单用户沉默 AI。"""
    v = os.environ.get("FEISHU_ALLOWED_USERS", "")
    if not v:
        try:
            with open(ENV, encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("FEISHU_ALLOWED_USERS="):
                        v = line.split("=", 1)[1].strip()
                        break
        except Exception:
            v = ""
    return {x.strip() for x in v.split(",") if x.strip()}


_ALLOW = _allowed()


def _append(rec):
    line = json.dumps(rec, ensure_ascii=False)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _on_card(text, ev):
    """卡片按钮点击。返回 skip 让 AI 闭嘴，或 None 放行（不认识就不插手）。"""
    try:
        val = card_value(ev, text)
        cmd = translate_card(text, val)
        if not cmd:
            _dbg("UNRECOGNIZED text=%r value=%r" % (text[:200], val))
            return None
        mid = getattr(ev, "message_id", "") or ""
        if not mid:
            _dbg("NO_MID cmd=%r text=%r" % (cmd, text[:120]))
            return None
        src = getattr(ev, "source", None)
        uid = str(getattr(src, "user_id", "") or "") or card_operator(ev)
        if _ALLOW and uid not in _ALLOW:
            _dbg("REJECT_NOT_ALLOWED uid=%r cmd=%r" % (uid, cmd))
            return None
        rec = {"text": cmd[:200], "message_id": str(mid)[:120],
               "user_id": uid[:64], "ts": round(time.time(), 3),
               "src": "card", "operator": card_operator(ev)[:64],
               "card_mid": card_msg_id(ev)[:120]}
        t = (val or {}).get("t")          # 撤销按钮的有效期戳（由 root 生成）
        ts = int(t) if isinstance(t, int) and not isinstance(t, bool) else (
            int(t) if isinstance(t, str) and t.isdigit() else None)
        if ts:
            rec["btn_t"] = ts
        _append(rec)
        _dbg("OK %s <- %r" % (cmd, text[:120]))
    except Exception as e:
        _dbg("EXC %r" % (e,))
        return None
    return {"action": "skip"}


def _on_gateway_dispatch(event=None, **kwargs):
    """飞书每条入站消息都会走到这里（含卡片按钮 / reaction 的合成事件）。"""
    try:
        text = getattr(event, "text", "") or ""
        if text.startswith("/card "):        # 卡片按钮点击
            return _on_card(text, event)
        if not is_command(text):
            return None
        mid = getattr(event, "message_id", "") or ""
        if not mid:
            return None
        src = getattr(event, "source", None)
        uid = str(getattr(src, "user_id", "") or "")
        if _ALLOW and uid not in _ALLOW:
            return None                      # 非白名单：不抄、也不替它闭嘴
        _append({"text": text[:200], "message_id": str(mid)[:120],
                 "user_id": uid[:64], "ts": round(time.time(), 3)})
        return {"action": "skip"}            # 让网关丢弃这条：AI 不再回答
    except Exception:
        return None                          # 桥坏掉也不能影响正常聊天


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", _on_gateway_dispatch)
