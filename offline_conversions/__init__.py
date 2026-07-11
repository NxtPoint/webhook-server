# offline_conversions/ — Google Ads offline-conversion CSV feed (gclid → paying customer).
#
# SHARED, PORTABLE PACKAGE — kept in lock-step with the CourtFlow (nextpoint) repo, exactly like the
# analytics beacon engine. It closes the loop opened by the gclid capture on core.acquisition: when a
# gclid'd visitor becomes a PAYING customer, we ledger the conversion and expose it as a Google Ads
# "scheduled upload" CSV — teaching Ads to bid for people who actually pay, not just click. NO Google
# Ads developer token / manager account needed (API Center is manager-only); Google fetches the feed
# over HTTPS on a schedule.
#
# Pieces:
#   schema.py    core.offline_conversion table (raw DDL; init(engine) called on boot).
#   recorder.py  record_from_emit(): hook in the shared track()/emit() funnel; money-event → row.
#   feed.py      build_csv(): pure Google-Ads-format renderer.
#   blueprint.py GET /feeds/google-ads/offline-conversions.csv (HTTP Basic auth; dark until env set).
#
# Per-repo glue is ONLY recorder.CONVERSION_MAP (which events are conversions + their value keys) and
# the boot/register two-liner. Portable across repos whose DB helper is `db` or `core_db.db`.

from offline_conversions.blueprint import register, offline_conv_bp  # noqa: F401
