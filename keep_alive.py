import os
import threading

from flask import Flask

app = Flask(__name__)


@app.get("/")
def root():
    return "OK", 200


def _run():
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
