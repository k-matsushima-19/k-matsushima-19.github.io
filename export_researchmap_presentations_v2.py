#!/usr/bin/env python3
"""
Export ACS canonical publications JSON to researchmap JSONL
for 「講演・口頭発表等」.

This version keeps ALL localized fields of one presentation on a single
researchmap language side (ja or en). This matters because researchmap requires:

- presentation_title.ja if any other ja-side field is present
- presentation_title.en if any other en-side field is present

The previous exporter independently guessed ja/en for title, presenters, event,
and location, which could create mixed records such as:

    presentation_title.en
    presenters.ja
    event.en

and researchmap then demanded presentation_title.ja as well.

This exporter chooses one "record language" and consistently uses it for:
    presentation_title
    presenters
    event
    location

Language choice:
    1. tags contains "JP" -> ja
    2. tags contains "EN" -> en
    3. otherwise, Japanese characters in title -> ja
    4. otherwise -> en

Input:
    acs/publications.final.json

Output:
    researchmap_presentations.jsonl

By default researchmap `presenters` is populated from ACS `authors`
(all coauthors). Use --presenter-source presenters to export only the
actual speaker(s) extracted from legacy <u>.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable

TALK_TYPES = {"conference", "inproceedings", "proceedings", "presentation"}

PRESENTATION_TYPES = {
    "oral_presentation",
    "invited_oral_presentation",
    "keynote_oral_presentation",
    "poster_presentation",
    "public_symposium",
    "nominated_symposium",
    "public_discourse",
    "media_report",
    "others",
}

JP_RE = re.compile(r"[ぁ-んァ-ン一-龯々〆ヵヶ]")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def has_japanese(text: str) -> bool:
    return bool(JP_RE.search(text or ""))


def normalized_tags(record: dict[str, Any]) -> set[str]:
    tags = record.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        return set()
    return {str(x).strip().upper() for x in tags if str(x).strip()}


def record_language(record: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Return (researchmap localized key, ISO 639-3 code, warnings)."""
    warnings: list[str] = []
    tags = normalized_tags(record)
    title = str(record.get("title", "") or "")

    if "JP" in tags and "EN" not in tags:
        key = "ja"
    elif "EN" in tags and "JP" not in tags:
        key = "en"
    else:
        key = "ja" if has_japanese(title) else "en"

    # Informational warning only. We deliberately keep a single language side.
    if key == "ja" and not has_japanese(title):
        warnings.append(
            f"{record.get('id', '(no id)')}: language=ja from tags, "
            "but title contains no Japanese characters"
        )
    if key == "en" and has_japanese(title):
        warnings.append(
            f"{record.get('id', '(no id)')}: language=en from tags, "
            "but title contains Japanese characters"
        )

    return key, ("jpn" if key == "ja" else "eng"), warnings


def clean_name_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def validate_iso_date(value: str, label: str, record_id: str) -> None:
    if not ISO_DATE_RE.fullmatch(value):
        raise ValueError(f"{record_id}: {label} must be YYYY-MM-DD, got {value!r}")
    y, m, d = map(int, value.split("-"))
    try:
        date(y, m, d)
    except ValueError as exc:
        raise ValueError(f"{record_id}: invalid {label} {value!r}: {exc}") from exc


def load_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a JSON array")
    if not all(isinstance(x, dict) for x in data):
        raise ValueError(f"{path} contains a non-object array element")
    return data


def is_talk(record: dict[str, Any]) -> bool:
    return str(record.get("type", "")).strip().lower() in TALK_TYPES


def derive_invited(record: dict[str, Any]) -> bool:
    pt = str(record.get("presentation_type", "") or "").strip()
    return bool(record.get("invited")) or pt in {
        "invited_oral_presentation",
        "keynote_oral_presentation",
    }


