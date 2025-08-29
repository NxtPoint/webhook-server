# wsgi.py
import os, sys, glob, importlib, traceback

print("=== WSGI BOOT ===")
print("CWD:", os.getcwd())
print("PYTHONPATH:", sys.path)
print("DIR LIST:", sorted(glob.glob("*")))
try:
    import upload_app
    print("upload_app module file:", getattr(upload_app, "__file__", "<no __file__>"))
    from upload_app import app
    # dump routes
    print("=== ROUTE DUMP (WSGI) ===")
    for r in sorted(app.url_map.iter_rules(), key=lambda x: x.rule):
        methods = ",".join(sorted(m for m in r.methods if m not in {"HEAD","OPTIONS"}))
        print(f"{r.rule:28s} -> {r.endpoint:20s} [{methods}]")
    print("=== END ROUTE DUMP ===")
except Exception as e:
    print("!!! FAILED TO IMPORT upload_app !!!")
    traceback.print_exc()
    raise

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    print(f"Starting Flask dev server on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
