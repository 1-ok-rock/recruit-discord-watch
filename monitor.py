#!/usr/bin/env python3
"""Public official pages -> meaningful-change detection -> Discord webhook.

This is a conservative keyword-based watcher, not an LLM-based extractor.
Only detected changes are sent; dates/eligibility not confidently extracted are *not* invented.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlparse, urldefrag

from xml.etree import ElementTree as ET
from email.utils import parsedate_to_datetime
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "state.json"
SOURCE_FILE = ROOT / "sources.yaml"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobWatch/1.0; personal-interest monitor)", "Accept-Language": "ja,en;q=0.7"}
TIMEOUT = (8, 25)
CJK_YEAR = re.compile(r"(?:2028\s*(?:年|年度|卒)|28\s*卒|'28\b|class\s+of\s+2028|new\s*grad(?:uate)?\s*2028)", re.I)
RECRUITMENT = re.compile(r"本選考|正社員|新卒採用|応募受付|応募開始|募集開始|エントリー受付|エントリー締切|締切|締め切り|選考開始|採用募集|新卒募集|採用職種|(?:full.?time|graduate|new.?grad).{0,20}(?:open|apply|application|recruit)", re.I)
INTERNSHIP = re.compile(r"インターン|internship|インターンシップ|夏季プログラム|サマープログラム", re.I)
LOW_VALUE = re.compile(r"プライバシー|利用規約|Cookie|サインイン|ログイン|個人情報|問い合わせ|お問い合わせ", re.I)
DEVELOP_ONLY = re.compile(r"(?:Software Engineer|SWE|ソフトウェアエンジニア|プログラマー|コーディングテスト|研究開発エンジニア)", re.I)
EVENT = re.compile(r"新卒|学生|説明会|座談会|交流会|採用イベント|\bSA\b|Solutions? Architect|セミナー|ウェビナー|イベント|workshop|summit|jumpstart|ハンズオン|トレーニング|勉強会|技術セッション|re:Invent", re.I)
EVENT_ACTION = re.compile(r"開催|申し込み|申込|参加登録|募集|受付|\d{1,2}月\d{1,2}日|20\d\d年|event|register|webinar|join|summit|jumpstart|workshop", re.I)
AWS_NEWS = re.compile(r"\b(?:EC2|VPC|RDS|S3|IAM|EKS|ECS|AWS\s+Lambda|Route\s*53|CloudFront|ELB|ALB|Well.Architected|Aurora|Auto\s*Scaling|CloudWatch|Amazon\s*Bedrock|AWS\s*Security|Direct\s*Connect)\b|セキュリティ|アーキテクチャ|ネットワーク|コスト最適化", re.I)
DATE_PAT = re.compile(r"\b20\d{2}[./\-年]\s*\d{1,2}(?:[./\-月]\s*\d{1,2}日?)?\b|\b\d{1,2}月\s*\d{1,2}日\b|\b\d{1,2}/\d{1,2}\b")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def safe_url(value: str, base: str) -> str:
    url = urldefrag(urljoin(base, value or ""))[0]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return base
    return url


def digest(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]


def stable_title(s: str) -> str:
    # Makes an item ID survive a changed deadline without misclassifying it as a new announcement.
    s = DATE_PAT.sub("(date)", s.lower())
    s = re.sub(r"\d{1,2}:\d{2}|\s+", " ", s).strip()
    return s[:150]


def make_item(src, title, url, detail):
    title, detail = clean(title)[:165], clean(detail)[:500]
    if not title or not detail:
        return None
    url = safe_url(url, src["url"])
    key = digest(src["id"] + "|" + url + "|" + stable_title(title))
    fingerprint = digest(detail)
    return {"id": key, "category": src["type"], "company": src["name"], "title": title,
            "url": url, "detail": detail, "fingerprint": fingerprint}


def is_relevant(src, text):
    if LOW_VALUE.search(text) and not RECRUITMENT.search(text):
        return False
    kind = src["type"]
    if kind == "recruitment":
        has_year = bool(CJK_YEAR.search(text) or src.get("year_context"))
        return bool(has_year and RECRUITMENT.search(text)
                    and not INTERNSHIP.search(text) and not DEVELOP_ONLY.search(text))
    if kind == "aws_recruiting_event":
        return bool(EVENT.search(text) and EVENT_ACTION.search(text)
                    and re.search(r"採用|新卒|学生|説明会|座談会|交流会|recruit", text, re.I)
                    and not re.fullmatch(r"未定[\s|]*未定.*", text))
    if kind == "aws_event":
        return bool(EVENT.search(text) and EVENT_ACTION.search(text)
                    and not re.fullmatch(r"未定.*", text))
    if kind == "aws_news":
        return bool(AWS_NEWS.search(text))
    return False


def extract_html(source, body):
    soup = BeautifulSoup(body, "html.parser")
    for x in soup(["script", "style", "noscript", "nav", "footer", "svg"]):
        x.decompose()
    results = {}
    # Limit to short, human-authored notice cards. Whole-page diffs create noisy alerts.
    for tag in soup.select("article, tr, li, p, h2, h3, h4, a"):
        text = clean(tag.get_text(" ", strip=True))
        if not 16 <= len(text) <= 420:
            continue
        if not is_relevant(source, text):
            continue
        a = tag if tag.name == "a" else tag.find("a", href=True)
        url = a.get("href") if a is not None else source["url"]
        # Title should remain stable when a deadline on the same card changes.
        title = (clean(a.get_text(" ", strip=True)) if a is not None else "") or clean(tag.get("aria-label", ""))
        if len(title) < 12 or not (RECRUITMENT.search(title) or EVENT.search(title)):
            title = re.split(r"(?:締切|応募期間|開催日|募集期間|募集締切|20\d\d[.年/\-])", text, maxsplit=1)[0][:115]
        title = clean(title) or text[:85]
        item = make_item(source, title, url, text)
        if item:
            if item["id"] not in results or len(item["detail"]) > len(results[item["id"]]["detail"]):
                results[item["id"]] = item
    return list(results.values())


def extract_rss(source, content):
    root = ET.fromstring(content)
    atom = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(".//item") or root.findall(f".//{atom}entry")
    results = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    def findtext(node, name):
        return clean(node.findtext(name) or "")
    for ent in entries[:100]:
        title = findtext(ent, "title") or findtext(ent, f"{atom}title")
        if not is_relevant(source, title):
            continue
        when_txt = (findtext(ent, "pubDate") or findtext(ent, f"{atom}published")
                    or findtext(ent, f"{atom}updated"))
        if when_txt:
            try:
                try:
                    when = parsedate_to_datetime(when_txt)
                except ValueError:
                    when = datetime.fromisoformat(when_txt.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
        link = findtext(ent, "link")
        if not link:
            for a in ent.findall(f"{atom}link"):
                if a.attrib.get("rel") in (None, "alternate"):
                    link = a.attrib.get("href", "")
                    if link:
                        break
        body = findtext(ent, "description") or findtext(ent, f"{atom}summary")
        summary = clean(BeautifulSoup(body, "html.parser").get_text(" ", strip=True))[:350]
        item = make_item(source, title, link or source["url"], title + " — " + summary)
        if item:
            results[item["id"]] = item
    return list(results.values())


def retrieve(session, source):
    response = session.get(source["url"], timeout=TIMEOUT)
    response.raise_for_status()
    if source.get("format") == "rss":
        return extract_rss(source, response.content)
    return extract_html(source, response.text)


def get_webhook():
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    parts = urlparse(url)
    if parts.scheme != "https" or parts.hostname not in ("discord.com", "discordapp.com") \
            or not re.match(r"^/api(?:/v\d+)?/webhooks/\d+/[^/]+/?$", parts.path):
        raise ValueError("GitHub Actions Secret DISCORD_WEBHOOK_URL が未設定、またはDiscordのWebhook URLではありません")
    return url


def post_discord(session, hook, item, changed=False):
    labels = {"recruitment": "28卒・本選考", "aws_recruiting_event": "AWS採用イベント",
              "aws_event": "AWSイベント", "aws_news": "AWS技術ニュース"}
    label = labels.get(item["category"], item["category"])
    desc = ("🔄 内容・日程が更新されました\n" if changed else "🆕 新しい情報を検出しました\n")
    desc += item["detail"][:700]
    payload = {"username": "28卒・AWS Watch", "allowed_mentions": {"parse": []},
               "embeds": [{"title": f"【{label}】{item['company']}：{item['title']}"[:250],
                           "url": item["url"], "description": desc[:1100],
                           "color": 2664261 if item["category"] == "recruitment" else 3903436,
                           "footer": {"text": "公式ページの変化を自動検出。応募資格・締切はリンク先で最終確認"}}]}
    for attempt in range(4):
        r = session.post(hook, json=payload, timeout=TIMEOUT)
        if r.status_code in (200, 204):
            return
        if r.status_code == 429 and attempt < 3:
            try:
                pause = float(r.json().get("retry_after", 2))
            except (ValueError, KeyError, TypeError):
                pause = 2
            time.sleep(min(pause + 0.5, 15))
            continue
        r.raise_for_status()
    raise RuntimeError("Discordへの送信ができませんでした")


def run():
    hook = get_webhook()   # Don't mutate state without a valid configured destination.
    sources = yaml.safe_load(SOURCE_FILE.read_text(encoding="utf-8"))["sources"]
    state = json.loads(STATE.read_text(encoding="utf-8"))
    old_sources = state.setdefault("sources", {})
    session = requests.Session()
    session.headers.update(HEADERS)
    sent = 0
    healthy = 0
    state_changed = False
    failed = []
    for source in sources:
        sid = source["id"]
        try:
            items = retrieve(session, source)
        except Exception as exc:
            logging.warning("取得失敗（状態は維持） %s: %s", sid, exc)
            failed.append(sid)
            continue
        healthy += 1
        prev = old_sources.get(sid)
        previous_items = (prev or {}).get("items", {})
        first = prev is None
        changes = []
        for item in items:
            before = previous_items.get(item["id"])
            if first:
                continue  # First success establishes baseline, sends no historical notices.
            if before is None or before["fingerprint"] != item["fingerprint"]:
                changes.append((item, before is not None))
        # prioritize job deadlines; constrain low-value AWS news volume
        cap = 3 if source["type"] == "aws_news" else (8 if source["type"] == "aws_event" else 15)
        for item, changed in changes[:cap]:
            post_discord(session, hook, item, changed)
            sent += 1
            time.sleep(0.25)
        # Preserve old IDs to avoid spamming if temporarily removed and re-added.
        updated_items = {**previous_items, **{item["id"]: item for item in items}}
        if len(updated_items) > 300:
            updated_items = dict(list(updated_items.items())[-300:])
        if first or previous_items != updated_items:
            old_sources[sid] = {"items": updated_items, "last_ok": datetime.now(timezone.utc).isoformat(),
                                "latest_count": len(items)}
            state_changed = True
        logging.info("%s: 検出=%d 新規または更新=%d (%s)", sid, len(items), len(changes), "初回ベースライン" if first else "通常")
    if healthy == 0:
        raise RuntimeError("すべての情報源へのアクセスに失敗しました。状態は更新しません")
    if state_changed:
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logging.info("処理終了 成功ソース=%d/全%d 通知=%d 失敗=%s", healthy, len(sources), sent, ", ".join(failed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-webhook", action="store_true", help="Discordにテスト投稿。採用ソースは取得しない")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.test_webhook:
        s = requests.Session()
        post_discord(s, get_webhook(), {"category": "aws_recruiting_event", "company": "接続テスト",
                                   "title": "Discord連携成功", "url": "https://aws.amazon.com/jp/careers/newgraduate/",
                                   "detail": "GitHub Actions から Discord Webhook への送信テストに成功しました。"})
        print("Discord テスト通知を送信しました。")
    else:
        run()


if __name__ == "__main__":
    main()
