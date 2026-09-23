#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把用户的一句话解析成对台账的操作，并落库。

设计原则（安全边界）：
  - 这里只做「解析 + UPDATE tasks.state」两件事，绝不 shell、绝不 eval、
    绝不去请求任务链接（那会代替用户做不可逆决定）。
  - 解析失败 = 当普通聊天，不做任何动作、不回话。
  - 指令必须带编号或"全部/都"，避免"我今天完成了很多事"被误判。
  - 所有指令都在 poller 里校验 open_id 白名单。
"""
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, "/root/mail-agent")
from common import CST, DB, sanitize  # noqa: E402

# 动词表（长的放前面，避免 "ok" 吃掉 "okay"）
_DONE = r"完成|做完|做好|搞定|好了|已做|已办|finished|finish|done|okay|ok"
_DROP = r"放弃|忽略|不做|不用做|取消|drop|skip|cancel"
_REOPEN = r"恢复|撤销|重开|undo|restore|reopen"
_ACCEPT = r"确认|采纳|接受|accept|yes"
_REJECT = r"驳回|不对|不是|拒绝|reject|no"
_BOARD = r"状态|台账|进度|看板|列表|清单|board|status|list|\?"
_HELP = r"帮助|说明|怎么用|help"

_VERBS = "|".join((_DONE, _DROP, _REOPEN, _ACCEPT, _REJECT))
_ACTION_RE = re.compile(r"^(?P<v>" + _VERBS + r")[了啦吧]?\s*[:：]?\s*(?P<rest>.*)$")
_ALL_PREFIX_RE = re.compile(r"^(?:全部|所有|全都|都|全)[了]?(?P<v>" + _VERBS + r")[了啦吧]?$")
_NUM_RE = re.compile(r"\d+")
_REST_NUM_RE = re.compile(r"^[#＃\s]*(?:\d+[#＃\s]*[,/]?\s*)+$")

_STATE_OF = {"done": "done", "accept": "done",
             "drop": "dismissed", "reject": "dismissed",
             "reopen": "todo"}
STATE_LABEL = {"todo": "进行中", "done": "已完成", "expired": "已过期", "dismissed": "已忽略"}
_OP_OF_VERB = {}


def _classify(verb):
    if re.fullmatch(_DONE, verb, re.I):
        return "done"
    if re.fullmatch(_DROP, verb, re.I):
        return "drop"
    if re.fullmatch(_REOPEN, verb, re.I):
        return "reopen"
    if re.fullmatch(_ACCEPT, verb, re.I):
        return "accept"
    return "reject"


def _norm(text):
    """全角数字/标点归一化 + 去零宽字符。"""
    t = (text or "").strip()
    t = "".join(chr(ord(c) - 0xFEE0) if 0xFF10 <= ord(c) <= 0xFF19 else c for c in t)
    t = t.replace("，", ",").replace("、", ",").replace("．", ".")
    t = re.sub(r"[\u200b-\u200f\ufeff\u2028\u2029]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def parse(text):
    """返回 dict(op=..., ids=[...]) / None（不是指令）。

    op ∈ {done, drop, reopen, accept, reject, board, help}
    ids 是 int 列表，或 ["all"]
    """
    t = _norm(text)
    if not t or len(t) > 120:
        return None

    if re.fullmatch(r"[#/!]?\s*(?:" + _BOARD + r")", t, re.I):
        return {"op": "board", "ids": []}
    if re.fullmatch(r"[#/!]?\s*(?:" + _HELP + r")", t, re.I):
        return {"op": "help", "ids": []}

    m = _ALL_PREFIX_RE.match(t)
    if m:
        op = _classify(m.group("v"))
        return {"op": op, "ids": ["all"]}

    m = _ACTION_RE.match(t)
    if not m:
        return None
    op = _classify(m.group("v"))
    rest = (m.group("rest") or "").strip()

    if not rest:
        # 整句就是一个动词：不猜，回用法提示
        return {"op": op, "ids": [], "need_id": True}
    if rest in ("全部", "所有", "全都", "都", "全") and op in ("done", "drop", "reopen"):
        return {"op": op, "ids": ["all"]}
    if _REST_NUM_RE.match(rest):
        ids = sorted({int(x) for x in _NUM_RE.findall(rest)})
        ids = [i for i in ids if 0 < i < 100000]      # 编号有界，挡住 999999999999
        return {"op": op, "ids": ids[:30]} if ids else None
    # 动词后面还有别的话 → 当成自然语言，不处理
    return None


def usage():
    return ("台账指令：\n"
            "  完成 3        标记 #3 已完成（支持「完成 3 4 5」）\n"
            "  放弃 3        忽略 #3\n"
            "  恢复 3        撤回，改回进行中\n"
            "  确认 3        采纳「疑似已完成」建议\n"
            "  状态          重发一份台账\n"
            "  全部完成      把所有待办一次标完")


def _now():
    return datetime.now(CST).isoformat(timespec="seconds")


def apply(db, op, ids, source="飞书回复"):
    """把一次操作落到 tasks 表。返回 (changed, err)。

    changed: [(id, title, new_state, 是否本来就是该状态)]
    只碰 state / done_at / done_source 三列。
    """
    if op not in _STATE_OF:
        return [], ""
    if ids == ["all"]:
        rows = db.execute("SELECT id FROM tasks WHERE state IN ('todo','expired')").fetchall()
        ids = [(r["id"] if isinstance(r, sqlite3.Row) else r[0]) for r in rows]
        if not ids:
            return [], "没有待办可以处理"
    if not ids:
        return [], ""

    new_state = _STATE_OF[op]
    changed = []
    db.row_factory = sqlite3.Row
    for tid in ids:
        if not isinstance(tid, int) or tid <= 0:
            continue
        r = db.execute("SELECT id, title, state FROM tasks WHERE id=?", (tid,)).fetchone()
        if not r:
            changed.append((tid, "（台账里没有这条）", None, False))
            continue
        if r["state"] == new_state:
            changed.append((tid, r["title"], new_state, True))
            continue
        if new_state == "done":
            done_at, done_source = _now(), source
        elif new_state == "dismissed":
            done_at, done_source = _now(), source
        else:                                    # reopen -> todo
            done_at, done_source = None, None
        db.execute("UPDATE tasks SET state=?, done_at=?, done_source=? WHERE id=?",
                   (new_state, done_at, done_source, tid))
        changed.append((tid, r["title"], new_state, False))
    db.commit()
    return changed, ""


def describe(changed):
    out = []
    for tid, title, st, same in changed:
        if st is None:
            out.append("#%d 不在台账里" % tid)
        else:
            out.append("#%d %s → %s%s" % (tid, sanitize(title, 40),
                                         STATE_LABEL.get(st, st), "（本来就是这个状态）" if same else ""))
    return "\n".join(out)


def progress(db):
    n_done = db.execute("SELECT COUNT(*) FROM tasks WHERE state='done'").fetchone()[0]
    n_open = db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('todo','expired')"
                        ).fetchone()[0]
    return n_done, n_open


if __name__ == "__main__":
    cases = [
        ("完成 3", ("done", [3])), ("完成3", ("done", [3])),
        ("完成了 3", ("done", [3])), ("做完了3 4", ("done", [3, 4])),
        ("完成 3 4 5", ("done", [3, 4, 5])), ("完成3,4", ("done", [3, 4])),
        ("完成 3、4", ("done", [3, 4])), ("done 3", ("done", [3])),
        ("搞定 3", ("done", [3])), ("好了 2", ("done", [2])),
        ("放弃 3", ("drop", [3])), ("忽略 2", ("drop", [2])),
        ("恢复 3", ("reopen", [3])), ("撤销 3", ("reopen", [3])),
        ("状态", ("board", [])), ("台账", ("board", [])), ("?", ("board", [])),
        ("进度", ("board", [])), ("帮助", ("help", [])),
        ("全部完成", ("done", ["all"])), ("都完成了", ("done", ["all"])),
        ("全部放弃", ("drop", ["all"])),
        ("确认 3", ("accept", [3])), ("驳回 3", ("reject", [3])),
        ("完成 3,4,5,6", ("done", [3, 4, 5, 6])),
        # 不应被当成指令
        ("我今天完成了很多事情", None), ("你好", None), ("帮我看看这个", None),
        ("3", None), ("完成 3 谢谢", None), ("这个任务完成了吗", None),
        ("我在做测评", None),
        # 需要编号
        ("完成", "need_id"), ("放弃", "need_id"), ("done", "need_id"),
    ]
    bad = 0
    for text, want in cases:
        got = parse(text)
        if want is None:
            ok = got is None
        elif want == "need_id":
            ok = bool(got and got.get("need_id"))
        else:
            ok = bool(got and (got["op"], got["ids"]) == want)
        if not ok:
            bad += 1
        print("%-22s -> %-46s %s" % (text, got, "OK" if ok else "FAIL want=%s" % (want,)))
    print("\n解析器自测：%d 例，%d 失败" % (len(cases), bad))
    sys.exit(1 if bad else 0)
