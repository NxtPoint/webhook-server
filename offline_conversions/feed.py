# offline_conversions/feed.py — render conversion rows as a Google Ads offline-conversion CSV.
#
# SHARED, PORTABLE, PURE (no I/O). Google Ads' scheduled upload fetches this CSV and imports each row
# against the named conversion action, matched to the original click by gclid. Format per Google's
# spec: a Parameters line (declares the time zone), a header, then one row per conversion. We emit all
# times in UTC and declare TimeZone=+0000 so there's zero ambiguity regardless of where the club is.

from datetime import timezone


def _fmt_time(dt):
    """'yyyy-MM-dd HH:mm:ss' in UTC (paired with the Parameters:TimeZone=+0000 header)."""
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _field(v):
    """CSV-escape a single field (quote + double-up quotes only when needed)."""
    s = "" if v is None else str(v)
    if any(ch in s for ch in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


def build_csv(rows):
    """rows: iterable of {gclid, action_name, occurred_at (datetime), value_minor, currency}.
    value_minor is MINOR units (cents); Google wants a major-unit decimal, so we divide by 100."""
    out = [
        "Parameters:TimeZone=+0000",
        "Google Click ID,Conversion Name,Conversion Time,Conversion Value,Conversion Currency",
    ]
    for r in rows:
        try:
            value = int(r.get("value_minor") or 0) / 100.0
        except (TypeError, ValueError):
            value = 0.0
        out.append(",".join([
            _field(r.get("gclid")),
            _field(r.get("action_name")),
            _field(_fmt_time(r["occurred_at"])),
            _field("%.2f" % value),
            _field(r.get("currency") or "ZAR"),
        ]))
    return "\n".join(out) + "\n"
