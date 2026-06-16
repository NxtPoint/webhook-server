# paypal_billing/catalog.py — one-off setup: create PayPal Products + Billing Plans.
#
# Run:  python -m paypal_billing.catalog            (uses PAYPAL_ENV, sandbox by default)
#       python -m paypal_billing.catalog --dry-run  (print what would be created)
#
# Reads paypal_billing/plans.py (PLANS + PRICES), creates one Product + one recurring
# Billing Plan per recurring plan, and writes the PayPal ids to catalog.json. PAYG packs
# need no PayPal object — they are priced server-side as one-off Orders at checkout.
#
# Idempotency: if catalog.json already has a plan_id for a code (same env), it is kept
# and skipped. Delete the entry (or the file) to recreate. Re-run after a price change
# creates a NEW PayPal plan (PayPal plans are immutable on price) — update pricing.html
# and the webhook resolves the new id automatically.

from __future__ import annotations

import argparse
import sys

from paypal_billing import client, plans


def build_catalog(*, dry_run: bool = False) -> dict:
    env = client._env()
    missing = [c for c in plans.missing_prices() if plans.by_code(c) and plans.by_code(c)["plan_type"] == "recurring"]
    # Only recurring prices block plan creation; PAYG prices are checked at checkout time.
    if missing:
        raise SystemExit(
            "Refusing to run: recurring plans are missing prices in plans.PRICES -> "
            + ", ".join(missing)
            + "\nFill them in paypal_billing/plans.py first."
        )

    existing = plans.load_catalog()
    out_plans = dict(existing.get("plans") or {}) if existing.get("env") == env else {}

    for plan in plans.recurring_plans():
        code = plan["code"]
        if code in out_plans and out_plans[code].get("plan_id"):
            print(f"[skip] {code}: already has plan_id {out_plans[code]['plan_id']}")
            continue

        price = plans.price_of(code)
        print(f"[create] {code}: {plan['name']} - {price:.2f} {plans.CURRENCY}/{plan['interval'].lower()}")
        if dry_run:
            out_plans[code] = {"product_id": "<dry-run>", "plan_id": "<dry-run>",
                               "price": price, "matches": plan["matches"]}
            continue

        product = client.create_product(
            name=f"TEN-FIFTY5 {plan['name']}",
            description=f"{plan['name']} plan — {plan['matches']} match analyses per month.",
        )
        billing_plan = client.create_plan(
            product_id=product["id"],
            name=f"TEN-FIFTY5 {plan['name']} (monthly)",
            price=price,
            currency=plans.CURRENCY,
            interval_unit=plan["interval"],
            interval_count=1,
        )
        out_plans[code] = {
            "product_id": product["id"],
            "plan_id": billing_plan["id"],
            "price": price,
            "matches": plan["matches"],
        }
        print(f"         → product {product['id']}  plan {billing_plan['id']}")

    catalog = {"env": env, "currency": plans.CURRENCY, "plans": out_plans}
    if not dry_run:
        plans.save_catalog(catalog)
        print(f"\nWrote {plans.CATALOG_PATH} ({len(out_plans)} recurring plans, env={env})")
    return catalog


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create PayPal Products + Billing Plans from plans.py")
    ap.add_argument("--dry-run", action="store_true", help="print actions without calling PayPal")
    args = ap.parse_args(argv)
    build_catalog(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
