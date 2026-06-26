# seo/ — GSC-backed weekly SEO audit + content-opportunity finder.
#
# Pulls Google Search Console (your OWN search performance — free, authoritative) and produces a
# weekly report: what you rank for, what's moving, "striking-distance" queries (position ~5-20
# where one good post pushes you onto page 1), and low-CTR pages (title/meta rewrite wins). The
# agent then writes the next blog post against real query demand instead of volume guesses.
#
# Credentials come from a Render env var (NOT a file in git): GSC_SERVICE_ACCOUNT_JSON holds the
# whole service-account JSON. Self-gating — every entry point degrades cleanly when unset.
#
# Run:  .venv/Scripts/python -m seo.weekly_seo            (prints the markdown report)
#       Render Cron Job command:  python -m seo.weekly_seo
#
# Setup (one-time): see seo/README.md.

__all__ = []
