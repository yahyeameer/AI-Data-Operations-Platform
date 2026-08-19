"""
Value coercion: the accountant-specific half of type inference.

PRD section 6 lists what real spreadsheets contain, and most of that list is
not a structural problem but a *value* problem -- `(150.00)` is minus one fifty,
`£1,240.00` is a number, `1.200,50` is a number in a German locale export, and
`08/03/2026` is either the 8th of March or the 3rd of August depending on which
system wrote the file.

Two rules run through this module.

**Never guess silently.** Every coercion returns what it decided *and* how
confident it is. An ambiguous date is not resolved by picking the more likely
option and moving on; it is resolved at the column level, where there is enough
evidence to actually decide, and surfaced to the accountant when there is not.

**Never lose the original.** Cleaning writes a new version (section 3), and a
coerced value that cannot be traced back to the characters that produced it
would break the provenance promise in section 7. Callers keep the raw text;
this module only reports what it would become.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

# Currency symbols an accounting export realistically carries. Deliberately not
# "any non-digit": stripping arbitrary characters turns typos into numbers, and
# a typo that silently becomes a figure is exactly the failure this product
# exists to prevent.
CURRENCY_SYMBOLS = "£$€¥₹"

# Suffixes used by ledger exports to mark the sign. DR is positive (debit),
# CR is negative (credit) in a sales context.
_CREDIT_SUFFIX = re.compile(r"\s*(CR|CRDR|CREDIT)\s*$", re.IGNORECASE)
_DEBIT_SUFFIX = re.compile(r"\s*(DR|DEBIT)\s*$", re.IGNORECASE)

_PARENS = re.compile(r"^\((?P<inner>.+)\)$")
_TRAILING_MINUS = re.compile(r"^(?P<inner>[^-]+)-$")

# Values that mean "nothing here" in a spreadsheet a human maintained.
NULL_TOKENS = {
    "", "-", "--", "n/a", "na", "nil", "none", "null", "#n/a", "#value!",
    "#ref!", "#div/0!", "#name?", ".", "tbc", "tbd",
}

# Row labels that mark a summary line rather than a transaction. A subtotal row
# double-counts if it survives into the cleaned dataset, which is the single
# most expensive parsing mistake this product can make.
SUBTOTAL_TOKENS = {
    "total", "totals", "subtotal", "sub total", "sub-total", "grand total",
    "sum", "balance", "net total", "running total", "carried forward", "c/f",
    "brought forward", "b/f",
}

NumberStyle = Literal["plain", "parentheses", "currency", "thousands", "percent", "credit_suffix"]


@dataclass(frozen=True)
class NumberParse:
    value: Decimal | None
    styles: tuple[NumberStyle, ...] = ()
    ok: bool = False

    @property
    def as_float(self) -> float | None:
        return float(self.value) if self.value is not None else None


@dataclass(frozen=True)
class DateParse:
    value: dt.date | None
    # True when the literal could be read as either DMY or MDY -- i.e. both
    # components are <= 12. Resolved per column, never per cell.
    ambiguous: bool = False
    # The order actually used to produce `value`.
    order: Literal["dmy", "mdy", "ymd", "iso", "excel", "unknown"] = "unknown"
    ok: bool = False


def normalize_text(value: Any) -> str:
    """
    Whitespace and unicode normalisation.

    NFKC because a workbook that has been through Word or a PDF round-trip
    carries non-breaking spaces and full-width digits that are invisible on
    screen and fatal to a join. "Fabrikam  Ltd" and "Fabrikam Ltd" must collapse
    to the same key or the supplier grouping in the fixture is wrong by design.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace(" ", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def is_null_token(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    return normalize_text(value).lower() in NULL_TOKENS


# Words that may legitimately follow a summary token: "Total August 2026",
# "Subtotal Q3", "Total for the period". Anything else after "Total" means the
# cell is a name, not a summary.
_PERIOD_WORDS = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april",
    "may", "jun", "june", "jul", "july", "aug", "august", "sep", "sept",
    "september", "oct", "october", "nov", "november", "dec", "december",
    "q1", "q2", "q3", "q4", "h1", "h2", "ytd", "mtd",
    "for", "the", "period", "month", "year", "quarter", "week", "to", "date",
}


def is_subtotal_label(value: Any) -> bool:
    """
    Whether a cell reads as a summary label.

    The rule is deliberately strict. A subtotal row that survives into the data
    double-counts every figure beneath it, so the temptation is to match
    anything containing "total" -- but a supplier genuinely called "Total
    Fitness Ltd" is a transaction, and silently dropping its rows is the worse
    of the two errors. It is money that vanishes rather than money counted
    twice, and nothing downstream would flag it.

    So: an exact match, or a summary token followed only by words that describe
    a period.
    """
    text = normalize_text(value).lower().strip(" :.*-")
    if not text:
        return False
    if text in SUBTOTAL_TOKENS:
        return True

    words = text.split()
    if words[0] not in {"total", "totals", "subtotal"}:
        # Two-word tokens such as "sub total" and "grand total".
        if " ".join(words[:2]) in SUBTOTAL_TOKENS:
            return len(words) == 2 or all(
                word in _PERIOD_WORDS or word.isdigit() for word in words[2:]
            )
        return False

    return all(word in _PERIOD_WORDS or word.isdigit() for word in words[1:])


def parse_number(value: Any) -> NumberParse:
    """
    Coerce a cell to a number, recording which accounting conventions it used.

    The styles matter as much as the value. They are the evidence behind a
    proposal: "412 values in this column are parenthesised negatives" is a
    sentence an accountant can check, where "the column is numeric" is not.
    """
    if value is None:
        return NumberParse(None)
    if isinstance(value, bool):
        return NumberParse(None)
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and value != value:
            return NumberParse(None)
        return NumberParse(Decimal(str(value)), (), True)

    text = normalize_text(value)
    if not text or text.lower() in NULL_TOKENS:
        return NumberParse(None)

    styles: list[NumberStyle] = []
    negative = False

    credit = _CREDIT_SUFFIX.search(text)
    if credit:
        text = text[: credit.start()].strip()
        negative = True
        styles.append("credit_suffix")
    else:
        debit = _DEBIT_SUFFIX.search(text)
        if debit:
            text = text[: debit.start()].strip()
            styles.append("credit_suffix")

    parens = _PARENS.match(text)
    if parens:
        text = parens.group("inner").strip()
        negative = not negative
        styles.append("parentheses")

    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
        styles.append("percent")

    for symbol in CURRENCY_SYMBOLS:
        if symbol in text:
            text = text.replace(symbol, "").strip()
            if "currency" not in styles:
                styles.append("currency")

    trailing_minus = _TRAILING_MINUS.match(text)
    if trailing_minus:
        text = trailing_minus.group("inner").strip()
        negative = not negative

    if text.startswith("+"):
        text = text[1:].strip()
    if text.startswith("-"):
        text = text[1:].strip()
        negative = not negative

    if not text:
        return NumberParse(None)

    cleaned, separator_style = _strip_group_separators(text)
    if cleaned is None:
        return NumberParse(None)
    if separator_style:
        styles.append(separator_style)

    try:
        number = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return NumberParse(None)

    if negative:
        number = -number
    if percent:
        number = number / Decimal(100)

    return NumberParse(number, tuple(dict.fromkeys(styles)), True)


def _strip_group_separators(text: str) -> tuple[str | None, NumberStyle | None]:
    """
    Remove thousands separators, deciding between the UK/US and European
    conventions from the position of the separators rather than from a locale
    setting -- the file does not carry one, and assuming the host's locale makes
    the same file parse differently on the developer's laptop and the VPS.
    """
    if not re.fullmatch(r"[0-9.,' ]+", text):
        return None, None

    has_comma = "," in text
    has_dot = "." in text

    if has_comma and has_dot:
        # Whichever appears last is the decimal mark: 1,234.56 vs 1.234,56.
        if text.rfind(",") > text.rfind("."):
            return text.replace(".", "").replace(",", "."), "thousands"
        return text.replace(",", ""), "thousands"

    if has_comma:
        parts = text.split(",")
        # A single comma with exactly two trailing digits is European decimal
        # notation; anything grouped in threes is a thousands separator.
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            return text.replace(",", "."), None
        if all(len(part) == 3 for part in parts[1:]):
            return text.replace(",", ""), "thousands"
        return None, None

    if "'" in text or " " in text:
        # Swiss (1'234'567) and French (1 234 567) grouping.
        return text.replace("'", "").replace(" ", ""), "thousands"

    if has_dot:
        parts = text.split(".")
        if len(parts) > 2 and all(len(part) == 3 for part in parts[1:]):
            return text.replace(".", ""), "thousands"

    return text, None


# Explicit formats, tried in order. Kept explicit rather than delegating to a
# fuzzy parser because a fuzzy parser will happily read "13/01/2026" as a date
# and "31/01/2026" as a date and never tell you it switched conventions between
# them.
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$"), "dmy_or_mdy"),
    (re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2})$"), "dmy_or_mdy_short"),
)

