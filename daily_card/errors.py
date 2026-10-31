"""Errors the daily-card CLI can show without a traceback."""


class DailyCardError(Exception):
    """A user-facing failure. The message must not contain secrets."""
