# wsgi.py — Gunicorn entry point for the main webhook-server (API) service.
# Imports the Flask app from upload_app.py so Gunicorn can bind to it.
# Start command: gunicorn wsgi:app
from upload_app import app