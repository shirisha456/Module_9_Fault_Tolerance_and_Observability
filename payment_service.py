"""Payment Service - a fake payment processor with basic observability.

Endpoints:
  POST /pay            -> approve a payment (optional ?delay_ms=N to simulate slowness)
  GET  /health         -> liveness check
  GET  /metrics        -> in-memory request metrics (JSON)
"""
import logging
import os
import threading
import time

from flask import Flask, jsonify, request

SERVICE_NAME = "payment"
PORT = int(os.environ.get("PAYMENT_PORT", "5001"))
MAX_DELAY_MS = 5000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(SERVICE_NAME)
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # hide Flask's default access log

app = Flask(__name__)

# ---------------- in-memory metrics ----------------
metrics_lock = threading.Lock()
metrics = {
    "total_requests": 0,
    "success_count": 0,
    "failure_count": 0,
    "last_latency_ms": 0.0,
    "total_latency_ms": 0.0,
}


def record(success, latency_ms):
    with metrics_lock:
        metrics["total_requests"] += 1
        metrics["success_count" if success else "failure_count"] += 1
        metrics["last_latency_ms"] = latency_ms
        metrics["total_latency_ms"] += latency_ms


# ---------------- endpoints ----------------
@app.route("/pay", methods=["POST"])
def pay():
    start = time.monotonic()
    trace_id = request.headers.get("X-Trace-ID", "missing")
    log.info("service=%s trace_id=%s endpoint=/pay event=request_started",
             SERVICE_NAME, trace_id)

    def finish(body, status, event):
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        record(status < 400, latency_ms)
        log.info("service=%s trace_id=%s endpoint=/pay event=%s status=%d latency_ms=%.1f",
                 SERVICE_NAME, trace_id, event, status, latency_ms)
        body["trace_id"] = trace_id
        return jsonify(body), status

    # Artificial delay for failure testing
    try:
        delay_ms = int(request.args.get("delay_ms", "0"))
    except ValueError:
        return finish({"error": "delay_ms must be an integer"}, 400, "bad_request")
    if not 0 <= delay_ms <= MAX_DELAY_MS:
        return finish({"error": f"delay_ms must be 0..{MAX_DELAY_MS}"}, 400, "bad_request")

    data = request.get_json(silent=True) or {}
    order_id = data.get("order_id")
    if not order_id:
        return finish({"error": "order_id is required"}, 400, "bad_request")

    if delay_ms:
        log.info("service=%s trace_id=%s endpoint=/pay event=artificial_delay delay_ms=%d",
                 SERVICE_NAME, trace_id, delay_ms)
        time.sleep(delay_ms / 1000)

    return finish({"payment_status": "approved", "order_id": order_id}, 200, "request_finished")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": SERVICE_NAME}), 200


@app.route("/metrics", methods=["GET"])
def get_metrics():
    with metrics_lock:
        snap = dict(metrics)
    total = snap["total_requests"]
    snap["error_rate"] = round(snap["failure_count"] / total, 3) if total else 0.0
    snap["average_latency_ms"] = round(snap["total_latency_ms"] / total, 1) if total else 0.0
    snap["total_latency_ms"] = round(snap["total_latency_ms"], 1)
    snap["service"] = SERVICE_NAME
    return jsonify(snap), 200


if __name__ == "__main__":
    log.info("service=%s event=startup port=%d", SERVICE_NAME, PORT)
    app.run(host="127.0.0.1", port=PORT, threaded=True)
