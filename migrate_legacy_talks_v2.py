#!/usr/bin/env python3
"""One-shot migration of the legacy <h2>講演 ...</h2> list in index.html.

The script parses the legacy talk list, merges it into acs/publications.json, and
writes a *new* JSON file by default. Existing records are never overwritten
blindly: when a conference record with the same title/year is found, curated
fields are preserved and only missing migration fields are added.

The review CSV contains a ``researchmap_presentation_type`` column. Fill that
column manually with one of the researchmap labels (or API codes) and run the
second command below to write the choice back to ``presentation_type`` in JSON.

No third-party packages are required.

Stage 1 -- parse legacy HTML and create a review CSV::

    python migrate_legacy_talks.py \
        --html index.html \
        --publications acs/publications.json \
        --output acs/publications.migrated.json \
        --review-csv acs/talks_migration_review.csv

Stage 2 -- after editing the CSV, apply researchmap presentation types::

    python migrate_legacy_talks.py \
        --apply-review-csv acs/talks_migration_review.csv \
        --publications acs/publications.migrated.json \
        --output acs/publications.final.json

Use ``--list-presentation-types`` to print accepted labels and API codes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


TALK_TYPES = {"conference", "inproceedings", "proceedings", "presentation"}
DASHES_RE = re.compile(r"[‐‑‒–—―−ーｰ~〜～]")
SPACE_RE = re.compile(r"\s+")
JP_RE = re.compile(r"[ぁ-んァ-ン一-龯々〆ヵヶ]")
YEAR_IN_TEXT_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")

# researchmap「講演・口頭発表等」> 種別
# CSVには左側の日本語ラベル、または右側のAPIコードのどちらを入力してもよい。
RESEARCHMAP_PRESENTATION_TYPES: dict[str, str] = {
    "口頭発表（一般）": "oral_presentation",
    "口頭発表（招待・特別）": "invited_oral_presentation",
    "口頭発表（基調）": "keynote_oral_presentation",
    "ポスター発表": "poster_presentation",
    "シンポジウム・ワークショップ パネル（公募）": "public_symposium",
    "シンポジウム・ワークショップ パネル（指名）": "nominated_symposium",
    "公開講演、セミナー、チュートリアル、講習、講義等": "public_discourse",
    "メディア報道等": "media_report",
    "その他": "others",
}
RESEARCHMAP_UNSPECIFIED_LABEL = "-- 指定なし --"



@dataclass
class LegacyTalk:
    number: int | None
    authors: list[str]
    presenters: list[str]
    title: str
    event: str
    location: str
    start_date: str | None
    end_date: str | None
    raw_date: str
    raw_venue_text: str
    invited: bool = False
    keynote: bool = False
    poster: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def year(self) -> int | None:
        if self.start_date:
            return int(self.start_date[:4])
        m = re.search(r"(20\d{2})年", self.raw_date)
        return int(m.group(1)) if m else None

    @property
    def month(self) -> int | None:
        if self.start_date:
            return int(self.start_date[5:7])
        m = re.search(r"20\d{2}年(\d{1,2})月", self.raw_date)
        return int(m.group(1)) if m else None


@dataclass
class _RawLI:
    number: int | None
    text_parts: list[str] = field(default_factory=list)
    presenters: list[str] = field(default_factory=list)
    _underline_parts: list[str] | None = None


class LegacyTalkHTMLParser(HTMLParser):
    """Extract <li> entries under an h2 whose text starts with '講演'."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_h2 = False
        self._h2_parts: list[str] = []
        self._in_talk_section = False
        self._current_li: _RawLI | None = None
        self._items: list[_RawLI] = []
        self._underline_depth = 0

    @property
    def items(self) -> list[_RawLI]:
        return self._items

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)
        if tag == "h2":
            self._in_h2 = True
            self._h2_parts = []
            return

        if tag == "li" and self._in_talk_section and self._current_li is None:
            value = attrs_dict.get("value")
            try:
                number = int(value) if value is not None else None
            except ValueError:
                number = None
            self._current_li = _RawLI(number=number)
            return

        if self._current_li is not None:
            if tag == "br":
                self._current_li.text_parts.append("\n")
            elif tag == "u":
                self._underline_depth += 1
                if self._underline_depth == 1:
                    self._current_li._underline_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "h2" and self._in_h2:
            heading = clean_text("".join(self._h2_parts))
            self._in_talk_section = heading.startswith("講演")
            self._in_h2 = False
            self._h2_parts = []
            return

        if self._current_li is not None and tag == "u":
            if self._underline_depth == 1 and self._current_li._underline_parts is not None:
                presenter = clean_text("".join(self._current_li._underline_parts))
                if presenter:
                    self._current_li.presenters.append(presenter)
                self._current_li._underline_parts = None
            self._underline_depth = max(0, self._underline_depth - 1)
            return

        if tag == "li" and self._current_li is not None:
            self._items.append(self._current_li)
            self._current_li = None
            self._underline_depth = 0

    def handle_data(self, data: str) -> None:
        if self._in_h2:
            self._h2_parts.append(data)
        if self._current_li is not None:
            self._current_li.text_parts.append(data)
            if self._underline_depth > 0 and self._current_li._underline_parts is not None:
                self._current_li._underline_parts.append(data)


