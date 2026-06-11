"""Printer-specific exception hierarchy."""

from __future__ import annotations


class PrinterError(Exception):
    """Base class for all printer errors."""


class PrinterTimeoutError(PrinterError):
    """Raised when a status request times out."""


class PrinterProtocolError(PrinterError):
    """Raised when the response payload cannot be parsed."""


class PauseDebouncedError(PrinterError):
    """Raised by pause() when a pause was already published within the debounce window.

    This is distinct from a successful pause or a communication failure.
    Callers that receive this exception must inspect the actual printer status
    (via status()) to determine whether the printer is truly paused before
    treating the operation as successful.
    """
