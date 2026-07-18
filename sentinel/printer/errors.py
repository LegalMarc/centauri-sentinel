"""Printer-specific exception hierarchy."""

from __future__ import annotations


class PrinterError(Exception):
    """Base class for all printer errors."""


class PrinterTimeoutError(PrinterError):
    """Raised when a status request times out."""


class PrinterProtocolError(PrinterError):
    """Raised when the response payload cannot be parsed."""


class PrinterCommandError(PrinterError):
    """Raised when a control command (pause/resume/stop) is not acknowledged.

    This covers a failed registration handshake, a missing command
    acknowledgement, or a non-zero ``error_code`` in the printer's response.
    It exists specifically so that a command the printer silently ignores
    surfaces as a real failure instead of a false success — on firmware 02.x
    an unregistered ``api_request`` is dropped without any error.
    """


class PrinterRegistrationError(PrinterCommandError):
    """Raised when the firmware-02.x registration handshake fails or times out.

    Commands cannot be delivered until the client is registered, so this is a
    specific, actionable subclass of PrinterCommandError.
    """


class PauseDebouncedError(PrinterError):
    """Raised by pause() when a pause was already published within the debounce window.

    This is distinct from a successful pause or a communication failure.
    Callers that receive this exception must inspect the actual printer status
    (via status()) to determine whether the printer is truly paused before
    treating the operation as successful.
    """