def clean_text(value: str) -> str:
    return SPACE_RE.sub(" ", value.replace("\xa0", " ")).strip()


def normalize_for_match(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = DASHES_RE.sub("-", value)
    value = value.casefold()
    # Remove whitespace and punctuation that commonly differs between curated JSON and HTML.
    return re.sub(r"[\s\"'“”‘’()（）\[\]{}・,:：;；._/\\&＆-]+", "", value)


def split_authors(text: str) -> list[str]:
    text = clean_text(text).strip(" ,，")
    if not text:
        return []
    return [clean_text(x) for x in re.split(r"[,，]", text) if clean_text(x)]


def _iso(y: int, m: int, d: int) -> str:
    return date(y, m, d).isoformat()


def parse_japanese_date_range(raw: str) -> tuple[str | None, str | None, list[str]]:
    """Parse common date forms used by the legacy HTML.

    Supported examples:
      2026年9月15–17日
      2024年6月30–7月5日
      2023年5月31日–6月4日
      2022年11月1日
      2026年12月31日–2027年1月2日
    """
    warnings: list[str] = []
    s = unicodedata.normalize("NFKC", clean_text(raw))
    s = DASHES_RE.sub("-", s)
    s = re.sub(r"\s+", "", s)

    patterns: list[tuple[re.Pattern[str], Any]] = [
        (
            re.compile(
                r"^(?P<y1>\d{4})年(?P<m1>\d{1,2})月(?P<d1>\d{1,2})日?-"
                r"(?P<y2>\d{4})年(?P<m2>\d{1,2})月(?P<d2>\d{1,2})日$"
            ),
            lambda g: (
                int(g["y1"]), int(g["m1"]), int(g["d1"]),
                int(g["y2"]), int(g["m2"]), int(g["d2"]),
            ),
        ),
        (
            re.compile(
                r"^(?P<y>\d{4})年(?P<m1>\d{1,2})月(?P<d1>\d{1,2})日?-"
                r"(?P<m2>\d{1,2})月(?P<d2>\d{1,2})日$"
            ),
            lambda g: (
                int(g["y"]), int(g["m1"]), int(g["d1"]),
                int(g["y"]), int(g["m2"]), int(g["d2"]),
            ),
        ),
        (
            re.compile(
                r"^(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d1>\d{1,2})日?-"
                r"(?P<d2>\d{1,2})日$"
            ),
            lambda g: (
                int(g["y"]), int(g["m"]), int(g["d1"]),
                int(g["y"]), int(g["m"]), int(g["d2"]),
            ),
        ),
        (
            re.compile(r"^(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日$"),
            lambda g: (
                int(g["y"]), int(g["m"]), int(g["d"]),
                int(g["y"]), int(g["m"]), int(g["d"]),
            ),
        ),
    ]

    for pattern, extractor in patterns:
        m = pattern.match(s)
        if not m:
            continue
        try:
            y1, m1, d1, y2, m2, d2 = extractor(m.groupdict())
            start = _iso(y1, m1, d1)
            end = _iso(y2, m2, d2)
            if end < start:
                warnings.append(f"end date precedes start date: {raw}")
            return start, end, warnings
        except ValueError as exc:
            warnings.append(f"invalid date {raw!r}: {exc}")
            return None, None, warnings

    warnings.append(f"unparsed date: {raw}")
    return None, None, warnings


def extract_date_suffix(details: str) -> tuple[str, str]:
    """Return (venue_text, date_text), assuming a Japanese date sits at the end."""
    details = clean_text(details).strip(" ,，")
    # Find a Japanese-style year that begins a *parseable full date suffix*.  We scan
    # left-to-right: this skips event-name years such as "2026年度年会", while preserving
    # the first year of a cross-year range such as 2026年12月31日–2027年1月2日.
    matches = list(re.finditer(r"20\d{2}年", details))
    if not matches:
        return details, ""
    for m in matches:
        candidate = details[m.start():].strip(" ,，")
        if not re.fullmatch(r"20\d{2}年.*日", candidate):
            continue
        start, end, warnings = parse_japanese_date_range(candidate)
        if start is not None and end is not None and not any(w.startswith("unparsed date") for w in warnings):
            venue = details[:m.start()].strip(" ,，")
            return venue, candidate
    return details, ""


def split_event_location(raw_venue: str) -> tuple[str, str]:
    parts = [clean_text(p) for p in re.split(r"[,，]", raw_venue) if clean_text(p)]
    if not parts:
        return "", ""
    return parts[0], ", ".join(parts[1:])


def detect_markers(after_title: str) -> tuple[str, bool, bool, bool]:
    invited = bool(re.search(r"(?:\(|（)\s*(?:invited|招待講演?|招待)\s*(?:\)|）)", after_title, re.I))
    keynote = bool(re.search(r"(?:\(|（)\s*(?:keynote|基調講演?|基調)\s*(?:\)|）)", after_title, re.I))
    poster = bool(re.search(r"(?:\(|（)\s*(?:poster|ポスター)\s*(?:\)|）)", after_title, re.I))
    cleaned = re.sub(
        r"(?:\(|（)\s*(?:invited|招待講演?|招待|keynote|基調講演?|基調|poster|ポスター)\s*(?:\)|）)",
        " ",
        after_title,
        flags=re.I,
    )
    return clean_text(cleaned), invited, keynote, poster


def raw_li_to_talk(item: _RawLI) -> LegacyTalk | None:
    full = clean_text("".join(item.text_parts))
    title_match = re.search(r'["“](.*?)["”]', full)
    if not title_match:
        return None

    authors_text = full[: title_match.start()]
    title = clean_text(title_match.group(1))
    after = full[title_match.end():]
    after, invited, keynote, poster = detect_markers(after)

    raw_venue, raw_date = extract_date_suffix(after)
    event, location = split_event_location(raw_venue)
    start_date, end_date, date_warnings = parse_japanese_date_range(raw_date) if raw_date else (
        None,
        None,
        ["date suffix not found"],
    )

    talk = LegacyTalk(
        number=item.number,
        authors=split_authors(authors_text),
        presenters=[clean_text(x) for x in item.presenters if clean_text(x)],
        title=title,
        event=event,
        location=location,
        start_date=start_date,
        end_date=end_date,
        raw_date=raw_date,
        raw_venue_text=raw_venue,
        invited=invited,
        keynote=keynote,
        poster=poster,
        warnings=list(date_warnings),
    )

    if not talk.authors:
        talk.warnings.append("authors not parsed")
    if not talk.presenters:
        talk.warnings.append("presenter (<u>) not found")
    if not talk.event:
        talk.warnings.append("event not parsed")

    if talk.year:
        years_in_event = {int(y) for y in YEAR_IN_TEXT_RE.findall(talk.event)}
        if years_in_event and talk.year not in years_in_event:
            talk.warnings.append(
                f"event name contains year(s) {sorted(years_in_event)} but date starts in {talk.year}"
            )
    return talk


def parse_legacy_talks(html_text: str) -> tuple[list[LegacyTalk], list[str]]:
    parser = LegacyTalkHTMLParser()
    parser.feed(html_text)

    talks: list[LegacyTalk] = []
    global_warnings: list[str] = []
    for item in parser.items:
        talk = raw_li_to_talk(item)
        if talk is None:
            global_warnings.append(f"talk #{item.number or '?'}: quoted title not found; skipped")
            continue
        talks.append(talk)

    numbers = [t.number for t in talks if t.number is not None]
    if numbers:
        duplicated = sorted({n for n in numbers if numbers.count(n) > 1})
        if duplicated:
            global_warnings.append(f"duplicate legacy talk numbers: {duplicated}")
        expected = set(range(min(numbers), max(numbers) + 1))
        missing = sorted(expected - set(numbers))
        if missing:
            global_warnings.append(f"missing legacy talk numbers: {missing}")

    return talks, global_warnings


def language_tag(title: str) -> str:
    return "JP" if JP_RE.search(title) else "EN"


def infer_type(talk: LegacyTalk) -> str:
    # Keep the current ACS convention: Japanese-named meetings -> 国内会議,
    # otherwise -> 国際会議. This is a heuristic and is visible in review CSV.
    return "conference" if JP_RE.search(talk.event) else "inproceedings"


def default_researchmap_presentation_type(talk: LegacyTalk) -> str:
    """Prefill only when the legacy HTML explicitly marks the presentation type.

    Most legacy entries have no researchmap presentation-type information, so
    they intentionally remain blank for manual review.
    """
    if talk.keynote:
        return "口頭発表（基調）"
    if talk.invited:
        return "口頭発表（招待・特別）"
    if talk.poster:
        return "ポスター発表"
    return ""


def _presentation_choice_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", "", value).casefold()


def parse_researchmap_presentation_type(value: str) -> str | None:
    """Return a researchmap API code, '' for explicit deletion, None for blank.

    Accepted inputs are either the Japanese labels used by the researchmap UI
    or the API codes. A blank cell means 'leave the JSON unchanged'.
    ``-- 指定なし --`` (or ``指定なし``) explicitly removes presentation_type.
    """
    raw = clean_text(value)
    if not raw:
        return None

    key = _presentation_choice_key(raw)
    unspecified = {
        _presentation_choice_key(RESEARCHMAP_UNSPECIFIED_LABEL),
        _presentation_choice_key("指定なし"),
    }
    if key in unspecified:
        return ""

    for label, code in RESEARCHMAP_PRESENTATION_TYPES.items():
        if key == _presentation_choice_key(label) or key == _presentation_choice_key(code):
            return code

    allowed = ", ".join(RESEARCHMAP_PRESENTATION_TYPES)
    raise ValueError(
        f"unknown researchmap presentation type {value!r}; "
        f"use one of: {allowed}, or an API code"
    )


def stable_legacy_id(talk: LegacyTalk) -> str:
    if talk.number is not None:
        return f"legacy-talk-{talk.number:04d}"
    basis = "|".join([talk.title, talk.event, talk.start_date or talk.raw_date])
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]
    return f"legacy-talk-{digest}"


