# marketing_crm.tracking — product event instrumentation (Prompt 3).
#
# track(...) is the single entry point. Fire-and-forget: it NEVER raises and NEVER blocks the
# request (work happens on a daemon thread). Dual-emits to core.usage_event (always) and Amplitude
# (if AMPLITUDE_API_KEY set). No-op unless TRACKING_ENABLED=1. Event names come from
# marketing_crm/contracts/events.md (see events.py).

from marketing_crm.tracking.events import EVENTS  # noqa: F401
from marketing_crm.tracking.client import track   # noqa: F401
from marketing_crm.tracking.beacon import page_bp, register as register_beacon  # noqa: F401

__all__ = ["track", "EVENTS", "page_bp", "register_beacon"]
