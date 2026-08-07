"""CSV format definitions: how one bank's export maps onto our canonical fields.

Definitions live in the ``csv_format`` table, which lives in the gitignored database, so
the source tree never records which institutions you bank with.

The first time an unrecognised CSV is imported, :func:`infer` proposes a mapping from
the header and a sample of rows. Anything it cannot work out becomes a
:class:`Question` for the caller to put to the user; nothing is guessed silently.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import CsvFormat

# How a row expresses its amount.
DEBIT_CREDIT = "debit_credit"  # two columns, one populated; debit is an outflow
SIGNED = "signed"  # one column, already signed (negative = outflow)
AMOUNT_STYLES = (DEBIT_CREDIT, SIGNED)

_JSON_FIELDS = ("signature", "date_formats", "dedup_columns")
_SCALAR_FIELDS = (
    "name",
    "posted_date_column",
    "description_column",
    "amount_style",
    "txn_date_column",
    "category_column",
    "debit_column",
    "credit_column",
    "amount_column",
    "account_column",
    "account_prefix",
    "invert_amount",
)


class UnknownFormat(ValueError):
    """The header matched no defined format, or no such format is defined."""


class AccountRequired(ValueError):
    """The format carries no account column, so the caller must name the account."""


class InvalidFormat(ValueError):
    """A format definition is missing required fields or is self-inconsistent."""


@dataclass(frozen=True)
class FormatSpec:
    name: str
    signature: Sequence[str]
    posted_date_column: str
    description_column: str
    date_formats: Sequence[str]
    amount_style: str
    dedup_columns: Sequence[str]
    txn_date_column: Optional[str] = None
    category_column: Optional[str] = None
    debit_column: Optional[str] = None
    credit_column: Optional[str] = None
    amount_column: Optional[str] = None
    account_column: Optional[str] = None
    account_prefix: str = ""
    # True when a positive value in this layout means money leaving the account (the
    # opposite of our convention). Only meaningful for amount_style == SIGNED; a
    # debit/credit pair already says which side is an outflow.
    invert_amount: bool = False

    @property
    def needs_account(self) -> bool:
        return self.account_column is None


def validate(spec: FormatSpec) -> FormatSpec:
    """Reject definitions that would fail confusingly at import time."""
    if not spec.name:
        raise InvalidFormat("A format needs a name.")
    for attribute in ("signature", "dedup_columns", "date_formats"):
        if not getattr(spec, attribute):
            raise InvalidFormat(f"{spec.name!r}: {attribute} must not be empty.")
    if not spec.posted_date_column:
        raise InvalidFormat(f"{spec.name!r}: a date column is required.")
    if not spec.description_column:
        raise InvalidFormat(f"{spec.name!r}: a description column is required.")
    if spec.amount_style not in AMOUNT_STYLES:
        raise InvalidFormat(
            f"{spec.name!r}: amount_style must be one of {list(AMOUNT_STYLES)}, "
            f"got {spec.amount_style!r}."
        )
    if spec.amount_style == SIGNED and not spec.amount_column:
        raise InvalidFormat(f"{spec.name!r}: amount_style 'signed' needs amount_column.")
    if spec.amount_style == DEBIT_CREDIT and not (
        spec.debit_column and spec.credit_column
    ):
        raise InvalidFormat(
            f"{spec.name!r}: amount_style 'debit_credit' needs debit_column and "
            "credit_column."
        )
    return spec


def from_dict(data: Dict[str, Any]) -> FormatSpec:
    known = set(_SCALAR_FIELDS) | set(_JSON_FIELDS)
    unknown = sorted(set(data) - known)
    if unknown:
        raise InvalidFormat(f"Unknown field(s) in format definition: {unknown}.")
    return validate(FormatSpec(**data))


def to_dict(spec: FormatSpec) -> Dict[str, Any]:
    return asdict(spec)


def _from_row(row: CsvFormat) -> FormatSpec:
    values: Dict[str, Any] = {f: getattr(row, f) for f in _SCALAR_FIELDS}
    values.update({f: json.loads(getattr(row, f)) for f in _JSON_FIELDS})
    return FormatSpec(**values)


# ------------------------------------------------------------------------- the store

def list_formats(session: Session) -> List[FormatSpec]:
    rows = session.scalars(select(CsvFormat).order_by(CsvFormat.name))
    return [_from_row(row) for row in rows]


def get_format(session: Session, name: str) -> FormatSpec:
    row = session.scalar(select(CsvFormat).where(CsvFormat.name == name.strip()))
    if row is None:
        known = ", ".join(f.name for f in list_formats(session)) or "none defined"
        raise UnknownFormat(f"Unknown format {name!r}; defined formats: {known}.")
    return _from_row(row)


def save_format(session: Session, spec: FormatSpec) -> FormatSpec:
    """Insert or update the format named ``spec.name``. Does not commit."""
    validate(spec)
    row = session.scalar(select(CsvFormat).where(CsvFormat.name == spec.name))
    if row is None:
        row = CsvFormat(name=spec.name)
        session.add(row)
    for name in _SCALAR_FIELDS:
        setattr(row, name, getattr(spec, name))
    for name in _JSON_FIELDS:
        setattr(row, name, json.dumps(list(getattr(spec, name))))
    session.flush()
    return spec


def set_account_prefix(session: Session, name: str, prefix: str) -> FormatSpec:
    """Change the prefix a layout puts in front of derived account names. No commit.

    Worth doing after merging accounts: leaving the old prefix in place would split them
    apart again on the next import.
    """
    spec = get_format(session, name)
    prefix = prefix.strip()
    if prefix and not prefix.endswith(" "):
        prefix += " "
    return save_format(session, FormatSpec(**{**to_dict(spec), "account_prefix": prefix}))


def set_invert_amount(session: Session, name: str, invert: bool) -> FormatSpec:
    """Flip a stored format's amount polarity. No commit.

    For when a format was learned the wrong way round: every provider using this layout
    keeps the same convention, so the fix belongs on the format, not on individual rows.
    Existing transactions already imported are unaffected; only future imports change.
    """
    spec = get_format(session, name)
    return save_format(
        session, FormatSpec(**{**to_dict(spec), "invert_amount": bool(invert)})
    )


def remove_format(session: Session, name: str) -> bool:
    """Delete a format. Does not commit."""
    row = session.scalar(select(CsvFormat).where(CsvFormat.name == name.strip()))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def detect(session: Session, fieldnames: Sequence[str]) -> FormatSpec:
    """Return the defined format whose signature columns are all present."""
    present = set(fieldnames or ())
    defined = list_formats(session)
    for spec in defined:
        if present.issuperset(spec.signature):
            return spec
    if not defined:
        raise UnknownFormat("No CSV formats are defined yet.")
    known = ", ".join(spec.name for spec in defined)
    raise UnknownFormat(
        f"CSV header matches no defined format ({known}). Columns found: "
        f"{sorted(present)}."
    )


# -------------------------------------------------------------------------- matching

# Ordered candidates per field; the first column whose name matches wins. Patterns are
# matched case-insensitively against the whole column name.
_COLUMN_PATTERNS = (
    ("posted_date_column", (r".*post.*date.*", r".*settle.*date.*", r"date")),
    ("txn_date_column", (r".*trans.*date.*", r".*effective.*date.*", r".*value.*date.*")),
    (
        "description_column",
        (r"description", r"payee", r"merchant", r"name", r".*description.*", r"memo"),
    ),
    ("category_column", (r".*category.*",)),
    ("account_column", (r"card\s*(no\.?|number)", r"account\s*(no\.?|number|id)")),
    ("debit_column", (r"debit", r"withdrawal.*", r"charge", r"money\s*out")),
    ("credit_column", (r"credit", r"deposit.*", r"payment", r"money\s*in")),
    ("amount_column", (r"amount", r".*amount.*", r"value")),
)

# Columns whose values, if unique across the sample, identify a row on their own.
_ID_PATTERNS = (r".*transaction\s*id.*", r"id", r".*reference.*(no|number|id)?.*")

# Tried against sample values; the first that parses every one is used.
_DATE_CANDIDATES = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    # Compact ISO, common in broker exports. Unambiguous: no other candidate here parses
    # eight bare digits, so it cannot shadow one of the separator-bearing formats.
    "%Y%m%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%b %d, %Y",
    "%d %b %Y",
    "%m/%d/%y",
    "%d/%m/%y",
)
# Pairs that cannot be told apart without a value where one component exceeds 12.
_AMBIGUOUS_PAIRS = (("%m/%d/%Y", "%d/%m/%Y"), ("%m-%d-%Y", "%d-%m-%Y"), ("%m/%d/%y", "%d/%m/%y"))


@dataclass
class Question:
    """Something inference could not settle, to be put to the user."""

    field: str
    prompt: str
    choices: Sequence[str] = ()
    default: Optional[str] = None
    allow_empty: bool = False


@dataclass
class Inference:
    values: Dict[str, Any]
    questions: List[Question]

    @property
    def complete(self) -> bool:
        return not self.questions


def _match_column(fieldnames: Sequence[str], patterns: Sequence[str]) -> Optional[str]:
    for pattern in patterns:
        regex = re.compile(pattern, re.IGNORECASE)
        for name in fieldnames:
            if regex.fullmatch(name.strip()):
                return name
    return None


def _samples(rows: Sequence[Dict[str, str]], column: str, limit: int = 50) -> List[str]:
    values = []
    for row in rows[:limit]:
        value = (row.get(column) or "").strip()
        if value:
            values.append(value)
    return values


def _infer_date_formats(values: Sequence[str]) -> List[str]:
    """Every candidate that parses all sample values, best first."""
    if not values:
        return []
    working = []
    for candidate in _DATE_CANDIDATES:
        try:
            for value in values:
                datetime.strptime(value, candidate)
        except ValueError:
            continue
        working.append(candidate)
    return working


def _is_ambiguous(working: Sequence[str]) -> bool:
    """True when only day/month order distinguishes the surviving candidates."""
    for first, second in _AMBIGUOUS_PAIRS:
        if first in working and second in working:
            return True
    return False


def _infer_dedup_columns(values: Dict[str, Any], rows, fieldnames) -> List[str]:
    """Prefer a unique id column; otherwise the mapped identifying fields.

    The fallback order is fixed — account, transaction date, posted date, description,
    then the amount column(s) — because it also defines the dedup hash. Changing it
    would make every previously imported row look new.
    """
    for pattern in _ID_PATTERNS:
        column = _match_column(fieldnames, (pattern,))
        if column is None:
            continue
        sample = _samples(rows, column)
        if sample and len(set(sample)) == len(sample):
            return [column]

    ordered = [
        values.get("account_column"),
        values.get("txn_date_column"),
        values.get("posted_date_column"),
        values.get("description_column"),
    ]
    if values.get("amount_style") == DEBIT_CREDIT:
        ordered += [values.get("debit_column"), values.get("credit_column")]
    else:
        ordered.append(values.get("amount_column"))
    return [column for column in ordered if column]


def _date_question(values, rows):
    """Resolve date_formats in place, or return the question that would settle it.

    This runs *after* the date column is known, and that column may itself have been a
    question, so it is re-checked on every pass rather than only on the first.
    """
    column = values.get("posted_date_column")
    if not column or values.get("date_formats"):
        return None
    samples = _samples(rows, column)
    working = _infer_date_formats(samples)
    if not working:
        example = f" (e.g. {samples[0]!r})" if samples else ""
        return Question(
            field="date_formats",
            prompt=(
                f"Could not recognise the dates in {column!r}{example}. "
                "Enter a strptime format, e.g. %d.%m.%Y"
            ),
        )
    if _is_ambiguous(working):
        # Every sampled day is <= 12, so month-first and day-first both parse. Guessing
        # here would silently mis-date transactions.
        example = samples[0] if samples else ""
        return Question(
            field="date_formats",
            prompt=f"Dates in {column!r} are ambiguous (e.g. {example}). Which is first?",
            choices=("month", "day"),
            default="month",
        )
    values["date_formats"] = working[:1]
    return None


def remaining_questions(values, rows, fieldnames):
    """Everything still unresolved, given what is known so far."""
    questions = []
    if not values.get("amount_style"):
        questions.append(
            Question(
                field="amount_column",
                prompt="Which column holds the amount (negative for money out)?",
                choices=fieldnames,
            )
        )
    if not values.get("posted_date_column"):
        questions.append(
            Question(
                field="posted_date_column",
                prompt="Which column holds the date the transaction posted?",
                choices=fieldnames,
            )
        )
    if not values.get("description_column"):
        questions.append(
            Question(
                field="description_column",
                prompt="Which column describes the transaction (payee or merchant)?",
                choices=fieldnames,
            )
        )
    date_question = _date_question(values, rows)
    if date_question is not None:
        questions.append(date_question)
    invert_question = _invert_amount_question(values, rows)
    if invert_question is not None:
        questions.append(invert_question)
    return questions


def _sample_pairs(rows, description_column, amount_column, limit=3):
    """Up to ``limit`` (description, amount) pairs, preferring a mix of signs.

    A lone sample like "-75.00" is a coin flip: nothing says whether the file was
    showing a purchase or a payment. Pairing it with its own description, and
    including a sample of the opposite sign when the file has one, makes the direction
    legible without the reader having to trust an arbitrary first row.
    """
    pairs = []
    for row in rows:
        amount = (row.get(amount_column) or "").strip() if amount_column else ""
        if not amount:
            continue
        description = (row.get(description_column) or "").strip() if description_column else ""
        pairs.append((description, amount))

    first_by_sign: Dict[bool, int] = {}
    for index, (_, amount) in enumerate(pairs):
        negative = amount.lstrip().startswith("-")
        first_by_sign.setdefault(negative, index)
    chosen = sorted(first_by_sign.values())
    for index in range(len(pairs)):
        if len(chosen) >= limit:
            break
        if index not in chosen:
            chosen.append(index)
    return [pairs[i] for i in sorted(chosen)[:limit]]


def _invert_amount_question(values, rows) -> Optional[Question]:
    """A debit/credit pair already says which side is an outflow; a single signed
    column does not, and providers disagree on which sign means money leaving the
    account. Ask, with real description/amount pairs so the answer is obvious at a
    glance instead of resting on whichever sign the first row happened to have.
    """
    if values.get("amount_style") != SIGNED or "invert_amount" in values:
        return None
    column = values.get("amount_column")
    examples = _sample_pairs(rows, values.get("description_column"), column)
    if examples:
        shown = "; ".join(
            f"{description or '(no description)'} {amount}"
            for description, amount in examples
        )
    else:
        shown = "617.66"
    return Question(
        field="invert_amount",
        prompt=(
            f"In this file: {shown}. Does a positive amount mean money leaving the "
            "account?"
        ),
        choices=("yes", "no"),
        default="no",
    )


def infer(name, fieldnames, rows):
    """Propose a :class:`FormatSpec` from a header and sample rows.

    Returns the fields worked out plus a :class:`Question` for each that was not.
    """
    fieldnames = [f for f in (fieldnames or []) if f]
    values = {"name": name, "account_prefix": "", "date_formats": []}

    for field, patterns in _COLUMN_PATTERNS:
        values[field] = _match_column(fieldnames, patterns)

    # A debit/credit pair beats a single amount column when both are present.
    if values.get("debit_column") and values.get("credit_column"):
        values["amount_style"] = DEBIT_CREDIT
        values["amount_column"] = None
    elif values.get("amount_column"):
        values["amount_style"] = SIGNED
        values["debit_column"] = values["credit_column"] = None
    else:
        values["amount_style"] = None

    # Fall back to the transaction date before asking which column dates the row.
    if not values.get("posted_date_column") and values.get("txn_date_column"):
        values["posted_date_column"] = values["txn_date_column"]

    values["signature"] = list(fieldnames)
    questions = remaining_questions(values, rows, fieldnames)
    values["dedup_columns"] = _infer_dedup_columns(values, rows, fieldnames)
    return Inference(values=values, questions=questions)


def apply_answers(values, answers, fieldnames, rows=()):
    """Fold user answers back into inferred values, then redo derived fields."""
    values = dict(values)
    for field, answer in answers.items():
        answer = (answer or "").strip()
        if not answer:
            continue
        if field == "date_formats":
            if answer in ("month", "day"):
                values["date_formats"] = [
                    "%m/%d/%Y" if answer == "month" else "%d/%m/%Y"
                ]
            else:
                values["date_formats"] = [answer]
        elif field == "amount_column":
            values["amount_column"] = answer
            values["amount_style"] = SIGNED
            values["debit_column"] = values["credit_column"] = None
        elif field == "invert_amount":
            values["invert_amount"] = answer.lower() in ("yes", "y", "true", "1")
        else:
            values[field] = answer
    # The dedup key depends on the mapped columns, so it is only final once they are.
    values["dedup_columns"] = _infer_dedup_columns(values, rows, fieldnames)
    return values


def spec_from_values(values: Dict[str, Any]) -> FormatSpec:
    """Build a validated spec, dropping keys that are not spec fields."""
    known = set(_SCALAR_FIELDS) | set(_JSON_FIELDS)
    return validate(FormatSpec(**{k: v for k, v in values.items() if k in known}))
