#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台域名 / 品牌清单的加载器。

**为什么单独拎出来**：一份"实测出现过的平台域名"清单，等于一张**服务使用画像**
（用过哪些电商、显卡、网盘、机场、盗版站、招聘系统）。源码是要公开的，
所以这类清单不能写死在代码里。做法：

  * 源码内置的默认清单**只含明显虚构的示例域名**（`*.example.com`），
    够让自测和演示跑起来就行；
  * 真实清单放 `platform_domains.txt`（**不入库**，见 .gitignore），
    启动时合并进来 —— 规则覆盖面一点不少，画像一点不留。

配置文件（默认取模块同目录的 `platform_domains.txt`，可用环境变量
`PLATFORM_DOMAINS_FILE` 指定别处）：

    # 每行一个值；`#` 起注释；`[节名]` 切换当前分类；没有节名的行归 [noise]
    [job]           招聘系统 / ATS / 求职平台 —— 命中即算"求职语境"，永不被 drop
    [noise]         消费级 / 工具类平台 —— 非求职语境下按域名 drop
    [spam]          确证的垃圾 / 钓鱼域名
    [spam_brand]    common.py 的 SPAM_PAT 里额外要命中的品牌词
    [action_hint]   links.py 的 ACTION_HINT 里额外要命中的平台词
    [contest]       triage.py 的 RE_COMPETITION 里额外要命中的赛事关键词

文件不存在 / 读不了 / 行写错：**一律不报错**，只返回空集合。
配置缺失只会让规则覆盖面变小，绝不能让脚本起不来。
"""
import os

PLATFORM_FILE = os.environ.get("PLATFORM_DOMAINS_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "platform_domains.txt")

SECTIONS = ("job", "noise", "spam", "spam_brand", "action_hint", "contest")


def load(path=PLATFORM_FILE):
    """读配置文件，返回 {节名: set(小写值)}。任何异常都吞掉并返回已读到的部分。"""
    out = {s: set() for s in SECTIONS}
    section = "noise"
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip().lower()
                if not line:
                    continue
                if line.startswith("[") and line.endswith("]"):
                    name = line[1:-1].strip()
                    section = name if name in out else "noise"
                    continue
                out[section].add(line)
    except Exception:
        pass
    return out


SOURCE = PLATFORM_FILE
CFG = load()


def job_domains():
    return set(CFG["job"])


def noise_domains():
    return set(CFG["noise"])


def spam_domains():
    return set(CFG["spam"])


def spam_brands():
    """SPAM_PAT 里额外命中的品牌词。"""
    return sorted(CFG["spam_brand"])


def action_hints():
    """ACTION_HINT 里额外命中的平台词。"""
    return sorted(CFG["action_hint"])


def contest_keywords():
    """RE_COMPETITION 里额外命中的赛事关键词。

    公开赛事本身也算"我参加过什么"的画像，所以源码里只留通用词
    （大赛/竞赛/初赛/复赛…），具体赛事名放配置。
    """
    return sorted(CFG["contest"])
