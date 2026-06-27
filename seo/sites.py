# seo/sites.py — the registry of sites the SEO console audits.
#
# ONE Google account / OAuth token covers every verified GSC property, so adding a site to the
# multi-site audit is just one row here (no new credentials, no new deploy). Kept deliberately
# config-only and dependency-free so this module can move into a shared cross-site package later
# without changes.

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Site:
    slug: str                # short id used on the CLI (--site <slug>) and report filenames
    label: str               # human name in the report header
    gsc_property: str        # EXACT GSC property id: "https://www.x/" or "sc-domain:x"
    email_to: str = ""       # optional per-site report recipient (for emailed cron output)


# Add a site = add a line (after verifying it in Search Console under the same Google account).
SITES: List[Site] = [
    Site("ten-fifty5", "Ten-Fifty5", "https://www.ten-fifty5.com/", "info@ten-fifty5.com"),
    Site("nextpoint", "Next Point Tennis", "https://www.nextpointtennis.com/", ""),
]


def all_sites() -> List[Site]:
    return list(SITES)


def get(slug: str) -> Optional[Site]:
    for s in SITES:
        if s.slug == slug:
            return s
    return None