def make_researchmap_merge(
    record: dict[str, Any],
    *,
    presenter_source: str,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []

    rid = str(record.get("id", "(no local id)"))
    title = str(record.get("title", "") or "").strip()
    if not title:
        raise ValueError(f"{rid}: title is required")

    lang_key, lang_code, lang_warnings = record_language(record)
    warnings.extend(lang_warnings)

    event = str(record.get("venue", "") or "").strip()
    location = str(record.get("location", "") or "").strip()

    if presenter_source == "authors":
        names = clean_name_list(record.get("authors"))
        if not names:
            names = clean_name_list(record.get("presenters"))
            warnings.append(f"{rid}: authors missing; fell back to presenters")
    else:
        names = clean_name_list(record.get("presenters"))
        if not names:
            names = clean_name_list(record.get("authors"))
            warnings.append(f"{rid}: presenters missing; fell back to authors")

    # IMPORTANT:
    # Use exactly one localized side for ALL localized fields in this record.
    merge: dict[str, Any] = {
        "display": "disclosed",
        "presentation_title": {lang_key: title},
        "languages": [lang_code],
    }

    if names:
        merge["presenters"] = {
            lang_key: [{"name": name} for name in names]
        }
    else:
        warnings.append(f"{rid}: no authors/presenters; presenters omitted")

    if event:
        merge["event"] = {lang_key: event}

    if location:
        merge["location"] = {lang_key: location}

    start = str(record.get("start_date", "") or "").strip()
    end = str(record.get("end_date", "") or "").strip()

    if start:
        validate_iso_date(start, "start_date", rid)
        merge["from_event_date"] = start
    if end:
        validate_iso_date(end, "end_date", rid)
        merge["to_event_date"] = end
    if start and end and end < start:
        raise ValueError(f"{rid}: end_date {end} precedes start_date {start}")

    # Only when the event is one day long do we know the actual presentation day.
    if start and end and start == end:
        merge["publication_date"] = start

    pt = str(record.get("presentation_type", "") or "").strip()
    if pt:
        if pt not in PRESENTATION_TYPES:
            raise ValueError(
                f"{rid}: invalid presentation_type {pt!r}; allowed: "
                + ", ".join(sorted(PRESENTATION_TYPES))
            )
        merge["presentation_type"] = pt
    else:
        warnings.append(f"{rid}: presentation_type is blank; field omitted")

    merge["invited"] = derive_invited(record)

    return merge, warnings


def localized_sides_used(merge: dict[str, Any]) -> set[str]:
    """Return ja/en sides used by localized fields."""
    used: set[str] = set()
    for field in ("presentation_title", "presenters", "event", "promoter", "location", "description"):
        value = merge.get(field)
        if isinstance(value, dict):
            for key in ("ja", "en"):
                if key in value:
                    v = value[key]
                    if v not in (None, "", [], {}):
                        used.add(key)
    return used


def validate_researchmap_title_rule(merge: dict[str, Any], record_id: str) -> None:
    """Local validation mirroring researchmap's localized-title requirement."""
    used = localized_sides_used(merge)
    titles = merge.get("presentation_title", {})
    if not isinstance(titles, dict):
        raise ValueError(f"{record_id}: presentation_title must be an object")

    if not titles.get("ja") and not titles.get("en"):
        raise ValueError(f"{record_id}: either presentation_title.ja or .en is required")

    if "ja" in used and not titles.get("ja"):
        raise ValueError(
            f"{record_id}: ja-side field exists but presentation_title.ja is missing"
        )
    if "en" in used and not titles.get("en"):
        raise ValueError(
            f"{record_id}: en-side field exists but presentation_title.en is missing"
        )


def iter_talks(records: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for rec in records:
        if is_talk(rec):
            yield rec


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=Path("acs/publications.final.json"),
        help="ACS canonical JSON array (default: acs/publications.final.json)",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("researchmap_presentations.jsonl"),
        help="researchmap JSONL output (default: researchmap_presentations.jsonl)",
    )
    p.add_argument(
        "--presenter-source",
        choices=("authors", "presenters"),
        default="authors",
        help=(
            "authors=all coauthors (default); "
            "presenters=actual speaker(s) extracted from legacy <u>"
        ),
    )
    p.add_argument(
        "--only-with-presentation-type",
        action="store_true",
        help="skip talk records whose presentation_type is blank",
    )
    p.add_argument(
        "--check-only",
        action="store_true",
        help="validate and print a summary without writing JSONL",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        records = load_records(args.input)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    talk_records = list(iter_talks(records))
    output_lines: list[str] = []
    all_warnings: list[str] = []
    skipped_missing_type = 0
    ja_records = 0
    en_records = 0

    try:
        for rec in talk_records:
            if args.only_with_presentation_type and not str(
                rec.get("presentation_type", "") or ""
            ).strip():
                skipped_missing_type += 1
                continue

            merge, warnings = make_researchmap_merge(
                rec,
                presenter_source=args.presenter_source,
            )

            rid = str(rec.get("id", "(no id)"))
            validate_researchmap_title_rule(merge, rid)

            title_obj = merge["presentation_title"]
            if "ja" in title_obj:
                ja_records += 1
            else:
                en_records += 1

            request = {
                "insert": {"type": "presentations"},
                "merge": merge,
            }

            # JSON Lines: exactly one complete JSON object per physical line.
            line = json.dumps(request, ensure_ascii=False, separators=(",", ":"))

            # Serialize/deserialize self-check.
            decoded = json.loads(line)
            validate_researchmap_title_rule(decoded["merge"], rid)

            output_lines.append(line)
            all_warnings.extend(warnings)

    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not args.check_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            "\n".join(output_lines) + ("\n" if output_lines else ""),
            encoding="utf-8",
        )

    print(f"Input records            : {len(records)}")
    print(f"Talk records             : {len(talk_records)}")
    print(f"Researchmap JSONL records: {len(output_lines)}")
    print(f"Japanese-side records    : {ja_records}")
    print(f"English-side records     : {en_records}")
    if args.only_with_presentation_type:
        print(f"Skipped (type blank)     : {skipped_missing_type}")
    print(f"Presenter source         : {args.presenter_source}")
    if args.check_only:
        print("Output                    : not written (--check-only)")
    else:
        print(f"Output                    : {args.output}")

    if all_warnings:
        print(f"Warnings                  : {len(all_warnings)}")
        for w in all_warnings:
            print(f"  - {w}")
    else:
        print("Warnings                  : 0")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