def talk_to_record(talk: LegacyTalk) -> dict[str, Any]:
    tags = [language_tag(talk.title)]
    if talk.invited:
        tags.append("invited")
    if talk.keynote:
        tags.append("keynote")
    if talk.poster:
        tags.append("poster")

    record: dict[str, Any] = {
        "id": stable_legacy_id(talk),
        "title": talk.title,
        "authors": talk.authors,
        "year": talk.year,
        "month": talk.month,
        "type": infer_type(talk),
        "venue": talk.event,
        "tags": tags,
        # Fields below are intentionally richer than the current ACS renderer needs.
        # The renderer ignores them, while a future researchmap exporter can use them.
        "presenters": talk.presenters,
        "location": talk.location,
        "start_date": talk.start_date,
        "end_date": talk.end_date,
        "invited": talk.invited,
        "keynote": talk.keynote,
        "poster": talk.poster,
        "legacy_talk_number": talk.number,
        "legacy_venue_text": talk.raw_venue_text,
        "legacy_date_text": talk.raw_date,
    }
    return {k: v for k, v in record.items() if v not in (None, "", [], False)}


def _record_year(record: dict[str, Any]) -> int | None:
    try:
        return int(record.get("year")) if record.get("year") is not None else None
    except (TypeError, ValueError):
        return None


