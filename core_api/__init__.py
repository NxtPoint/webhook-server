# core_api — HTTP surface (/api/core/*) over the canonical core.* schema.
#
# DARK by default: register(app) is a no-op unless CORE_API_ENABLED=1, so importing/
# registering it has zero effect on prod until explicitly switched on. See core_api/blueprint.py.

from core_api.blueprint import core_bp, register  # noqa: F401

__all__ = ["core_bp", "register"]
