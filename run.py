#!/usr/bin/env python3
"""Entrypoint for the CloudBox Discord bot."""

from __future__ import annotations

import logging
import sys


def main() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[assignment]

    if load_dotenv is not None:
        load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    from cloudbox.bot import CloudBoxBot
    from cloudbox.config import load_settings

    settings = load_settings()
    if not settings.token:
        sys.exit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if not settings.owners and settings.staff_role_id is None:
        logging.warning("no OWNER_IDS and no STAFF_ROLE_ID configured: nobody can use /staff")

    bot = CloudBoxBot(settings)
    try:
        bot.run(settings.token, log_handler=None)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
