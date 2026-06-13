from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests

from daily_us.config import TelegramConfig


class TelegramClient:
    def __init__(self, config: TelegramConfig) -> None:
        self.bot_token = os.getenv(config.bot_token_env)
        self.chat_id = os.getenv(config.chat_id_env)
        if not self.bot_token or not self.chat_id:
            raise RuntimeError(
                f"Set {config.bot_token_env} and {config.chat_id_env} in your environment or .env file."
            )

    def send_audio(self, audio_path: Path, caption: str | None = None) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendAudio"
        data = {"chat_id": self.chat_id}
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

    def send_message(self, text: str, parse_mode: str | None = None) -> None:
        data = {"chat_id": self.chat_id, "text": text}
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
