#!/usr/bin/env bash
set -e

PORT_TO_BIND="${PORT:-8088}"

echo "==> Running Superset DB upgrade (with retries)..."
for i in 1 2 3 4 5; do
  if superset db upgrade; then
    echo "==> DB upgrade successful"
    break
  fi
  echo "==> DB upgrade failed (attempt $i); sleeping 5s and retrying..."
  sleep 5
done

# --- Hard upsert of the admin user from environment vars ---
if [ -n "$SUPERSET_ADMIN_USER" ] && [ -n "$SUPERSET_ADMIN_PASSWORD" ] && [ -n "$SUPERSET_ADMIN_EMAIL" ]; then
  echo "==> Ensuring admin user $SUPERSET_ADMIN_USER via Superset SecurityManager"
  python - <<'PY'
import os
from superset.app import create_app
from flask_appbuilder.security.sqla.models import User
app = create_app()
username = os.environ["SUPERSET_ADMIN_USER"]
email = os.environ["SUPERSET_ADMIN_EMAIL"]
password = os.environ["SUPERSET_ADMIN_PASSWORD"]
with app.app_context():
    sm = app.appbuilder.sm
    admin_role = sm.find_role("Admin")
    user = sm.find_user(username=username)
    if user is None:
        # create
        sm.add_user(
            username=username,
            first_name="Admin",
            last_name="User",
            email=email,
            role=admin_role,
            password=password,
            # for FAB >=4 this kw is 'is_active', for older it's 'active'; handle both
            is_active=True
        )
        print(f"[OK] Created user {username}")
    else:
        # ensure active, email, role, and reset password
        user.active = True if hasattr(user, "active") else True
        user.is_active = True if hasattr(user, "is_active") else True
        user.email = email
        if admin_role and admin_role not in user.roles:
            user.roles = [admin_role]
        sm.update_user(user, password=password)
        print(f"[OK] Updated password/role for {username}")
PY
fi

echo "==> Running superset init..."
superset init || true

echo "==> Starting Gunicorn..."
exec gunicorn -w 4 -k gevent --timeout 300 -b 0.0.0.0:"$PORT_TO_BIND" "superset.app:create_app()"