"""Shared formatting for Telegram printer status."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sentinel.printer.exceptions import exception_name
from sentinel.printer.types import PrinterStatus


def format_status_caption(
    status: PrinterStatus | None,
    *,
    watcher_state: str,
    detection_enabled: bool,
    last_detection=None,
) -> str:
    printer_state = "Offline"
    print_elapsed = "—"
    extruder_temp = None
    extruder_target = None
    bed_temp = None
    bed_target = None
    progress = 0.0
    remaining_seconds = 0.0
    filename = "—"
    current_layer = 0
    total_layers = 0

    if status:
        print_state = status.print_state or (
            "printing" if status.printing else "idle"
        )
        if status.stale:
            print_state = "offline (stale data)"

        printer_state = print_state.capitalize()
        is_active = status.printing or print_state == "paused"

        if is_active:
            elapsed_seconds = max(0, int(status.elapsed_seconds))
            eh, er = divmod(elapsed_seconds, 3600)
            em, es = divmod(er, 60)
            print_elapsed = (
                f"{eh}h {em}m {es}s"
                if eh
                else f"{em}m {es}s"
            )

        extruder_temp = status.extruder_temp
        extruder_target = status.extruder_target
        bed_temp = status.bed_temp
        bed_target = status.bed_target
        progress = status.progress
        remaining_seconds = status.remaining_seconds
        filename = status.filename or "—"
        current_layer = status.current_layer
        total_layers = status.total_layers

    exception_codes = (
        getattr(status, "exception_codes", [])
        if status
        else []
    )

    time_rem = "—"
    if remaining_seconds > 0:
        hours = int(remaining_seconds // 3600)
        minutes = int((remaining_seconds % 3600) // 60)
        secs = int(remaining_seconds % 60)
        time_rem = (
            f"{hours}h {minutes}m {secs}s"
            if hours > 0
            else f"{minutes}m {secs}s"
        )

    ext_str = (
        f"{extruder_temp:.1f}°C / {extruder_target:.0f}°C"
        if extruder_temp is not None and extruder_target is not None
        else "—"
    )
    bed_str = (
        f"{bed_temp:.1f}°C / {bed_target:.0f}°C"
        if bed_temp is not None and bed_target is not None
        else "—"
    )

    progress_clamped = max(0.0, min(100.0, progress))
    filled = round(progress_clamped / 5)
    progress_bar = "█" * filled + "░" * (20 - filled)

    now = datetime.now(ZoneInfo("Europe/Tallinn"))
    updated = now.strftime("%H:%M")
    eta = (
        (now + timedelta(seconds=remaining_seconds)).strftime("%H:%M")
        if remaining_seconds > 0
        else "—"
    )

    lines = [
        f"📊 {progress:.1f}%",
        progress_bar,
        f"📐 Layer {current_layer} / {total_layers}",
        "",
        f"👁️ Watcher: {watcher_state}",
        f"⚙️ Detection: {'enabled' if detection_enabled else 'disabled'}",
        f"🖨️ Printer: {printer_state}",
        "",
        f"📄 {filename}",
        f"⏳ Remaining: {time_rem}",
        f"⏱️ Elapsed: {print_elapsed}",
        f"🏁 ETA: {eta}",
        f"🕐 Updated: {updated}",
    ]

    if exception_codes:
        lines.extend(["", "⚠️ Active printer errors:"])
        for code in exception_codes:
            lines.append(f"• {exception_name(code)} ({code})")

    lines.extend([
        "",
        f"🔥 Extruder: {ext_str}",
        f"🛏️ Bed: {bed_str}",
    ])

    if last_detection:
        detection_time = last_detection["ts_utc"]
        try:
            detection_dt = datetime.fromisoformat(
                detection_time.replace("Z", "+00:00")
            ).astimezone(ZoneInfo("Europe/Tallinn"))
            detection_time = detection_dt.strftime("%H:%M")
        except (ValueError, TypeError):
            pass

        lines.append(
            f"⚠️ Last detection: score={last_detection['score']:.2f} "
            f"at {detection_time}"
        )

    return "\n".join(lines)
