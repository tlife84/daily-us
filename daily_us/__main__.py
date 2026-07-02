from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

from daily_us.config import load_config
from daily_us.poller import (
    poll_once,
    DEFAULT_SEED_LIMIT,
    run_forever,
    seed_seen_posts,
    send_latest_body_for_test,
    send_latest_for_test,
)
from daily_us.site import UsInsightClient
from daily_us.telegram import TelegramClient


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be 1 or greater")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="US Insight audio poller")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config YAML file. Defaults to config.yaml.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console logging level.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("login", help="Open a browser and save the logged-in session.")
    subparsers.add_parser("check-login", help="Verify whether the saved session can open the feed.")
    subparsers.add_parser("telegram-updates", help="Print recent Telegram chat ids from getUpdates.")
    subparsers.add_parser("test-telegram", help="Send a small Telegram test message.")
    subparsers.add_parser("test-telegram-html", help="Send a small Telegram HTML formatting test.")
    subparsers.add_parser("test-telegram-markdown", help="Send a small Telegram MarkdownV2 formatting test.")
    test_latest_parser = subparsers.add_parser(
        "test-latest",
        help="Send the newest matching post without reading or writing seen history.",
    )
    test_latest_parser.add_argument(
        "--watcher",
        help="Watcher name to test. Defaults to all watchers.",
    )
    test_latest_parser.add_argument(
        "--limit",
        type=_positive_int,
        default=1,
        help="Number of recent matching posts to test. Defaults to 1.",
    )
    test_latest_parser.add_argument(
        "--admin",
        action="store_true",
        help="Send only to the admin chat instead of the normal recipients.",
    )
    test_latest_body_parser = subparsers.add_parser(
        "test-latest-body",
        help="Send only the newest matching post body without audio or seen history.",
    )
    test_latest_body_parser.add_argument(
        "--watcher",
        help="Watcher name to test. Defaults to all watchers.",
    )
    test_latest_body_parser.add_argument(
        "--limit",
        type=_positive_int,
        default=1,
        help="Number of recent matching posts to test. Defaults to 1.",
    )
    test_latest_body_parser.add_argument(
        "--admin",
        action="store_true",
        help="Send only to the admin chat instead of the normal recipients.",
    )
    poll_parser = subparsers.add_parser("poll", help="Run one poll immediately, ignoring active hours.")
    poll_parser.add_argument(
        "--watcher",
        help="Watcher name to poll. Defaults to all watchers.",
    )
    seed_seen_parser = subparsers.add_parser(
        "seed-seen",
        help="Mark recent matching posts as seen without sending them.",
    )
    seed_seen_parser.add_argument(
        "--watcher",
        help="Watcher name to seed. Defaults to all watchers.",
    )
    seed_seen_parser.add_argument(
        "--limit",
        type=_positive_int,
        default=DEFAULT_SEED_LIMIT,
        help=f"Number of recent matching posts to seed. Defaults to {DEFAULT_SEED_LIMIT}.",
    )
    subparsers.add_parser("run", help="Run continuously using watcher schedules.")

    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    load_dotenv()

    config = load_config(args.config)

    if args.command == "login":
        login_config = config.site.__class__(
            feed_url=config.site.feed_url,
            profile_dir=config.site.profile_dir,
            auth_state_path=config.site.auth_state_path,
            session_storage_path=config.site.session_storage_path,
            headless=False,
            navigation_timeout_ms=config.site.navigation_timeout_ms,
        )
        with UsInsightClient(login_config) as client:
            client.open_login_page()
    elif args.command == "check-login":
        with UsInsightClient(config.site) as client:
            verified, url = client._verify_feed_access()
            print(f"verified={verified}")
            print(f"url={url}")
    elif args.command == "telegram-updates":
        updates = TelegramClient(config.telegram).get_updates()
        if not updates:
            print("No updates. Send any message to your bot in Telegram, then run this again.")
        for update in updates:
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            name = " ".join(
                part for part in [sender.get("first_name"), sender.get("last_name")] if part
            )
            print(
                f"chat_id={chat.get('id')} "
                f"type={chat.get('type')} "
                f"username={sender.get('username')} "
                f"name={name}"
            )
    elif args.command == "test-telegram":
        TelegramClient(config.telegram).send_message("daily-us Telegram test")
        print("Telegram test message sent.")
    elif args.command == "test-telegram-html":
        TelegramClient(config.telegram).send_message(
            "<b>HTML 굵게 테스트</b>\n&lt;아침&gt; 문자는 꺾쇠로 보여야 합니다.",
            parse_mode="HTML",
        )
        print("Telegram HTML test message sent.")
    elif args.command == "test-telegram-markdown":
        TelegramClient(config.telegram).send_message(
            "*MarkdownV2 굵게 테스트*\n\\<아침\\> 문자는 그대로 보여야 합니다\\.",
            parse_mode="MarkdownV2",
        )
        print("Telegram MarkdownV2 test message sent.")
    elif args.command == "test-latest":
        send_latest_for_test(
            config,
            watcher_name=args.watcher,
            limit=args.limit,
            admin_only=args.admin,
        )
    elif args.command == "test-latest-body":
        send_latest_body_for_test(
            config,
            watcher_name=args.watcher,
            limit=args.limit,
            admin_only=args.admin,
        )
    elif args.command == "poll":
        poll_once(config, ignore_schedule=True, watcher_name=args.watcher)
    elif args.command == "seed-seen":
        seed_seen_posts(config, watcher_name=args.watcher, limit=args.limit)
    elif args.command == "run":
        run_forever(config)


if __name__ == "__main__":
    main()