def find_existing_match(talk: LegacyTalk, records: list[dict[str, Any]]) -> tuple[int | None, str | None]:
    title_key = normalize_for_match(talk.title)
    event_key = normalize_for_match(talk.event)
    year = talk.year

    candidates: list[tuple[int, dict[str, Any]]] = []
    for i, rec in enumerate(records):
        if str(rec.get("type", "")).lower() not in TALK_TYPES:
            continue
        if normalize_for_match(str(rec.get("title", ""))) != title_key:
            continue
        if year is not None and _record_year(rec) not in (None, year):
            continue
        candidates.append((i, rec))

    if not candidates:
        return None, None

    exact_event = [
        (i, rec)
        for i, rec in candidates
        if normalize_for_match(str(rec.get("venue", ""))) == event_key
    ]
    if len(exact_event) == 1:
        return exact_event[0][0], "title+year+venue"
    if len(exact_event) > 1:
        return None, f"ambiguous: {len(exact_event)} exact title/year/venue matches"

    if talk.start_date:
        exact_date = [(i, rec) for i, rec in candidates if rec.get("start_date") == talk.start_date]
        if len(exact_date) == 1:
            return exact_date[0][0], "title+year+start_date"

    if len(candidates) == 1:
        return candidates[0][0], "unique title+year"

    return None, f"ambiguous: {len(candidates)} title/year matches"


