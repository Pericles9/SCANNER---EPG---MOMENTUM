"""Single mutable source of truth for the current trading session date."""
from datetime import date


class SessionClock:
    """Single mutable source of truth for the current trading session date.

    Passed by reference everywhere session_date is needed.
    Call .roll() at session boundary to advance to the next calendar date.
    """

    def __init__(self) -> None:
        self._date: date = date.today()

    @property
    def date(self) -> date:
        return self._date

    def roll(self) -> None:
        """Advance to today's calendar date. Called by session_close()."""
        self._date = date.today()
