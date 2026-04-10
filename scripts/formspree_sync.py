#!/usr/bin/env python3
"""
formspree_sync.py — Formspreeの未読問い合わせをNotionに登録してDiscordに通知する
GitHub Actionsから15分おきに実行される
"""

import json
import os
import sys
from datetime import datetime

import requests

FORMSPREE_API_KEY  = os.environ["FORMSPREE_API_KEY"]
FORMSPREE_FORM_ID  = os.environ["FORMSPREE_FORM_ID"]
NOTION_API_TOKEN   = os.environ["NOTION_API_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
DISCORD_WEBHOOK    = os.environ["DISCORD_WEBHOOK_URL"]

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def fetch_submissions() -> list[dict]:
    """Formspree APIから未読のsubmissionを取得"""
    resp = requests.get(
        f"https://formspree.io/api/0/forms/{FORMSPREE_FORM_ID}/submissions",
        headers={"Authorization": f"Bearer {FORMSPREE_API_KEY}"},
        params={"page_size": 20},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    submissions = data.get("submissions", [])
    # 未読のみ処理（Formspreeはread/unreadフラグを持つ）
    return [s for s in submissions if not s.get("_read", False)]


def register_to_notion(sub: dict) -> str:
    """NotionのDB案件テーブルに登録してページURLを返す"""
    fields = sub.get("body", sub)
    client   = fields.get("company") or fields.get("name", "不明")
    contact  = fields.get("name", "")
    email    = fields.get("email", "")
    service  = fields.get("service", "その他")
    message  = fields.get("message", "")
    today    = datetime.now().strftime("%Y-%m-%d")

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "名前":       {"title": [{"text": {"content": f"{client} — {service}"}}]},
            "クライアント": {"rich_text": [{"text": {"content": client}}]},
            "担当者":      {"rich_text": [{"text": {"content": f"{contact} <{email}>"}}]},
            "種別":        {"select": {"name": service}},
            "ステータス":  {"select": {"name": "商談中"}},
            "問い合わせ日": {"date": {"start": today}},
            "メモ":        {"rich_text": [{"text": {"content": message[:2000]}}]},
        },
    }
    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("url", "")


def notify_discord(sub: dict, notion_url: str) -> None:
    """Discordに新規問い合わせ通知を送る"""
    fields  = sub.get("body", sub)
    client  = fields.get("company") or fields.get("name", "不明")
    service = fields.get("service", "その他")
    message = fields.get("message", "")[:100]

    text = (
        f"**新規問い合わせ**\n"
        f"クライアント: {client}\n"
        f"サービス: {service}\n"
        f"内容: {message}...\n"
        f"Notion: {notion_url}"
    )
    requests.post(DISCORD_WEBHOOK, json={"content": text}, timeout=10)


def mark_as_read(submission_id: str) -> None:
    """Formspreeでsubmissionを既読にする"""
    requests.patch(
        f"https://formspree.io/api/0/forms/{FORMSPREE_FORM_ID}/submissions/{submission_id}",
        headers={"Authorization": f"Bearer {FORMSPREE_API_KEY}"},
        json={"_read": True},
        timeout=10,
    )


def main():
    submissions = fetch_submissions()
    if not submissions:
        print("新規問い合わせなし")
        return

    print(f"{len(submissions)}件の新規問い合わせを処理します")
    for sub in submissions:
        sid = sub.get("_id") or sub.get("id", "")
        try:
            notion_url = register_to_notion(sub)
            notify_discord(sub, notion_url)
            if sid:
                mark_as_read(sid)
            fields = sub.get("body", sub)
            print(f"[OK] {fields.get('name', '?')} → Notion登録 + Discord通知")
        except Exception as e:
            print(f"[ERROR] {sid}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
