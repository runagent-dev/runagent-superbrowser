"""The eval task suite — EDIT THIS FILE.

Replace the placeholders with your five tasks, written exactly like the ones you
run through ``nanobot/test_superbrowser.py``: a natural-language instruction plus
an optional starting URL.

``reference`` is an optional success rubric the LLM judge (eval/oracles.py) scores
the orchestrator's final answer against. If omitted, the judge falls back to the
instruction's implied success criteria. A good rubric states what a correct,
non-fabricated answer must contain.

The first two tasks below are the ones already wired in test_superbrowser.py, kept
as worked examples. Tasks whose instruction still starts with "TODO" are skipped
by the runner (with a warning), so the harness runs even before you fill all five.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Task:
    id: str
    instruction: str
    url: str | None = None
    reference: str | None = None

    @property
    def is_placeholder(self) -> bool:
        return self.instruction.strip().upper().startswith("TODO")


TASKS: list[Task] = [
    Task(
        id="petfinder_rabbits",
        instruction=(
            "Go to https://www.petfinder.com/ and show me the list of young male    Must use this url. "
            "English Spot rabbits available for adoption in Chicago, IL, within 50 miles. Do it very very carefully. Don't use any check learning tool."
        ),
        url="https://www.petfinder.com/",
        reference=(
            "Success = one or more young male English Spot rabbit listings (or a clear "
            "'none currently available' result) scoped to Chicago, IL within 50 miles, "
            "with the listings actually read from petfinder.com — not fabricated."
        ),
    ),
    Task(
        id="trip_flight_dac_bkk",
        instruction=(
            "Go to trip.com and find the cheapest flight from Dhaka (DAC) to Bangkok (BKK) "
            "departing 30 April 2026 and returning 5 May 2026."
        ),
        url="https://www.trip.com/",
        reference=(
            "Success = a concrete cheapest-fare price with airline/itinerary for the "
            "DAC->BKK round trip departing 30 Apr 2026 / returning 5 May 2026, read from "
            "trip.com search results — not fabricated."
        ),
    ),
    Task(
        id="accuweather_so2_cork",
        instruction=(
            "Go to https://www.accuweather.com/ and find the SO2 (sulfur dioxide) air "
            "quality reading over the past hour for Maine North, County Cork, Ireland. Use browser tools."
        ),
        url="https://www.accuweather.com/",
        reference=(
            "Success = the SO2 air-quality value (with units, e.g. µg/m³) for the past "
            "hour at Maine North, County Cork, Ireland, read from AccuWeather's air-quality "
            "page for that location — not fabricated."
        ),
    ),
    Task(
        id="bestbuy_8k_samsung_openbox",
        instruction=(
            "Go to https://www.bestbuy.com/ and browse 8K Samsung TVs that are Open-Box."
        ),
        url="https://www.bestbuy.com/",
        reference=(
            "Success = one or more Samsung 8K TVs filtered to Open-Box condition (or a clear "
            "'none currently available' result) from Best Buy, with the open-box listings "
            "actually read from bestbuy.com — not fabricated."
        ),
    ),
    Task(
        id="bestbuy_qled_240hz_monitor",
        instruction=(
            "Go to https://www.bestbuy.com/ and search for a 33-to-49-inch QLED gaming "
            "monitor with a 240Hz refresh rate priced between $1000 and $2000."
        ),
        url="https://www.bestbuy.com/",
        reference=(
            "Success = one or more QLED gaming monitors matching all of: 33-49 inch screen "
            "size, 240Hz refresh rate, and $1000-$2000 price, from Best Buy search/filter "
            "results — not fabricated."
        ),
    ),
]


def active_tasks(include_placeholders: bool = False) -> list[Task]:
    return [t for t in TASKS if include_placeholders or not t.is_placeholder]