_MONTH_NAMES = {
    name.lower(): index
    for index, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}

_TEXT_DATE = re.compile(
    r"^(\d{1,2})[\s-]*(" + "|".join(_MONTH_NAMES) + r")[a-z]*[\s-]*(\d{2,4})$",
    re.IGNORECASE,
)


def parse_date(value: Any, prefer: Literal["dmy", "mdy"] = "dmy") -> DateParse:
    """
    Coerce a cell to a date.

    `prefer` is the column-level decision, passed back down. On the first pass a
    caller uses the default and reads `ambiguous` to find out whether the column
    even needs a decision; on the second it passes the order the column's
    unambiguous values proved.
    """
    if value is None:
        return DateParse(None)
    if isinstance(value, dt.datetime):
        return DateParse(value.date(), False, "excel", True)
    if isinstance(value, dt.date):
        return DateParse(value, False, "excel", True)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Excel serial. 1900-01-01 is serial 1, and the 1900 leap-year bug means
        # serials below 61 are off by one -- excluded rather than corrected,
        # because a 1900 date in an accounting export is a data error, not a
        # date.
        serial = int(value)
        if 61 <= serial <= 60000:
            return DateParse(dt.date(1899, 12, 30) + dt.timedelta(days=serial), False, "excel", True)
        return DateParse(None)

    text = normalize_text(value)
    if not text or text.lower() in NULL_TOKENS:
        return DateParse(None)

    text = text.split(" ")[0] if re.match(r"^\S+\s+\d{1,2}:\d{2}", text) else text

    named = _TEXT_DATE.match(text)
    if named:
        day, month_name, year_text = named.groups()
        year = int(year_text)
        if year < 100:
            year += 2000 if year < 70 else 1900
        return _build_date(year, _MONTH_NAMES[month_name.lower()[:3]], int(day), False, "dmy")

    for pattern, kind in _DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue

        if kind == "ymd":
            year, month, day = (int(part) for part in match.groups())
            return _build_date(year, month, day, False, "ymd")

        first, second, year_text = (int(part) for part in match.groups())
        year = year_text
        if kind.endswith("short"):
            year += 2000 if year < 70 else 1900

        ambiguous = first <= 12 and second <= 12

        if first > 12:
            return _build_date(year, second, first, False, "dmy")
        if second > 12:
            return _build_date(year, first, second, False, "mdy")

        if prefer == "mdy":
            return _build_date(year, first, second, ambiguous, "mdy")
        return _build_date(year, second, first, ambiguous, "dmy")

    return DateParse(None)


