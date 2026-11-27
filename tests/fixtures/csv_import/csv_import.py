"""Import rows from a CSV file with a header."""

import csv
from pathlib import Path


def import_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream)
        next(reader, None)
        return list(reader)
