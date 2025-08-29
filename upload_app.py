# upload_app.py
from flask import Flask

app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "OK", 200

@app.get("/")
def root():
    return "root ok", 200
