"""Rule-based rubricator (classification code) extraction.

Extracts library / scientific-technical classification codes together with
their numbers from plain document text.  Currently supported:

* УДК — Универсальная десятичная классификация (e.g. ``УДК 51.78``)
* ГРНТИ — Государственный реестр научно-технической информации
  (e.g. ``ГРНТИ 12.34.56``)
* ГНТИ — legacy variant of the same register

The extractor is purely regex-based (no VLM, no markdown).  Every match is
returned as a small dict with the code type, the full value (code + number),
the bare number, an evidence snippet and the 1-based page the match was found
on (page is always 1 for single-page texts; callers pass the page offset when
splitting multi-page documents).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ``УДК 658.123.4`` — number may contain dots and digits.
_UDK_RE = re.compile(
    r"\bУДК\s*([0-9]+(?:\.[0-9]+)*)",
    re.IGNORECASE,
)
# ``ГРНТИ 12.34.56`` — two- or three-part decimal code.
_GRNTI_RE = re.compile(
    r"\bГРНТИ\s*([0-9]{1,2}(?:\.[0-9]{1,2}){1,2})",
    re.IGNORECASE,
)
# Legacy ``ГНТИ`` spelling.
_GNTI_RE = re.compile(
    r"\bГНТИ\s*([0-9]{1,2}(?:\.[0-9]{1,2}){1,2})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RubricatorRecord:
    code_type: str   # "УДК", "ГРНТИ" or "ГНТИ"
    value: str       # full match, e.g. "УДК 51.78"
    number: str      # bare number, e.g. "51.78"
    evidence: str    # short snippet around the match
    page: int        # 1-based page number


def _snippet(text: str, start: int, end: int, radius: int = 60) -> str:
    s = max(0, start - radius)
    e = min(len(text), end + radius)
    return text[s:e].replace("\n", " ").strip()


def extract_rubricators(text: str, page: int = 1) -> list[dict]:
    """Find all rubricator codes in ``text``.

    Returns a list of dicts: ``{"type", "value", "number", "evidence", "page"}``.
    Duplicates (same type + number on the same page) are collapsed.
    """
    records: list[RubricatorRecord] = []
    seen: set[tuple[str, str, int]] = set()

    for match in _UDK_RE.finditer(text):
        code_type = "УДК"
        key = (code_type, match.group(1), page)
        if key in seen:
            continue
        seen.add(key)
        records.append(RubricatorRecord(
            code_type=code_type,
            value=match.group(0),
            number=match.group(1),
            evidence=_snippet(text, match.start(), match.end()),
            page=page,
        ))

    for match in _GRNTI_RE.finditer(text):
        code_type = "ГРНТИ"
        key = (code_type, match.group(1), page)
        if key in seen:
            continue
        seen.add(key)
        records.append(RubricatorRecord(
            code_type=code_type,
            value=match.group(0),
            number=match.group(1),
            evidence=_snippet(text, match.start(), match.end()),
            page=page,
        ))

    for match in _GNTI_RE.finditer(text):
        code_type = "ГНТИ"
        key = (code_type, match.group(1), page)
        if key in seen:
            continue
        seen.add(key)
        records.append(RubricatorRecord(
            code_type=code_type,
            value=match.group(0),
            number=match.group(1),
            evidence=_snippet(text, match.start(), match.end()),
            page=page,
        ))

    return [
        {
            "type": r.code_type,
            "value": r.value,
            "number": r.number,
            "evidence": r.evidence,
            "page": r.page,
        }
        for r in records
    ]
