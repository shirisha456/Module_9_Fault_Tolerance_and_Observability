"""Checkout Service - calls the Payment Service over HTTP with basic observability.

Endpoints:
  POST /checkout   -> process an order by calling Payment
                      (optional ?payment_delay_ms=N is forwarded to Payment for failure testing)
  GET  /health     -> liveness check
  GET  /metrics    -> in-memory request metrics (JSON)
"""
import logging
import os
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, request

SERVICE_NAME = "checkout"
PORT = int(os.environ.get("CHECKOUT_PORT", "5050"))  # 5000 is taken by macOS AirPlay
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://127.0.0.1:5001/pay")
PAYMENT_TIMEOUT_SECONDS = 1.0

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
    "timeout_count": 0,
    "last_latency_ms": 0.0,
    "total_latency_ms": 0.0,
}


def record(success, latency_ms, timeout=False):
    with metrics_lock:
        metrics["total_requests"] += 1
        metrics["success_count" if success else "failure_count"] += 1
        if timeout:
            metrics["timeout_count"] += 1
        metrics["last_latency_ms"] = latency_ms
        metrics["total_latency_ms"] += latency_ms


# ---------------- endpoints ----------------
@app.route("/checkout", methods=["POST"])
def checkout():
    start = time.monotonic()
    trace_id = str(uuid.uuid4())  # Checkout always starts a new trace
    log.info("service=%s trace_id=%s endpoint=/checkout event=request_started",
             SERVICE_NAME, trace_id)

    def finish(body, status, event):
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        record(status < 400, latency_ms, timeout=(status == 504))
        level = logging.INFO if status < 400 else logging.WARNING
        log.log(level, "service=%s trace_id=%s endpoint=/checkout event=%s status=%d latency_ms=%.1f",
                SERVICE_NAME, trace_id, event, status, latency_ms)
        body["trace_id"] = trace_id
        return jsonify(body), status

    # ---- validate input ----
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return finish({"error": "body must be a JSON object"}, 400, "bad_request")
    order_id = data.get("order_id")
    amount = data.get("amount")
    if not isinstance(order_id, str) or not order_id:
        return finish({"error": "order_id must be a non-empty string"}, 400, "bad_request")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        return finish({"error": "amount must be a positive number"}, 400, "bad_request")

    # Optional failure-test control, forwarded to Payment
    params = {}
    if "payment_delay_ms" in request.args:
        params["delay_ms"] = request.args["payment_delay_ms"]

    # ---- call Payment over HTTP, propagating the trace ID ----
    try:
        resp = requests.post(
            PAYMENT_URL,
            json={"order_id": order_id, "amount": amount},
            params=params,
            headers={"X-Trace-ID": trace_id},
            timeout=PAYMENT_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        return finish({"error": "payment_timeout"}, 504, "payment_timeout")
    except requests.RequestException as exc:
        log.warning("service=%s trace_id=%s event=payment_unreachable error=%s",
                    SERVICE_NAME, trace_id, type(exc).__name__)
        return finish({"error": "payment_unavailable"}, 503, "payment_unavailable")

    if resp.status_code != 200:
        return finish({"error": "payment_failed", "payment_status_code": resp.status_code},
                      502, "payment_error")

    payment = resp.json()
    return finish({"order_id": order_id, "status": "confirmed",
                   "payment_status": payment.get("payment_status")}, 200, "request_finished")


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
    log.info("service=%s event=startup port=%d payment_url=%s timeout_s=%.1f",
             SERVICE_NAME, PORT, PAYMENT_URL, PAYMENT_TIMEOUT_SECONDS)
    app.run(host="127.0.0.1", port=PORT, threaded=True)
