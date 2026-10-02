"""Totaljobs.com (StepStone platform)."""

from .stepstone import StepStoneScraper


class TotaljobsScraper(StepStoneScraper):
    base_url = "https://www.totaljobs.com"

    @property
    def source_name(self) -> str:
        return "totaljobs.com"
