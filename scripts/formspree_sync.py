#!/usr/bin/env python3
"""Formspree lead sync preparation; external delivery is fail-closed.

See docs/lead-sync-operations.md before enabling any delivery implementation.
"""

import os
import sys
from datetime import datetime
from urllib.parse import quote

import requests

FORMSPREE_API_KEY  = os.environ.get("FORMSPREE_API_KEY", "")
FORMSPREE_FORM_ID  = os.environ.get("FORMSPREE_FORM_ID", "")
NOTION_API_TOKEN   = os.environ.get("NOTION_API_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
DISCORD_WEBHOOK    = os.environ.get("DISCORD_WEBHOOK_URL", "")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


class SyncFailure(Exception):
    """Internal failure category; never include submission data in its message."""


def require_durable_delivery_contract() -> None:
    """Block before HTTP until persistent claims and ambiguous sends are handled.

    No environment flag can bypass this guard. A separate reviewed implementation
    must supply durable, atomic delivery state before this guard can be replaced.
    """
    raise SyncFailure("durable_delivery_contract_missing")


def submission_id(sub: dict) -> str:
    if not isinstance(sub, dict):
        raise SyncFailure("invalid_submission")
    sid = sub.get("_id") or sub.get("id")
    if not isinstance(sid, str) or not sid.strip():
        raise SyncFailure("submission_id_missing")
    if not isinstance(sub.get("body", sub), dict):
        raise SyncFailure("invalid_submission")
    return sid


def preflight_submissions(submissions: list[dict]) -> None:
    """Validate the whole batch before the first write, including duplicate IDs."""
    if not isinstance(submissions, list):
        raise SyncFailure("invalid_response")
    seen = set()
    for sub in submissions:
        sid = submission_id(sub)
        if sid in seen:
            raise SyncFailure("duplicate_submission")
        seen.add(sid)


def fetch_submissions() -> list[dict]:
    """Formspree APIから未読のsubmissionを取得"""
    resp = requests.get(
        f"https://formspree.io/api/0/forms/{quote(FORMSPREE_FORM_ID, safe='')}/submissions",
        headers={"Authorization": f"Bearer {FORMSPREE_API_KEY}"},
        params={"page_size": 20},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise SyncFailure("invalid_response")
    submissions = data.get("submissions")
    preflight_submissions(submissions)
    # 未読のみ処理（Formspreeはread/unreadフラグを持つ）
    return [s for s in submissions if not s.get("_read", False)]


def register_to_notion(sub: dict) -> str:
    """NotionのDB案件テーブルに登録してページURLを返す"""
    submission_id(sub)
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
    data = resp.json()
    if not isinstance(data, dict) or not isinstance(data.get("url"), str) or not data["url"]:
        # Creation may already have happened. Never retry this ambiguous outcome.
        raise SyncFailure("invalid_response")
    return data["url"]


def notify_discord(sub: dict, notion_url: str) -> None:
    """Discordに新規問い合わせ通知を送る"""
    submission_id(sub)
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
    resp = requests.post(DISCORD_WEBHOOK, json={"content": text}, timeout=10)
    resp.raise_for_status()


def mark_as_read(sid: str) -> None:
    """Formspreeでsubmissionを既読にする"""
    submission_id({"id": sid})
    resp = requests.patch(
        f"https://formspree.io/api/0/forms/{quote(FORMSPREE_FORM_ID, safe='')}/submissions/{quote(sid, safe='')}",
        headers={"Authorization": f"Bearer {FORMSPREE_API_KEY}"},
        json={"_read": True},
        timeout=10,
    )
    resp.raise_for_status()


def sync_submissions(submissions: list[dict]) -> None:
    preflight_submissions(submissions)
    require_durable_delivery_contract()
    # This sequence is unreachable in production until the durable contract is
    # implemented. Helpers remain separately testable with offline transports.
    for sub in submissions:
        sid = submission_id(sub)
        notion_url = register_to_notion(sub)
        notify_discord(sub, notion_url)
        mark_as_read(sid)


def report_failure(error: Exception) -> None:
    # Never print an exception string, response body, URL, submission or ID.
    allowed = {
        "configuration_missing", "durable_delivery_contract_missing",
        "submission_id_missing", "invalid_submission", "duplicate_submission",
        "invalid_response",
    }
    if (isinstance(error, SyncFailure) and error.args
            and isinstance(error.args[0], str) and error.args[0] in allowed):
        category = error.args[0]
    elif isinstance(error, requests.exceptions.Timeout):
        category = "request_outcome_uncertain"
    elif isinstance(error, requests.exceptions.HTTPError):
        category = "http_failure"
    elif isinstance(error, requests.exceptions.RequestException):
        category = "request_failure"
    else:
        category = "unexpected_failure"
    print(f"[ERROR] lead_sync: {category}", file=sys.stderr)


def main() -> int:
    try:
        if not all(value.strip() for value in (
            FORMSPREE_API_KEY, FORMSPREE_FORM_ID, NOTION_API_TOKEN,
            NOTION_DATABASE_ID, DISCORD_WEBHOOK,
        )):
            raise SyncFailure("configuration_missing")
        require_durable_delivery_contract()
        submissions = fetch_submissions()
        if not submissions:
            print("[OK] lead_sync: no_unread_submissions")
            return 0
        sync_submissions(submissions)
        print("[OK] lead_sync: delivery_complete")
        return 0
    except Exception as error:
        report_failure(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
