
# ------------------------------------------------------------ B. 按钮链路
def call_handle(conn, text, via="card", uid="ou_TEST", btn_t=None):
    clear()
    poller.handle(conn, text, "om_test_%d" % time.time(), uid, via, {"ou_TEST"},
                  "TOKEN", "CHAT", btn_t=btn_t)
    return replies_text(), ack_cards()


def part_b(conn):
    print("\n" + "=" * 74)
    print("B. 点\u300C\u2705 完成\u300D之后\uFF1A每条失败路径现在说什么\uFF08飞书全被桩拦住\uFF09")
    print("=" * 74)
    conn.row_factory = sqlite3.Row
    tid = conn.execute("SELECT id FROM tasks WHERE email_id=902").fetchone()[0]
    done_tid = conn.execute("SELECT id FROM tasks WHERE state='done' ORDER BY id LIMIT 1").fetchone()[0]
    t901 = conn.execute("SELECT id FROM tasks WHERE email_id=901").fetchone()[0]

    # B1 正常路径\uFF08先证明这条链路没坏\uFF09
    texts, cards = call_handle(conn, "完成 %d" % tid)
    st = conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    card = cards[0] if cards else {}
    btns = card_buttons(card)
    check("B1 点\u300C完成\u300D\u2192 落库为 done", st == "done")
    check("B1 \u2192 回一张确认卡\uFF0C写明改了什么",
          bool(cards) and ("已完成" in card_text(card)), card_text(card).replace("\n", " / ")[:90])
    check("B1 \u2192 确认卡带\u300C\u21A9\uFE0F 撤销\u300D按钮\uFF0C且 value 里有编号和时间戳",
          any(b["value"].get("id") == tid and b["value"].get("t") for b in btns),
          json.dumps([b.get("value") for b in btns], ensure_ascii=False))

    # B2 重复点击\uFF08任务已经是 done\uFF09
    texts, cards = call_handle(conn, "完成 %d" % tid)
    card = cards[0] if cards else {}
    body = card_text(card) if card else (texts[0] if texts else "")
    btns = card_buttons(card)
    check("B2 重复点击 \u2192 明说\u300C这次没有任何改动\u300D+ 早就是该状态",
          "没有任何改动" in body and "早就是" in body, body.replace("\n", " / ")[:100])
    check("B2 重复点击 \u2192 不再给\u300C撤销\u300D按钮\uFF08避免手滑把自己标好的撤掉\uFF09",
          not any(b["value"].get("k") == "undo" for b in btns))

    # B3 编号不存在
    texts, cards = call_handle(conn, "完成 9999")
    body = card_text(cards[0]) if cards else (texts[0] if texts else "")
    check("B3 编号不存在 \u2192 明说\u300C台账里没有 #9999 这条\u300D并给下一步",
          "#9999" in body and "台账里没有" in body and "状态" in body,
          body.replace("\n", " / ")[:110])

    # B4 撤销窗口过期
    old = int(time.time()) - (poller.UNDO_WINDOW + 300)
    texts, cards = call_handle(conn, "撤销 %d" % done_tid, btn_t=old)
    body = texts[0] if texts else ""
    check("B4 撤销按钮过期 \u2192 明说过期多久 + 文字指令仍可用",
          "过期" in body and "撤销 %d" % done_tid in body, body.replace("\n", " / ")[:110])

    # B5 落库失败\uFF08数据库被锁\uFF09
    real_apply = poller.cmds.apply
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    poller.cmds.apply = boom
