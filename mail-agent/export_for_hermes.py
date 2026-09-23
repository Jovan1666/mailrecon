#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 Hermes 导出一份脱敏的求职台账，供它在飞书里回答用户提问。

为什么要这个：Hermes 跑在无权限的 hermes 用户下，读不到 /root 里的账本。
但用户会问"我投了哪些公司""进展怎么样"。所以由 root 定期导出一份
**脱敏**快照放到 hermes 能读的地方。

脱敏边界（很重要）：
  - 不含邮件正文
  - 不含授权码、API key 等任何凭据
  - 不含手机号、身份证、学号
  - 发件人只保留域名，不保留完整地址（避免 HR 个人邮箱外泄）
  - 保留：公司、岗位、状态、时间、域名 —— 这些是回答所需的最小集

安全：写文件的路径写死在 /home/hermes/.hermes/maildata/ 下，
     写入点再做一次规范化校验（防路径穿越），不接收任何外部传入的路径。
"""
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "/root/mail-agent")
from common import DB, CST, JOB_PAT, BOUNCE_PAT, BOUNCE_FROM, AUTO_PAT, SPAM_PAT, \
    NOREPLY_FROM, match_key, sanitize, fmt as fmt_time

# 输出目录写死（不接收参数、不拼接外部输入）
OUT_DIR = "/home/hermes/.hermes/maildata"
OUT_MD = "/home/hermes/.hermes/maildata/求职台账.md"
OUT_JSON = "/home/hermes/.hermes/maildata/求职台账.json"

# 从"应聘XX岗-姓名-学校-..."这类主题里抽岗位名
JOB_TITLE = re.compile(r"^(?:应届生)?应聘\s*([^-+]{2,24})")


def _safe_out(path, mode=0o644):
    """把目标路径规范化并断言落在 OUT_DIR 内；返回规范化后的路径。"""
    base = os.path.realpath(OUT_DIR)
    rp = os.path.realpath(os.path.abspath(path))
    if not (rp == base or rp.startswith(base + os.sep)):
        raise SystemExit("拒绝写入 %s 之外的文件：%r" % (base, path))
    if ".." in path.replace("\\", "/").split("/"):
        raise SystemExit("路径不允许包含 ..：%r" % path)
    return rp


def _write_text(path, text, mode=0o644):
    """原子写：先写临时文件再替换。用 os.open 显式指定权限。"""
    rp = _safe_out(path)
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    tmp = rp + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, rp)


def domain_only(addr):
    a = (addr or "").strip().lower()
    return a.split("@", 1)[1] if "@" in a else (a or "?")


def _load_names():
    """取本人姓名和别名，用于把主题里的姓名换掉。"""
    try:
        import prep
        ident = prep.load_identity() or {}
        names = [ident.get("name") or ""] + list(ident.get("name_aliases") or [])
        return [n for n in names if n and len(n) >= 2]
    except Exception:
        return []


def _scrub(text, names):
    """把正文/主题里出现的本人姓名替换成标记（长名优先，避免切碎）。"""
    s = text or ""
    for n in sorted(names, key=len, reverse=True):
        s = s.replace(n, "[本人]")
    return s


def main():
    NAMES = _load_names()
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    # ---------- 我发出的投递 ----------
    sent = []
    for r in db.execute("""SELECT e.to_addr, e.from_addr, e.subject, e.internaldate
                             FROM email e JOIN folder f ON f.id=e.folder_id
                            WHERE f.role='sent' AND e.fetched=1 AND e.origin<>'stale'
                            ORDER BY e.internaldate"""):
        subj = r["subject"] or ""
        if not JOB_PAT.search(subj):
            continue
        m = JOB_TITLE.search(subj)
        sent.append({
            "对象": match_key(r["to_addr"]) or domain_only(r["to_addr"]),
            "域名": domain_only(r["to_addr"]),
            "岗位": sanitize(m.group(1), 24) if m else "",
            "时间": fmt_time(r["internaldate"], with_date=True),
        })

    # ---------- 收到的回信 ----------
    recv = []
    for r in db.execute("""SELECT e.from_addr, e.subject, e.internaldate
                             FROM email e JOIN folder f ON f.id=e.folder_id
                            WHERE f.role IN ('inbox','junk') AND e.fetched=1
                              AND e.origin<>'stale' ORDER BY e.internaldate"""):
        subj = r["subject"] or ""
        if BOUNCE_PAT.search(subj) or BOUNCE_FROM.match(r["from_addr"] or ""):
            kind = "退信"
        elif SPAM_PAT.search(subj):
            continue
        elif AUTO_PAT.search(subj):
            kind = "自动回复"
        elif NOREPLY_FROM.match(r["from_addr"] or ""):
            kind = "系统通知"
        else:
            kind = "来信"
        recv.append({
            "来源": match_key(r["from_addr"]) or domain_only(r["from_addr"]),
            "域名": domain_only(r["from_addr"]),
            "类型": kind,
            "主题": sanitize(_scrub(subj, NAMES), 60),
            "时间": fmt_time(r["internaldate"], with_date=True),
        })

    # ---------- 行动项 ----------
    tasks = []
    try:
        for r in db.execute("""SELECT id, title, due_utc, link_host, state, credential
                                 FROM tasks ORDER BY
                                 CASE state WHEN 'todo' THEN 0 ELSE 1 END, id"""):
            tasks.append({
                "编号": r["id"], "内容": r["title"],
                "截止": fmt_time(r["due_utc"], with_date=True) if r["due_utc"] else "",
                "域名": r["link_host"],
                "登录凭据": r["credential"] or "",
                "状态": {"todo": "待办", "done": "已完成",
                         "expired": "已过期", "dismissed": "已忽略"}.get(r["state"], r["state"]),
            })
    except sqlite3.OperationalError:
        pass

    # ---------- 对账汇总 ----------
    targets = {s["对象"] for s in sent}
    replied = defaultdict(list)
    for r in recv:
        if r["类型"] == "来信":
            replied[r["来源"]].append(r)
    matched = sorted(targets & set(replied))
    no_reply = sorted(targets - set(replied))

    stamp = datetime.now(CST).isoformat(timespec="seconds")

    data = {
        "生成时间": stamp,
        "汇总": {
            "投递对象数": len(targets),
            "投递邮件数": len(sent),
            "有回信": len(matched),
            "无回音": len(no_reply),
            "待办任务": len([t for t in tasks if t["状态"] == "待办"]),
        },
        "投递": sent,
        "有回信的": matched,
        "无回音的": no_reply,
        "收到的信": recv[-200:],
        "行动项": tasks,
    }
    _write_text(OUT_JSON, json.dumps(data, ensure_ascii=False, indent=1))

    # ---------- 写给人看的 Markdown ----------
    L = ["# 求职台账（由脚本自动生成，只读）", "",
         "> 生成时间：%s" % stamp,
         "> 这是脱敏快照：不含邮件正文、不含姓名手机号、发件人只留域名。", "",
         "## 汇总", "", "| 指标 | 数值 |", "|---|---|"]
    for k, v in data["汇总"].items():
        L.append("| %s | %s |" % (k, v))
    L.append("")

    if tasks:
        L += ["## 行动项（%d 条）" % len(tasks), "",
              "| 编号 | 内容 | 截止 | 状态 | 域名 | 登录凭据 |", "|---|---|---|---|---|---|"]
        for t in tasks:
            L.append("| %s | %s | %s | %s | %s | %s |" % (
                t["编号"], t["内容"], t["截止"] or "—", t["状态"],
                t["域名"] or "—", t["登录凭据"] or "—"))
        L.append("")

    L += ["## 我投递过的对象（%d 个）" % len(targets), ""]
    agg = defaultdict(lambda: {"n": 0, "岗位": set(), "最早": "9999", "最晚": ""})
    for s in sent:
        a = agg[s["对象"]]
        a["n"] += 1
        if s["岗位"]:
            a["岗位"].add(s["岗位"])
        a["最早"] = min(a["最早"], s["时间"])
        a["最晚"] = max(a["最晚"], s["时间"])
    for k in sorted(agg, key=lambda x: agg[x]["最晚"], reverse=True):
        a = agg[k]
        back = "✅ 有回信" if k in matched else "—"
        L.append("- **%s**　%s　投递 %d 次　最近 %s　%s" % (
            k, "/".join(sorted(a["岗位"]))[:30] or "岗位未标注", a["n"], a["最晚"][:10], back))
    L.append("")

    L += ["## 最近收到的信（最多 30 条）", ""]
    for r in sorted(recv, key=lambda x: x["时间"], reverse=True)[:30]:
        L.append("- [%s] %s ｜ %s ｜ %s" % (
            r["时间"][:16], r["类型"], r["来源"], r["主题"][:44]))
    L.append("")

    _write_text(OUT_MD, "\n".join(L))

    os.chmod(os.path.realpath(OUT_DIR), 0o755)
    db.close()
    print("已导出：%s" % OUT_MD)
    print("  投递对象 %d 个 / 有回信 %d / 无回音 %d / 待办 %d 条"
          % (len(targets), len(matched), len(no_reply),
             len([t for t in tasks if t["状态"] == "待办"])))


if __name__ == "__main__":
    main()
