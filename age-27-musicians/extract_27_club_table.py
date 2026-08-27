#!/usr/bin/env python3
"""Extract the archived 27 Club table without applying any filters."""

from __future__ import annotations

import argparse
import csv
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


DEFAULT_SOURCE = Path(__file__).parent / "27_Club-table-history" / (
    "2024-04-18_07-59_27 Club - Wikipedia.html"
)
DEFAULT_OUTPUT = Path(__file__).parent / "age_27_club_members_2024-04-18.csv"
CSV_COLUMNS = [
    "name",
    "date_of_birth",
    "date_of_death",
    "cause_of_death",
    "fame",
    "age",
    "sources",
    "wikipedia_url",
]


def clean_text(parts: Iterable[str]) -> str:
    """Collapse HTML whitespace while retaining the table's visible wording."""

    # Joining text nodes with a single separator matches the archived page's
    # visible spacing around inline links, citations, and language labels.
    return " ".join(part.strip() for part in parts if part.strip())


class TableParser(HTMLParser):
    """Read the first HTML table's rows and visible cell text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.in_table = False
        self.current_row: list[dict[str, object]] | None = None
        self.current_cell: dict[str, object] | None = None
        self.rows: list[list[dict[str, object]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and not self.in_table:
            self.in_table = True
            self.table_depth = 1
            return
        if not self.in_table:
            return
        if tag == "table":
            self.table_depth += 1
        elif tag == "tr":
            self.current_row = []
        elif tag in {"th", "td"} and self.current_row is not None:
            self.current_cell = {"parts": [], "enwiki_url": ""}
            self.current_row.append(self.current_cell)
        elif tag == "a" and self.current_cell is not None:
            href = dict(attrs).get("href") or ""
            if not self.current_cell["enwiki_url"] and href.startswith(
                "https://en.wikipedia.org/wiki/"
            ):
                self.current_cell["enwiki_url"] = href

    def handle_endtag(self, tag: str) -> None:
        if not self.in_table:
            return
        if tag in {"th", "td"}:
            self.current_cell = None
        elif tag == "tr":
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = None
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth == 0:
                self.in_table = False

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell["parts"].append(data)  # type: ignore[union-attr]


def extract_rows(source: Path) -> list[dict[str, str]]:
    parser = TableParser()
    parser.feed(source.read_text(encoding="utf-8"))
    if not parser.rows or len(parser.rows[0]) != 7:
        raise ValueError("Expected a seven-column 27 Club table")

    output: list[dict[str, str]] = []
    for row in parser.rows[1:]:
        if len(row) != 7:
            raise ValueError(f"Unexpected row width: {len(row)}")
        values = [clean_text(cell["parts"]) for cell in row]
        output.append(
            dict(
                zip(
                    CSV_COLUMNS,
                    values[:7] + [str(row[0]["enwiki_url"])],
                    strict=True,
                )
            )
        )
    return output


def write_csv(rows: Iterable[dict[str, str]], output: Path) -> None:
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    rows = extract_rows(args.source)
    write_csv(rows, args.output)
    print(f"Wrote {len(rows)} rows to {args.output}")


if __name__ == "__main__":
    main()