def merge_tags(existing: Any, derived: Iterable[str]) -> list[str]:
    result: list[str] = []
    for item in list(existing or []) + list(derived):
        value = str(item)
        if value and value not in result:
            result.append(value)
    return result


def enrich_existing(existing: dict[str, Any], talk: LegacyTalk) -> list[str]:
    """Enrich a curated existing record without replacing curated content."""
    changed: list[str] = []
    derived = talk_to_record(talk)

    # Do not overwrite these curated fields if they already exist.
    fill_if_missing = [
        "year",
        "month",
        "presenters",
        "location",
        "start_date",
        "end_date",
        "legacy_talk_number",
        "legacy_venue_text",
        "legacy_date_text",
    ]
    for key in fill_if_missing:
        if key in derived and key not in existing:
            existing[key] = derived[key]
            changed.append(key)

    for key in ("invited", "keynote", "poster"):
        if derived.get(key) and not existing.get(key):
            existing[key] = True
            changed.append(key)

    new_tags = merge_tags(existing.get("tags", []), derived.get("tags", []))
    if new_tags != existing.get("tags", []):
        existing["tags"] = new_tags
        changed.append("tags")

    return changed


def migrate(
    talks: list[LegacyTalk], existing: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    output = [dict(rec) for rec in existing]
    review_rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    for talk in talks:
        idx, reason = find_existing_match(talk, output)
        if idx is not None:
            changed = enrich_existing(output[idx], talk)
            status = "matched+enriched" if changed else "matched"
            record_id = output[idx].get("id", "")
            match_reason = reason or ""
        else:
            new_record = talk_to_record(talk)
            output.append(new_record)
            status = "new"
            record_id = new_record.get("id", "")
            match_reason = reason or "no existing match"
            if reason and reason.startswith("ambiguous"):
                warnings.append(f"talk #{talk.number or '?'} {talk.title}: {reason}; added as new")

        row = {
            "legacy_number": talk.number or "",
            "status": status,
            "record_id": record_id,
            "match_reason": match_reason,
            "type_inferred": infer_type(talk),
            "researchmap_presentation_type": default_researchmap_presentation_type(talk),
            "title": talk.title,
            "authors": " | ".join(talk.authors),
            "presenters": " | ".join(talk.presenters),
            "event": talk.event,
            "location": talk.location,
            "start_date": talk.start_date or "",
            "end_date": talk.end_date or "",
            "invited": talk.invited,
            "keynote": talk.keynote,
            "poster": talk.poster,
            "warnings": " | ".join(talk.warnings),
            "raw_venue_text": talk.raw_venue_text,
            "raw_date": talk.raw_date,
        }
        review_rows.append(row)

    return output, review_rows, warnings


def write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def apply_review_csv(
    path: Path, records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int, int, list[str]]:
    """Apply ``researchmap_presentation_type`` choices from review CSV to JSON.

    Rows are matched by ``record_id``. Blank cells are ignored, while
    ``-- 指定なし --`` removes an existing ``presentation_type`` field.
    """
    output = [dict(rec) for rec in records]
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for rec in output:
        rid = clean_text(str(rec.get("id", "")))
        if not rid:
            continue
        if rid in by_id:
            duplicate_ids.add(rid)
        by_id[rid] = rec

    warnings: list[str] = []
    if duplicate_ids:
        warnings.append(f"duplicate JSON record id(s): {sorted(duplicate_ids)}")

    changed = 0
    removed = 0
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path} has no CSV header")
        required = {"record_id", "researchmap_presentation_type"}
        missing = required - set(reader.fieldnames)
        if missing:
            if "researchmap_presentation_type" in missing:
                raise ValueError(
                    f"{path} does not contain 'researchmap_presentation_type'. "
                    "It was probably generated by an older version of this script; "
                    "regenerate the review CSV first."
                )
            raise ValueError(f"{path} is missing required column(s): {sorted(missing)}")

        for line_no, row in enumerate(reader, start=2):
            record_id = clean_text(row.get("record_id", ""))
            raw_choice = row.get("researchmap_presentation_type", "")
            try:
                code = parse_researchmap_presentation_type(raw_choice)
            except ValueError as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc

            # Blank means 'not reviewed / keep as-is'.
            if code is None:
                continue
            if not record_id:
                warnings.append(f"CSV line {line_no}: record_id is blank; skipped")
                continue
            rec = by_id.get(record_id)
            if rec is None:
                warnings.append(
                    f"CSV line {line_no}: record_id {record_id!r} not found in JSON; skipped"
                )
                continue

            if code == "":
                if "presentation_type" in rec:
                    del rec["presentation_type"]
                    removed += 1
                continue

            if rec.get("presentation_type") != code:
                rec["presentation_type"] = code
                changed += 1

    return output, changed, removed, warnings


