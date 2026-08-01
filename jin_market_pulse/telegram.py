from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from typing import Any

import requests

from .config import Settings


TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
SAFE_PART_LIMIT = 3800
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "현재 시장"}, {"text": "오늘 일정"}],
        [{"text": "이번 주"}, {"text": "왜 움직여?"}],
        [{"text": "최근 리포트"}, {"text": "상태"}],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": True,
    "is_persistent": False,
    "input_field_placeholder": "시장 질문을 입력하세요",
}

REMOVE_KEYBOARD = {"remove_keyboard": True}

BOT_COMMANDS = [
    {"command": "start", "description": "처음 화면과 질문 안내"},
    {"command": "menu", "description": "빠른 버튼 열기"},
    {"command": "brief", "description": "모닝 리포트 다시 보기"},
    {"command": "price", "description": "자산 가격 조회"},
    {"command": "compare", "description": "두 자산 변동 비교"},
    {"command": "markets", "description": "지원 자산 전체 조회"},
    {"command": "calendar", "description": "앞으로 24시간 일정"},
    {"command": "week", "description": "이번 주 중요 일정"},
    {"command": "last", "description": "최근 알림과 리포트"},
    {"command": "alerts", "description": "내 가격 알림 관리"},
    {"command": "mute", "description": "자동 알림 잠시 끄기"},
    {"command": "settings", "description": "알림 설정"},
    {"command": "status", "description": "봇과 데이터 상태"},
    {"command": "reset", "description": "최근 대화 연결만 초기화"},
    {"command": "help", "description": "질문 예시와 사용법"},
]


def _plain_from_html(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "", value)
    return html.unescape(without_tags)


def split_message(text: str, limit: int = SAFE_PART_LIMIT) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]

    paragraphs = [
        paragraph.strip()
        for paragraph in text.split("\n\n")
        if paragraph.strip()
    ]
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


def split_html_message(text: str, limit: int = SAFE_PART_LIMIT) -> list[str]:
    if len(text.strip()) <= limit:
        return [text.strip()]
    # Long generated responses are sent as safe plain text. Routine HTML messages
    # are deliberately capped below this path, so mobile formatting is preserved.
    return [
        html.escape(part)
        for part in split_message(_plain_from_html(text), limit=limit)
    ]


class TelegramClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

    def _call(
        self,
        method: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> Any:
        response = requests.post(
            f"{self.base_url}/{method}",
            json=json_body,
            data=data,
            files=files,
            timeout=timeout or self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API rejected {method}")
        return payload.get("result")

    def send(
        self,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
        disable_notification: bool = False,
    ) -> list[int]:
        parts = (
            split_html_message(text)
            if parse_mode == "HTML"
            else split_message(text)
        )
        total = len(parts)
        message_ids: list[int] = []
        for index, part in enumerate(parts, start=1):
            prefix = f"[{index}/{total}]\n" if total > 1 else ""
            payload_text = prefix + part
            if len(payload_text) > TELEGRAM_TEXT_LIMIT:
                raise ValueError("Telegram message part exceeds 4096 characters")
            request_body: dict[str, Any] = {
                "chat_id": chat_id or self.settings.telegram_chat_id,
                "text": payload_text,
                "disable_web_page_preview": True,
                "disable_notification": disable_notification,
            }
            if parse_mode:
                request_body["parse_mode"] = parse_mode
            if reply_markup and index == total:
                request_body["reply_markup"] = reply_markup
            if reply_to_message_id and index == 1:
                request_body["reply_parameters"] = {
                    "message_id": reply_to_message_id,
                    "allow_sending_without_reply": True,
                }
            result = self._call("sendMessage", json_body=request_body)
            if isinstance(result, dict) and isinstance(result.get("message_id"), int):
                message_ids.append(int(result["message_id"]))
            logging.info("Telegram message part %s/%s sent.", index, total)
        return message_ids

    def send_photo(
        self,
        content: bytes,
        *,
        filename: str = "btc-24h.png",
        caption: str = "",
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        if len(caption) > TELEGRAM_CAPTION_LIMIT:
            raise ValueError("Telegram photo caption exceeds 1024 characters")
        data: dict[str, Any] = {"chat_id": self.settings.telegram_chat_id}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup:
            import json

            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        result = self._call(
            "sendPhoto",
            data=data,
            files={"photo": (filename, content, "image/png")},
            timeout=max(self.settings.request_timeout_seconds, 30),
        )
        logging.info("Telegram BTC chart sent.")
        if isinstance(result, dict) and isinstance(result.get("message_id"), int):
            return int(result["message_id"])
        return None

    def edit(
        self,
        message_id: int,
        text: str,
        *,
        parse_mode: str = "HTML",
    ) -> bool:
        try:
            self._call(
                "editMessageText",
                json_body={
                    "chat_id": self.settings.telegram_chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
            return True
        except Exception:
            logging.warning("Telegram message edit failed.", exc_info=True)
            return False

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        body: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text[:200]
        self._call("answerCallbackQuery", json_body=body)

    def send_action(self, action: str = "typing") -> None:
        self._call(
            "sendChatAction",
            json_body={
                "chat_id": self.settings.telegram_chat_id,
                "action": action,
            },
        )

    def get_file(self, file_id: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
        result = self._call("getFile", json_body={"file_id": file_id})
        if not isinstance(result, dict) or not result.get("file_path"):
            raise RuntimeError("Telegram file metadata missing")
        file_size = int(result.get("file_size") or 0)
        if file_size and file_size > max_bytes:
            raise ValueError("Telegram file is too large")
        response = requests.get(
            (
                "https://api.telegram.org/file/bot"
                f"{self.settings.telegram_bot_token}/{result['file_path']}"
            ),
            timeout=max(self.settings.request_timeout_seconds, 30),
            stream=True,
        )
        response.raise_for_status()
        content = response.content
        if len(content) > max_bytes:
            raise ValueError("Telegram file is too large")
        return content

    def set_commands(self) -> None:
        self._call("setMyCommands", json_body={"commands": BOT_COMMANDS})

    def set_webhook(self, public_base_url: str, secret_token: str) -> None:
        if not public_base_url.startswith("https://"):
            raise ValueError("Telegram webhook requires a public HTTPS URL")
        self._call(
            "setWebhook",
            json_body={
                "url": f"{public_base_url.rstrip('/')}/telegram/webhook",
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
            },
        )

    def webhook_info(self) -> dict[str, Any]:
        result = self._call("getWebhookInfo")
        return result if isinstance(result, dict) else {}

    def get_me(self) -> dict[str, Any]:
        result = self._call("getMe")
        return result if isinstance(result, dict) else {}


def write_temp_media(directory: Path, name: str, content: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return path
