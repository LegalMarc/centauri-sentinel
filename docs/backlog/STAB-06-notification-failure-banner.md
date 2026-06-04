# No Dashboard Banner When Notification Channels Fail

**ID:** STAB-06
**Severity:** Low
**Category:** Stability
**Status:** Closed

## Affected Files
- `sentinel/web/routes.py` — no endpoint for notification delivery status
- `templates/` — no banner component for undelivered notifications

## Description
When all notification channels (Telegram, ntfy) fail to deliver an alert, the user's only indication is the detection history in the web dashboard. There is no prominent banner or toast warning that notifications were not delivered.

## Evidence
- Detection history shows events but does not indicate delivery status.
- No notification failure state tracked or exposed to the frontend.
- Routes do not surface notification delivery errors.

## Impact
- User may believe they were notified of a detection when in fact all channels failed.
- Critical print failures go unattended because the user expected a push notification.

## Acceptance Criteria
- [x] Dashboard shows a prominent banner when notifications for recent detections failed
- [x] Banner links to detection details and indicates which channels failed
- [x] Banner auto-dismisses when notifications resume working
- [x] Tests pass
- [x] Coverage maintained ≥ 85%