def load_json_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise ValueError(f"{path} must contain a JSON array of objects")
    return data


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--html", type=Path, default=Path("index.html"), help="legacy HTML (default: index.html)")
    p.add_argument(
        "--publications",
        type=Path,
        default=Path("acs/publications.json"),
        help="current canonical JSON to enrich (default: acs/publications.json)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path("acs/publications.migrated.json"),
        help="output JSON; deliberately differs from input by default",
    )
    p.add_argument(
        "--review-csv",
        type=Path,
        default=Path("acs/talks_migration_review.csv"),
        help="human-review CSV (default: acs/talks_migration_review.csv)",
    )
    p.add_argument(
        "--apply-review-csv",
        type=Path,
        default=None,
        help=(
            "apply researchmap_presentation_type values from an edited review CSV "
            "to --publications and write --output"
        ),
    )
    p.add_argument(
        "--list-presentation-types",
        action="store_true",
        help="print accepted researchmap presentation-type labels and API codes",
    )
    p.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="exit non-zero if any parser/migration warning is found",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.list_presentation_types:
        print(RESEARCHMAP_UNSPECIFIED_LABEL)
        for label, code in RESEARCHMAP_PRESENTATION_TYPES.items():
            print(f"{label}\t{code}")
        return 0

    if args.output.resolve() == args.publications.resolve():
        print(
            "ERROR: --output must differ from --publications for this one-shot migration. "
            "Review the result before replacing the canonical file.",
            file=sys.stderr,
        )
        return 2

    if args.apply_review_csv is not None:
        try:
            existing = load_json_list(args.publications)
            updated, changed, removed, apply_warnings = apply_review_csv(
                args.apply_review_csv, existing
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Input JSON records  : {len(existing)}")
        print(f"Types set/changed   : {changed}")
        print(f"Types removed       : {removed}")
        print(f"Wrote JSON          : {args.output}")
        if apply_warnings:
            print(f"Warnings            : {len(apply_warnings)}")
            for warning in apply_warnings:
                print(f"  - {warning}")
        else:
            print("Warnings            : 0")
        return 1 if args.fail_on_warning and apply_warnings else 0

    html_text = args.html.read_text(encoding="utf-8")
    existing = load_json_list(args.publications)
    talks, parser_warnings = parse_legacy_talks(html_text)
    migrated, review_rows, migration_warnings = migrate(talks, existing)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(migrated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_review_csv(args.review_csv, review_rows)

    matched = sum(1 for r in review_rows if str(r["status"]).startswith("matched"))
    new = sum(1 for r in review_rows if r["status"] == "new")
    per_talk_warnings = [
        f"talk #{t.number or '?'}: {w}" for t in talks for w in t.warnings
    ]
    all_warnings = parser_warnings + migration_warnings + per_talk_warnings

    print(f"Parsed legacy talks : {len(talks)}")
    print(f"Existing JSON records: {len(existing)}")
    print(f"Matched existing    : {matched}")
    print(f"Added as new        : {new}")
    print(f"Output records      : {len(migrated)}")
    print(f"Wrote JSON          : {args.output}")
    print(f"Wrote review CSV    : {args.review_csv}")

    if all_warnings:
        print(f"Warnings            : {len(all_warnings)}")
        for warning in all_warnings:
            print(f"  - {warning}")
    else:
        print("Warnings            : 0")

    return 1 if args.fail_on_warning and all_warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())
