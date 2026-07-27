from __future__ import annotations

import logging

import requests

from .config import Settings


TELEGRAM_TEXT_LIMIT = 4096
SAFE_PART_LIMIT = 3800


def split_message(text: str, limit: int = SAFE_PART_LIMIT) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]

    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
    parts: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        while len(paragraph) > limit:
            split_at = paragraph.rfind("\n", 0, limit)
            if split_at < limit // 2:
                split_at = paragraph.rfind(" ", 0, limit)
            if split_at < limit // 2:
                split_at = limit
            parts.append(paragraph[:split_at].rstrip())
            paragraph = paragraph[split_at:].lstrip()
        current = paragraph
    if current:
        parts.append(current)
    return parts


class TelegramClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

    def send(self, text: str) -> None:
        parts = split_message(text)
        total = len(parts)
        for index, part in enumerate(parts, start=1):
            prefix = f"[{index}/{total}]\n" if total > 1 else ""
            payload = prefix + part
            if len(payload) > TELEGRAM_TEXT_LIMIT:
                raise ValueError("Telegram message part exceeds 4096 characters")
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": payload,
                    "disable_web_page_preview": True,
                },
                timeout=self.settings.request_timeout_seconds,
            )
            response.raise_for_status()
            logging.info("Telegram message part %s/%s sent.", index, total)
