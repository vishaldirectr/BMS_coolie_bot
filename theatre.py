"""Stable internal theatre identity helpers."""

import hashlib


def format_date(value):
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[6:8]}-{text[4:6]}-{text[:4]}"
    return text or "Date unknown"


def theatre_identifier(venue_code, venue_name):
    code = str(venue_code or "").strip()
    name = str(venue_name or "").strip()
    if not code or not name or name.casefold() == "unknown":
        return ""
    identity = f"{code}\0{name}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:16]


def theatre_location(additional_data):
    for key in (
        "venueArea",
        "venueLocation",
        "venueAddress",
        "area",
        "location",
        "address",
    ):
        value = additional_data.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""
