"""Generate the simulated wearable dataset and the evaluation fixtures.

Run from the project root:

    python data/generate_data.py

The output is deterministic (fixed seed and fixed end date) so evaluation
results are reproducible. No real personal health data is used anywhere.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = DATA_DIR / "fixtures"
END_DATE = date(2026, 11, 9)
FIELDS = [
    "date",
    "sleep_hours",
    "steps",
    "active_minutes",
    "exercise_minutes",
    "resting_hr",
]


def build_rows(days: int, *, seed: int = 42, healthy_trend: bool = True) -> list[dict]:
    """Create realistic-looking wearable data with a mild recent decline."""
    rnd = random.Random(seed)
    rows: list[dict] = []
    for i in range(days):
        current = END_DATE - timedelta(days=days - 1 - i)
        weekend = current.weekday() >= 5
        recent = i >= days - 7

        sleep = 7.25 + (0.35 if weekend else 0.0) + rnd.uniform(-0.28, 0.28)
        steps = 8100 + (900 if weekend else 0) + rnd.uniform(-900, 900)
        exercise = 27 + rnd.uniform(-9, 9)
        resting = 63 + rnd.uniform(-2.5, 2.5)

        if healthy_trend and recent:
            sleep -= 0.55
            steps -= 1300
            exercise -= 10
            resting += 3

        active = int(max(0, min(180, steps / 125 + rnd.uniform(-6, 6))))
        rows.append(
            {
                "date": current.isoformat(),
                "sleep_hours": round(max(3.2, min(10.5, sleep)), 1),
                "steps": int(max(800, min(22000, steps))),
                "active_minutes": active,
                "exercise_minutes": int(max(0, min(90, exercise))),
                "resting_hr": int(max(48, min(95, resting))),
            }
        )
    return rows


def flat_rows(days: int = 30, *, seed: int = 7) -> list[dict]:
    rows = build_rows(days, seed=seed, healthy_trend=False)
    rnd = random.Random(seed + 1)
    for row in rows:
        row["sleep_hours"] = round(7.0 + rnd.uniform(-0.05, 0.05), 2)
    return rows


def write_csv(path: Path, rows: list[dict], *, fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fieldnames or FIELDS
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_raw(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    rows_35 = build_rows(35)
    rows_30 = rows_35[-30:]

    # Main demo dataset: 35 days, clean, mild recent decline.
    write_csv(DATA_DIR / "sample_health_data.csv", rows_35)
    write_csv(FIXTURES_DIR / "clean_30d.csv", rows_30)
    write_csv(FIXTURES_DIR / "clean_35d.csv", rows_35)

    # Rows shuffled: order must not matter.
    shuffled = rows_30[:]
    random.Random(3).shuffle(shuffled)
    write_csv(FIXTURES_DIR / "unsorted.csv", shuffled)

    # Missing values are left empty on purpose.
    missing_sleep = [dict(row) for row in rows_30]
    for idx in (0, 5, 12, 18, 25):
        missing_sleep[idx]["sleep_hours"] = ""
    write_csv(FIXTURES_DIR / "missing_sleep.csv", missing_sleep)

    # Missing required column.
    write_csv(
        FIXTURES_DIR / "missing_exercise_column.csv",
        rows_30,
        fieldnames=[f for f in FIELDS if f != "exercise_minutes"],
    )

    # Invalid numbers / dates / duplicates.
    negative = [dict(row) for row in rows_30]
    negative[3]["steps"] = -1200
    negative[7]["sleep_hours"] = -0.5
    write_csv(FIXTURES_DIR / "negative_values.csv", negative)

    bad_date = [dict(row) for row in rows_30]
    bad_date[4]["date"] = "2026-10-32"
    write_csv(FIXTURES_DIR / "invalid_date.csv", bad_date)

    non_numeric = [dict(row) for row in rows_30]
    non_numeric[6]["steps"] = "many"
    write_csv(FIXTURES_DIR / "non_numeric.csv", non_numeric)

    duplicate = [dict(row) for row in rows_30]
    duplicate.append(dict(duplicate[10]))
    write_csv(FIXTURES_DIR / "duplicate_dates.csv", duplicate)

    # Insufficient history: only 7 days and 12 days.
    write_csv(FIXTURES_DIR / "insufficient_7d.csv", rows_30[-7:])
    write_csv(FIXTURES_DIR / "insufficient_12d.csv", rows_30[-12:])

    # Valid rows but with 10 calendar gaps inside a 30-day span.
    gappy = [dict(row) for row in rows_30 if int(row["date"][-2:]) % 3 != 0]
    write_csv(FIXTURES_DIR / "gappy_30d.csv", gappy)

    # Header only.
    write_raw(FIXTURES_DIR / "empty.csv", ",".join(FIELDS) + "\n")

    # Stable sleep, useful for the "stable trend" case.
    write_csv(FIXTURES_DIR / "flat_sleep.csv", flat_rows(30))

    # A larger-than-usual but still valid variation case.
    variable = [dict(row) for row in rows_30]
    for idx, row in enumerate(variable):
        if idx % 5 == 0:
            row["steps"] = 15000 + idx * 10
            row["sleep_hours"] = 8.4
        elif idx % 7 == 0:
            row["steps"] = 3500
            row["sleep_hours"] = 5.6
    write_csv(FIXTURES_DIR / "variable_30d.csv", variable)

    print(f"Wrote sample dataset and {len(list(FIXTURES_DIR.glob('*.csv')))} fixtures to {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
