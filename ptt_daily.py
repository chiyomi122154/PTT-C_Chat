#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PTT C_Chat 每日爬蟲 + LINE 推播
--------------------------------
每天早上 08:00 執行，抓取「前一日」的文章，整理後用 LINE Messaging API 推播。

用法：
    python ptt_daily.py                    # 抓前一天，並推 LINE
    python ptt_daily.py --dry-run          # 只印在畫面上，不推 LINE
    python ptt_daily.py --date 2026-07-25  # 補抓指定日期
    python ptt_daily.py --min-push 30 --limit 30
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv 非必要
    pass

# ---------------------------------------------------------------- 設定 ----

BASE_URL = "https://www.ptt.cc"
BOARD = "C_Chat"
TZ = ZoneInfo("Asia/Taipei")

# 抓取區間：使用整天 00:00:00 ~ 23:59:59。
# （你原本寫 00:01，但那樣會漏掉剛好 00:00 發的文，所以這裡取整天。）
DAY_START = "00:00:00"
DAY_END = "23:59:59"

MIN_PUSH = int(os.getenv("MIN_PUSH", "20"))       # 推文數門檻，低於此不列入
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "25"))     # LINE 訊息最多列幾篇
MAX_PAGES = int(os.getenv("MAX_PAGES", "80"))     # 最多往回翻幾頁（安全上限）
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.4"))  # 每次請求間隔（秒）
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))

LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TO = os.getenv("LINE_USER_ID", "")
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ptt")


# ------------------------------------------------------------ 工具函式 ----

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # 部分看板（如 Gossiping）需要年齡確認，C_Chat 不需要，但帶著無害
    s.cookies.set("over18", "1", domain=".ptt.cc")
    return s


def fetch(session: requests.Session, url: str, retries: int = 3) -> str | None:
    """帶重試的 GET。"""
    for i in range(retries):
        try:
            r = session.get(url, timeout=15)
            if r.status_code == 404:
                log.warning("404 Not Found: %s", url)
                return None
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text
        except requests.RequestException as e:
            wait = 2 ** i
            log.warning("請求失敗 (%s/%s) %s：%s，%s 秒後重試", i + 1, retries, url, e, wait)
            time.sleep(wait)
    log.error("放棄請求：%s", url)
    return None


def parse_push_count(text: str) -> int:
    """把 PTT 的推文標記轉成數字：爆=100, XX=-100, X5=-50, 空白=0。"""
    text = (text or "").strip()
    if not text:
        return 0
    if text == "爆":
        return 100
    if text == "XX":
        return -100
    if text.startswith("X"):
        try:
            return -int(text[1:]) * 10
        except ValueError:
            return -100
    try:
        return int(text)
    except ValueError:
        return 0


def ts_from_href(href: str) -> datetime | None:
    """
    從文章網址取得發文時間。
    PTT 文章 ID 格式：M.1753712345.A.1B2 -> 中間那段是 unix timestamp。
    這比去抓每篇文章的 meta 快非常多，也不會對站方造成負擔。
    """
    m = re.search(r"/M\.(\d{9,11})\.A\.", href)
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)), tz=TZ)


def prev_page_url(soup: BeautifulSoup) -> str | None:
    for a in soup.select("div.btn-group-paging a.btn"):
        if "上頁" in a.get_text():
            href = a.get("href")
            return BASE_URL + href if href else None
    return None


# -------------------------------------------------------------- 爬蟲 ----

def parse_index(html: str) -> tuple[list[dict], BeautifulSoup]:
    """解析列表頁，回傳文章清單與 soup（供翻頁使用）。"""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []

    for ent in soup.select("div.r-ent"):
        a = ent.select_one("div.title a")
        if not a:  # 已被刪除的文章沒有連結
            continue
        href = a.get("href", "")
        posted = ts_from_href(href)
        if posted is None:
            continue

        author_el = ent.select_one("div.meta div.author")
        nrec_el = ent.select_one("div.nrec")

        items.append({
            "title": a.get_text(strip=True),
            "url": BASE_URL + href,
            "author": author_el.get_text(strip=True) if author_el else "",
            "push": parse_push_count(nrec_el.get_text() if nrec_el else ""),
            "posted_at": posted,
        })

    return items, soup


