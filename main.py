"""Command-line entry point for the interactive BMS_AGENT."""

import sys

import bms_services
from bms_services import (
    AVAIL_STATUS_MAP,
    BOOKABLE_STATUSES,
    BMS_HEADERS,
    CITY_SLUG,
    DATE_STATUS_PRIORITY,
    DATE_STYLE_MAP,
    LISTING_URL,
    REGION_MAP,
    STATE_FILE,
    TELEGRAM_API_URL,
    CatInfo,
    DateInfo,
    MovieLinkParser,
    ShowInfo,
    _message_chunks,
    build_state,
    detect_changes,
    discover_movies,
    fetch_bms,
    load_state,
    parse_dates,
    parse_movie_info,
    parse_shows,
    resolve_region,
    save_state,
    scan_movie,
    send_telegram,
)

requests = bms_services.requests


def main():
    bms_services.main()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
