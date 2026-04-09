# ingest_worker_wsgi.py — Gunicorn entry point for the ingest worker service.
# Imports the Flask app from ingest_worker_app.py so Gunicorn can bind to it.
# Start command: gunicorn ingest_worker_wsgi:app
from ingest_worker_app import app