def crawl_day(session: requests.Session, target: date) -> list[dict]:
    """由最新一頁往回翻，收集 target 當日（00:00:00~23:59:59）的所有文章。"""
    start = datetime.strptime(f"{target} {DAY_START}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    end = datetime.strptime(f"{target} {DAY_END}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)

    url = f"{BASE_URL}/bbs/{BOARD}/index.html"
    collected: dict[str, dict] = {}
    pages = 0

    while url and pages < MAX_PAGES:
        html = fetch(session, url)
        if html is None:
            break

        items, soup = parse_index(html)
        pages += 1

        if items:
            newest = max(i["posted_at"] for i in items)
            oldest = min(i["posted_at"] for i in items)
            log.info("第 %s 頁：%s ~ %s（%s 篇）",
                     pages,
                     oldest.strftime("%m/%d %H:%M"),
                     newest.strftime("%m/%d %H:%M"),
                     len(items))

            for it in items:
                if start <= it["posted_at"] <= end:
                    collected[it["url"]] = it

            # 整頁都比目標日還舊 -> 已經翻過頭，結束
            if newest < start:
                log.info("已翻至目標日期之前，停止翻頁")
                break

        url = prev_page_url(soup)
        time.sleep(REQUEST_DELAY)

    result = sorted(collected.values(), key=lambda x: x["posted_at"])
    log.info("共翻 %s 頁，%s 當日取得 %s 篇文章", pages, target, len(result))
    return result


# ------------------------------------------------------- 產出與推播 ----

def build_messages(articles: list[dict], target: date) -> list[str]:
    """整理成 LINE 文字訊息（自動分段，每段 < 4800 字）。"""
    hot = [a for a in articles if a["push"] >= MIN_PUSH]
    hot.sort(key=lambda x: (-x["push"], x["posted_at"]))
    hot = hot[:MAX_ITEMS]

    header = (
        f"📅 PTT {BOARD} {target:%Y/%m/%d} 日報\n"
        f"全日共 {len(articles)} 篇 ｜ 推文 ≥ {MIN_PUSH} 有 "
        f"{len([a for a in articles if a['push'] >= MIN_PUSH])} 篇\n"
        f"{'─' * 15}"
    )

    if not hot:
        return [header + "\n\n今天沒有符合門檻的文章。"]

    blocks = []
    for i, a in enumerate(hot, 1):
        push = "爆" if a["push"] >= 100 else str(a["push"])
        blocks.append(
            f"{i}. [{push}推] {a['title']}\n"
            f"   {a['author']} ｜ {a['posted_at']:%H:%M}\n"
            f"   {a['url']}"
        )

    messages, current = [], header
    for b in blocks:
        if len(current) + len(b) + 2 > 4800:
            messages.append(current)
            current = b
        else:
            current += "\n\n" + b
    messages.append(current)
    return messages


def send_line(messages: list[str]) -> bool:
    if not LINE_TOKEN or not LINE_TO:
        log.error("缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID，無法推播")
        return False

    headers = {
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json",
    }
    ok = True
    # LINE 單次 push 最多 5 則
    for i in range(0, len(messages), 5):
        batch = messages[i:i + 5]
        payload = {"to": LINE_TO, "messages": [{"type": "text", "text": m} for m in batch]}
        try:
            r = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=15)
            if r.status_code != 200:
                log.error("LINE 推播失敗 %s：%s", r.status_code, r.text)
                ok = False
            else:
                log.info("LINE 推播成功（%s 則）", len(batch))
        except requests.RequestException as e:
            log.error("LINE 推播例外：%s", e)
            ok = False
    return ok


def save_json(articles: list[dict], target: date) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{BOARD}_{target:%Y%m%d}.json"
    payload = [
        {**a, "posted_at": a["posted_at"].isoformat()}
        for a in articles
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已存檔：%s", path)
    return path


# --------------------------------------------------------------- main ----

def main(argv: Iterable[str] | None = None) -> int:
    global MIN_PUSH, MAX_ITEMS

    p = argparse.ArgumentParser(description="PTT C_Chat 每日爬蟲 + LINE 推播")
    p.add_argument("--date", help="指定日期 YYYY-MM-DD（預設為昨天）")
    p.add_argument("--dry-run", action="store_true", help="只輸出到畫面，不推 LINE")
    p.add_argument("--min-push", type=int, help=f"推文數門檻（預設 {MIN_PUSH}）")
    p.add_argument("--limit", type=int, help=f"最多列幾篇（預設 {MAX_ITEMS}）")
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.min_push is not None:
        MIN_PUSH = args.min_push
    if args.limit is not None:
        MAX_ITEMS = args.limit

    if args.date:
        try:
            target = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            log.error("日期格式錯誤，請用 YYYY-MM-DD")
            return 2
    else:
        target = datetime.now(TZ).date() - timedelta(days=1)

    log.info("開始爬取 %s 看板 %s 的文章", BOARD, target)

    session = make_session()
    articles = crawl_day(session, target)

    if not articles:
        log.warning("沒有抓到任何文章，可能是網路問題或版面結構變動")

    save_json(articles, target)
    messages = build_messages(articles, target)

    if args.dry_run:
        print("\n" + ("=" * 40))
        for m in messages:
            print(m)
            print("=" * 40)
        return 0

    return 0 if send_line(messages) else 1


if __name__ == "__main__":
    sys.exit(main())
