from __future__ import annotations

import os
import re
from pathlib import Path
from collections.abc import Callable
from typing import Any

import requests

from daily_us.config import TelegramConfig


class TelegramClient:
    def __init__(self, config: TelegramConfig) -> None:
        self.bot_token = os.getenv(config.bot_token_env)
        self.chat_ids = _parse_chat_ids(
            os.getenv(config.chat_ids_env) or os.getenv(config.chat_id_env)
        )
        self.admin_chat_id = os.getenv(config.admin_chat_id_env) or (
            self.chat_ids[0] if self.chat_ids else None
        )
        if not self.bot_token or not self.chat_ids:
            raise RuntimeError(
                f"Set {config.bot_token_env} and {config.chat_ids_env} or "
                f"{config.chat_id_env} in your environment or .env file."
            )

    def send_audio(self, audio_path: Path, caption: str | None = None) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendAudio"

        def send_one(chat_id: str) -> None:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption

            with audio_path.open("rb") as audio:
                response = requests.post(
                    url,
                    data=data,
                    files={"audio": (audio_path.name, audio, "audio/mpeg")},
                    timeout=120,
                )
            _raise_for_telegram_error(response, "sendAudio")

        _send_to_chat_ids(self.chat_ids, send_one, "sendAudio")

    def send_document(self, document_path: Path, caption: str | None = None) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"

        def send_one(chat_id: str) -> None:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption

            with document_path.open("rb") as document:
                response = requests.post(
                    url,
                    data=data,
                    files={"document": (document_path.name, document, "application/pdf")},
                    timeout=120,
                )
            _raise_for_telegram_error(response, "sendDocument")

        _send_to_chat_ids(self.chat_ids, send_one, "sendDocument")

    def send_message(self, text: str, parse_mode: str | None = None) -> None:
        _send_to_chat_ids(
            self.chat_ids,
            lambda chat_id: self._send_message_to(chat_id, text, parse_mode=parse_mode),
            "sendMessage",
        )

    def send_admin_message(self, text: str, parse_mode: str | None = None) -> None:
        if not self.admin_chat_id:
            raise RuntimeError("Set the admin Telegram chat id environment variable.")
        self._send_message_to(self.admin_chat_id, text, parse_mode=parse_mode)

    def _send_message_to(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
    ) -> None:
        data = {"chat_id": chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode

        response = requests.post(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=data,
            timeout=30,
        )
        _raise_for_telegram_error(response, "sendMessage")

    def get_updates(self) -> list[dict[str, Any]]:
        response = requests.get(
            f"https://api.telegram.org/bot{self.bot_token}/getUpdates",
            timeout=30,
        )
        _raise_for_telegram_error(response, "getUpdates")
        return response.json().get("result", [])


def _parse_chat_ids(raw_value: str | None) -> tuple[str, ...]:
    if not raw_value:
        return ()
    return tuple(
        part.strip()
        for part in re.split(r"[,;\s]+", raw_value)
        if part.strip()
    )


def _send_to_chat_ids(
    chat_ids: tuple[str, ...],
    send_one: Callable[[str], None],
    method: str,
) -> None:
    errors = []
    for chat_id in chat_ids:
        try:
            send_one(chat_id)
        except Exception as exc:
            errors.append(f"{chat_id}: {exc}")

    if errors:
        joined_errors = "; ".join(errors)
        raise RuntimeError(f"Telegram {method} failed for {len(errors)} recipient(s): {joined_errors}")


def _raise_for_telegram_error(response: requests.Response, method: str) -> None:
    if response.ok:
        return

    details: dict[str, Any] | str
    try:
        details = response.json()
    except ValueError:
        details = response.text

    hint = ""
    if response.status_code == 403:
        description = ""
        if isinstance(details, dict):
            description = str(details.get("description", ""))
        if "can't send messages to the bot" in description:
            hint = (
                " TELEGRAM_CHAT_ID is the bot id, not your personal chat id. "
                "Send any message to the bot, then run `python -m daily_us telegram-updates` "
                "and copy the private chat id."
            )
        else:
            hint = (
                " For a personal chat, open the bot in Telegram, press Start, "
                "and make sure TELEGRAM_CHAT_ID belongs to that chat. "
                "If this token was exposed, revoke it with BotFather and create a new one."
            )
    raise RuntimeError(f"Telegram {method} failed: HTTP {response.status_code} {details}.{hint}")
