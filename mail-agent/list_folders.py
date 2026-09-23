#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第一步：列出邮箱里所有文件夹，确认真实名字。全程只读。

**注意：真正干活的代码全部收在 main() 里，模块级只有常量和函数定义。**
这样 `import list_folders` 不会产生任何网络连接、不会打印账号名 ——
否则任何人 clone 这个仓库后顺手 import 一下，就会拿你的凭据去连你的邮箱。
"""
import sys

from imapclient import IMAPClient

# 默认配置路径；可用环境变量 MAIL_ENV 指到别处
ENV_PATH = "/root/mail-agent/.env"


def load_env(path=ENV_PATH):
    cfg = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


def main():
    cfg = load_env()
    host = cfg.get("IMAP_HOST", "imap.qq.com")
    port = int(cfg.get("IMAP_PORT", "993"))
    user, code = cfg["QQ_EMAIL"], cfg["QQ_AUTH_CODE"]

    print("连接 %s:%d" % (host, port))
    print("账号 %s" % user)
    try:
        c = IMAPClient(host, port=port, ssl=True, timeout=30)
        c.login(user, code)
    except Exception as e:
        print("登录失败: %r" % e)
        return 1

    try:
        print("登录成功 ✓")
        caps = c.capabilities()
        print("服务端能力: ID=%s IDLE=%s UIDPLUS=%s"
              % ("ID" in caps, "IDLE" in caps, "UIDPLUS" in caps))
        print()

        folders = c.list_folders()
        print("=== 所有文件夹（共 %d 个）===" % len(folders))
        for flags, delim, name in folders:
            fl = " ".join(f.decode() if isinstance(f, bytes) else str(f) for f in flags)
            print("  %-34s [%s]" % (name, fl))

        print()
        print("=== 关键文件夹定位 ===")
        by_attr = {}
        for flags, delim, name in folders:
            for f in flags:
                key = f.decode() if isinstance(f, bytes) else str(f)
                by_attr.setdefault(key.lower(), []).append(name)
        for role, attr in [("收件箱", "\\inbox"), ("已发送", "\\sent"),
                           ("垃圾邮件", "\\junk"), ("已删除", "\\trash"),
                           ("草稿", "\\drafts")]:
            hit = by_attr.get(attr)
            print("  %-8s -> %s" % (role, hit[0] if hit else "（服务端未标该属性）"))

        print()
        print("=== 各文件夹统计 ===")
        for flags, delim, name in folders:
            try:
                st = c.folder_status(name, ["MESSAGES", "UIDNEXT", "UIDVALIDITY"])
                print("  %-34s 邮件数=%-5s UIDNEXT=%-8s UIDVALIDITY=%s" % (
                    name, st.get(b"MESSAGES"), st.get(b"UIDNEXT"),
                    st.get(b"UIDVALIDITY")))
            except Exception as e:
                print("  %-34s 取状态失败: %s" % (name, e))

        print()
        print("完成（全程只读，未修改任何东西）")
        return 0
    finally:
        try:
            c.logout()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
