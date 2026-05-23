"""Printer-specific exception hierarchy."""

from __future__ import annotations


class PrinterError(Exception):
    """Base class for all printer errors."""


class PrinterTimeoutError(PrinterError):
    """Raised when a status request times out."""


class PrinterProtocolError(PrinterError):
    """Raised when the response payload cannot be parsed."""