def _build_date(year: int, month: int, day: int, ambiguous: bool, order: str) -> DateParse:
    try:
        return DateParse(dt.date(year, month, day), ambiguous, order, True)  # type: ignore[arg-type]
    except ValueError:
        return DateParse(None)


def looks_like_boolean(value: Any) -> bool | None:
    text = normalize_text(value).lower()
    if text in {"true", "yes", "y", "1"}:
        return True
    if text in {"false", "no", "n", "0"}:
        return False
    return None


def entity_key(value: Any) -> str:
    """
    A comparison key for a name that humans retype every month.

    "Northwind Supplies Ltd", "northwind supplies" and "NORTHWIND SUPPLIES LTD."
    are one supplier. Folding case, punctuation and the legal-form suffix is
    what lets the agent notice that -- and noticing it is what produces a
    mapping-table proposal instead of three separate suppliers in the report.

    This is a *candidate* key for grouping, never an automatic merge. Section 5
    puts entity resolution in the review tier for good reason: "Smith Ltd" and
    "Smith Holdings Ltd" fold together under any rule loose enough to catch the
    real duplicates.
    """
    text = normalize_text(value).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    suffixes = {
        "ltd", "limited", "llp", "llc", "plc", "inc", "incorporated",
        "co", "company", "corp", "corporation", "gmbh", "bv", "sa", "srl", "pty",
    }
    words = [word for word in text.split() if word not in suffixes]
    return " ".join(words) if words else text
