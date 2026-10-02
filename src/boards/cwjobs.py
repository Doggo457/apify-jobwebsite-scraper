"""CWJobs.co.uk - UK IT & tech jobs (StepStone platform)."""

from .stepstone import StepStoneScraper


class CWJobsScraper(StepStoneScraper):
    base_url = "https://www.cwjobs.co.uk"

    @property
    def source_name(self) -> str:
        return "cwjobs.co.uk"
