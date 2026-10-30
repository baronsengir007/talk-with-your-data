"""Grounding: every figure in an answer is a placeholder filled by code from a named result cell.

render(template, rows, question) fills `{{col}}` (one-row results) and
`{{rows[i].col}}` from the query result. It returns the text and the list
of failures. A placeholder that names no cell fails. A literal number left
in the template fails too, unless it is a year or a number copied from the
question. A column ending in `_pct` is a percentage (the SQL multiplied it
by 100) and renders with a % sign; a % or "percent" written after a
placeholder of any other column fails, so a fraction never reads as a
percentage. Pure: no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

PLACEHOLDER = re.compile(
    r"\{\{\s*(?:rows\[(\d+)\]\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\}\}(\s?%|\s+percent\b)?"
)
PCT_SUFFIX = "_pct"
# A money column (revenue, price, freight, payment value) renders in Brazilian reais.
MONEY = re.compile(r"revenue|price|freight|_value|amount|spend")
NUMBER = re.compile(r"(?<![A-Za-z0-9_])\d[\d,.]*")
# A state column holds a two-letter code; the answer shows the name with the code after it.
STATES = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapa", "AM": "Amazonas", "BA": "Bahia",
    "CE": "Ceara", "DF": "Distrito Federal", "ES": "Espirito Santo", "GO": "Goias",
    "MA": "Maranhao", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais",
    "PA": "Para", "PB": "Paraiba", "PR": "Parana", "PE": "Pernambuco", "PI": "Piaui",
    "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul",
    "RO": "Rondonia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "Sao Paulo",
    "SE": "Sergipe", "TO": "Tocantins",
}


@dataclass(frozen=True)
class Rendered:
    text: str
    failures: list[str]

    @property
    def ok(self) -> bool:
        return not self.failures


def format_value(value) -> str:
    """One result cell as text. Thousands separators from 10,000 up (so a year stays 2017), floats two decimals."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}" if abs(value) >= 10_000 else str(value)
    if isinstance(value, (float, Decimal)):
        number = float(value)
        if number.is_integer():
            return format_value(int(number))
        return f"{number:,.2f}" if abs(number) >= 10_000 else f"{number:.2f}"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def format_money(column: str, value) -> str:
    """A cell as text, with R$ in front when the column holds an amount of money."""
    text = format_value(value)
    is_number = isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    return f"R$ {text}" if is_number and MONEY.search(column) else text


def format_cell(column: str, value) -> str:
    """A cell as text: a state code as "Sao Paulo (SP)", money with R$, the rest as format_value."""
    if column.endswith("state") and isinstance(value, str) and value.upper() in STATES:
        return f"{STATES[value.upper()]} ({value.upper()})"
    return format_money(column, value)


def format_pct(value, as_word: bool = False) -> str:
    """A `_pct` cell as text: the number with a % sign (or the word percent); n/a stays n/a."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return format_value(value)
    return f"{format_value(value)} percent" if as_word else f"{format_value(value)}%"


def _is_year(token: str) -> bool:
    return token.isdigit() and len(token) == 4 and 1900 <= int(token) <= 2100


def _numbers_in(text: str) -> set[str]:
    return {t.strip(".,").replace(",", "") for t in NUMBER.findall(text)}


def render(template: str, rows: list[dict], question: str = "") -> Rendered:
    """Fill the placeholders of `template` from `rows`; report every failure."""
    failures: list[str] = []
    allowed_numbers = _numbers_in(question)

    def fill(match: re.Match) -> str:
        index, column = match.group(1), match.group(2)
        if index is None:
            if len(rows) != 1:
                failures.append(
                    f"{match.group(0)} needs exactly one result row, the result has "
                    f"{len(rows)}; use {{{{rows[i].{column}}}}}"
                )
                return match.group(0)
            row = rows[0]
        else:
            i = int(index)
            if i >= len(rows):
                failures.append(
                    f"{match.group(0)} names row {i}, the result has {len(rows)} rows"
                )
                return match.group(0)
            row = rows[i]
        if column not in row:
            failures.append(
                f"{match.group(0)} names column {column}, which is not in the result"
            )
            return match.group(0)
        unit = match.group(3)
        if column.endswith(PCT_SUFFIX):
            return format_pct(row[column], "percent" in (unit or ""))
        if unit:
            failures.append(
                f"{match.group(0)} shows column {column} as a percentage, but only a column "
                f"ending in {PCT_SUFFIX} (multiplied by 100 in SQL) is one"
            )
            return match.group(0)
        return format_cell(column, row[column])

    text = PLACEHOLDER.sub(fill, template)
    without_placeholders = PLACEHOLDER.sub("", template)
    for token in NUMBER.findall(without_placeholders):
        clean = token.strip(".,")
        if not clean:
            continue
        bare = clean.replace(",", "")
        if _is_year(bare) or bare in allowed_numbers:
            continue
        failures.append(f"literal number {clean} in the text is not a result cell")
    return Rendered(text=text, failures=failures)
