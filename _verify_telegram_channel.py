"""Does the alert actually reach BOTH destinations?

Run:  python _verify_telegram_channel.py

Posts a clearly-labelled TEST message through the real notifier and reports what
Telegram answered for each destination. Nothing here touches MT5, the scanner,
the strategy or the database — the subject is delivery only.

Two passes:
  1. The production path, with nothing injected: the notifier posts to the chat
     and mirrors to the channel on its own. Any mirror failure surfaces as a
     warning on the logger, which is captured here.
  2. A recording pass that wraps the channel send so the per-destination result
     and the two bodies can be compared directly.
"""
from __future__ import annotations

import logging
import sys

import config
from notifications.telegram import ALERT_ONLY_TAG, TelegramNotifier, _transport_telegram

BANNER = "[TEST] AI_Trading_bot — Telegram delivery check. Not a trade signal."


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage() % record.args
                          if record.args else record.getMessage())


def mask(token: str) -> str:
    return f"{token.split(':')[0]}:***" if token else "(unset)"


def main() -> int:
    cfg = config.get_settings()
    print(f"telegram_enabled      : {cfg.telegram_enabled}")
    print(f"bot token             : {mask(cfg.telegram_bot_token)}")
    print(f"primary chat id       : {cfg.telegram_chat_id}")
    print(f"configured channel id : {cfg.telegram_channel_id or '(blank — chat only)'}")

    notifier = TelegramNotifier(settings=cfg)
    if not notifier.enabled:
        print("\nTelegram is disabled (TELEGRAM_ENABLED / TELEGRAM_BOT_TOKEN). "
              "Nothing to verify.")
        return 1
    print(f"mirror active         : {bool(notifier.channel_id)} "
          f"(resolved channel: {notifier.channel_id or '-'})")

    capture = Capture()
    logging.getLogger("notifications.telegram").addHandler(capture)

    # --- pass 1: the production path, nothing injected ---------------------- #
    print("\n1. production notifier (no injection)")
    res = notifier.send_text(
        f"{BANNER}\nDestination: chat {cfg.telegram_chat_id} + channel "
        f"{notifier.channel_id or '-'}")
    print(f"   chat  -> ok={res.ok} http={res.http_status} error={res.error or '-'}")
    if capture.lines:
        for line in capture.lines:
            print(f"   mirror warning: {line}")
    else:
        print("   mirror -> no warning logged (channel accepted the post)")

    # --- pass 2: recording pass, same text both ways ------------------------ #
    print("\n2. recording pass (per-destination result + body comparison)")
    seen: dict[str, object] = {}

    def channel(text: str):
        seen["text"] = text
        result = _transport_telegram(cfg.telegram_bot_token, notifier.channel_id,
                                     text, cfg.telegram_timeout_seconds)
        seen["result"] = result
        return result

    chat_body: dict[str, str] = {}
    real_primary = notifier.transport

    def primary(text: str):
        chat_body["text"] = text
        return real_primary(text)

    recording = TelegramNotifier(settings=cfg,
                                 transport=primary,
                                 channel_transport=channel)
    result = recording.send_text(f"{BANNER} (second pass)")
    channel_result = seen["result"]

    print(f"   chat    -> ok={result.ok} http={result.http_status} "
          f"error={result.error or '-'}")
    print(f"   channel -> ok={channel_result.ok} http={channel_result.http_status} "
          f"error={channel_result.error or '-'}")
    identical = chat_body.get("text") == seen.get("text")
    print(f"   identical body at both destinations: {identical}")
    print(f"   alert-only tag present             : "
          f"{ALERT_ONLY_TAG in str(seen.get('text', ''))}")

    ok = bool(result.ok and channel_result.ok and identical)
    print(f"\nRESULT: {'both destinations received the message' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
