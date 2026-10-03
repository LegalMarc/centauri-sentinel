from sentinel.bot.status_format import format_status_caption
from sentinel.printer.types import PrinterStatus


def test_status_caption_shows_active_printer_exception() -> None:
    status = PrinterStatus(
        printing=False,
        elapsed_seconds=0,
        current_layer=0,
        total_layers=0,
        filename=None,
        exception_codes=[1242],
    )

    caption = format_status_caption(
        status,
        watcher_state="IDLE",
        detection_enabled=True,
    )

    assert "⚠️ Active printer errors:" in caption
    assert "• Canvas: Feed Self-Check Timeout (1242)" in caption


def test_status_caption_shows_unknown_printer_exception() -> None:
    status = PrinterStatus(
        printing=False,
        elapsed_seconds=0,
        current_layer=0,
        total_layers=0,
        filename=None,
        exception_codes=[9999],
    )

    caption = format_status_caption(
        status,
        watcher_state="IDLE",
        detection_enabled=True,
    )

    assert "• Unknown printer error (9999)" in caption
