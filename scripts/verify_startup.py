#!/usr/bin/env python3
"""Verify the local shop workflow and telemetry; never execute a fault."""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from demo import EXPECTED, PROJECT, REVISION, ROOT, compose, host_port


def runtime_inventory():
    """Inspect the running sandbox containers: names, images, state, health and published ports."""
    ids = subprocess.check_output(compose() + ["ps", "--all", "--quiet"],
                                  text=True, timeout=30).split()
    if not ids:
        raise RuntimeError("No sandbox containers are running.")
    containers = json.loads(subprocess.check_output(["docker", "inspect", *ids],
                                                   text=True, timeout=30))
    image_ids = sorted({container["Image"] for container in containers})
    images = json.loads(subprocess.check_output(["docker", "image", "inspect", *image_ids],
                                               text=True, timeout=30))
    digests = {image["Id"]: image.get("RepoDigests", []) for image in images}
    services = []
    for container in containers:
        labels = container["Config"]["Labels"]
        if labels.get("com.docker.compose.project") != PROJECT:
            raise RuntimeError("A container outside the selected sandbox was returned.")
        state = container["State"]
        services.append({"service": labels["com.docker.compose.service"],
                         "status": state["Status"], "health": state.get("Health", {}).get("Status"),
                         "oom_killed": state["OOMKilled"], "restarts": container["RestartCount"],
                         "image": container["Config"]["Image"], "image_id": container["Image"],
                         "image_digests": digests[container["Image"]]})
    return sorted(services, key=lambda service: service["service"])


def request(url, payload=None):
    """Make one HTTP request and return a record (status, latency) and the parsed body."""
    data = json.dumps(payload).encode() if payload is not None else None
    start = time.monotonic()
    with urlopen(Request(url, data=data, headers={"Content-Type": "application/json"}), timeout=20) as response:
        body = response.read()
        result = json.loads(body) if "json" in response.headers.get("Content-Type", "") else None
        return {"url": url, "status": response.status,
                "elapsed_ms": round((time.monotonic() - start) * 1000, 2)}, result


def main():
    """Verify the sandbox started correctly (pages, APIs, cart, checkout, metrics, traces) and save
    the evidence.
    """
    evidence = {"experiment": None, "fault_executed": False,
                "kind": "startup_verification", "started_at": datetime.now(timezone.utc).isoformat(),
                "checks": [], "passed": False}
    app = f"http://127.0.0.1:{host_port('FAULTLINE_APP_PORT', 8080)}"
    metrics = f"http://127.0.0.1:{host_port('FAULTLINE_METRICS_PORT', 9090)}"

    def check(label, path, payload=None):
        """Request one shop path, record it as a check and return its body."""
        record, body = request(app + path, payload)
        record["check"] = label
        evidence["checks"].append(record)
        print(f"PASS {label}: HTTP {record['status']}", flush=True)
        return body

    try:
        evidence["upstream_revision"] = REVISION
        evidence["project"] = PROJECT
        evidence["runtime"] = runtime_inventory()
        if {service["service"] for service in evidence["runtime"]} != EXPECTED or len(evidence["runtime"]) != len(EXPECTED):
            raise RuntimeError("Sandbox does not contain exactly the selected 25 services.")
        if any(service["status"] != "running" or service["health"] not in (None, "healthy")
               or service["oom_killed"] for service in evidence["runtime"]):
            raise RuntimeError("One or more sandbox containers are not ready; inspect make status.")
        print("PASS runtime: all 25 services running; defined health checks pass", flush=True)
        check("shop home", "/")
        products = check("product catalog", "/api/products")
        if not isinstance(products, list) or not products:
            raise RuntimeError("Product catalog is empty or has an unexpected response shape.")
        evidence["catalog_product_count"] = len(products)
        recommendations = check("recommendations", "/api/recommendations?currencyCode=USD")
        if not isinstance(recommendations, list) or not recommendations:
            raise RuntimeError("No recommendations were returned.")
        currencies = check("currencies", "/api/currency")
        if "USD" not in currencies:
            raise RuntimeError("USD is missing from supported currencies.")
        user = "startup-" + str(uuid4())
        product = products[0]["id"]
        check("add item to cart", "/api/cart", {"userId": user, "item": {"productId": product, "quantity": 1}})
        cart = check("read cart", "/api/cart?" + urlencode({"sessionId": user, "currencyCode": "USD"}))
        if not any(item.get("productId") == product for item in cart.get("items", [])):
            raise RuntimeError("Added product was not present in the cart.")
        # This is the upstream demo's test-only payment data, not a real card.
        person = {"userId": user, "email": "startup-check@example.com", "userCurrency": "USD",
                  "address": {"streetAddress": "1600 Amphitheatre Parkway", "zipCode": "94043",
                              "city": "Mountain View", "state": "CA", "country": "United States"},
                  "creditCard": {"creditCardNumber": "4432-8015-6152-0454", "creditCardExpirationMonth": 1,
                                 "creditCardExpirationYear": 2039, "creditCardCvv": 672}}
        order = check("demo checkout", "/api/checkout?currencyCode=USD", person)
        if not order.get("orderId") or not order.get("items"):
            raise RuntimeError("Checkout did not return an order with items.")
        evidence["demo_order_id"] = order["orderId"]
        emptied = check("cart cleared after checkout", "/api/cart?" + urlencode({"sessionId": user, "currencyCode": "USD"}))
        if emptied.get("items"):
            raise RuntimeError("Cart was not cleared after checkout.")
        for label, url in [("Grafana", app + "/grafana/api/health"), ("Prometheus", metrics + "/-/ready")]:
            record, _ = request(url)
            record["check"] = label
            evidence["checks"].append(record)
            print(f"PASS {label}: HTTP {record['status']}", flush=True)

        deadline = time.monotonic() + 180
        while True:
            _, traces = request(app + "/jaeger/ui/api/services")
            _, series = request(metrics + "/api/v1/query?" + urlencode({"query": "count by (service_name) (target_info)"}))
            try:
                count = json.loads(subprocess.check_output(
                    compose() + ["exec", "-T", "opensearch", "curl", "-fsS", "http://localhost:9200/otel-logs*/_count"],
                    text=True, stderr=subprocess.DEVNULL, timeout=20))
            except subprocess.CalledProcessError:
                count = {"count": 0}
            names = traces.get("data") or []
            metric_services = [r["metric"]["service_name"] for r in series.get("data", {}).get("result", [])
                               if r["metric"].get("service_name")]
            evidence["telemetry"] = {"checked_at": datetime.now(timezone.utc).isoformat(),
                                     "trace_services": names, "metric_services": metric_services,
                                     "indexed_logs": count.get("count", 0)}
            if len(names) >= 3 and len(metric_services) >= 3 and count.get("count", 0) > 0:
                print("PASS telemetry: traces, metrics and indexed logs are present", flush=True)
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Telemetry did not arrive at all three backends within 180 seconds.")
            print("Waiting for telemetry ingestion...", flush=True)
            time.sleep(5)
        evidence["passed"] = True
        return 0
    except Exception as error:
        evidence["error"] = str(error)
        print(f"FAIL startup verification: {error}", file=sys.stderr)
        return 1
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        evidence["limitations"] = ["Startup verification, not a measured healthy baseline.",
                                   "Telemetry presence does not establish freshness of every signal.",
                                   "No fault, threshold abort, or recovery experiment was executed."]
        destination = ROOT / "artifacts" / "startup_verification.json"
        destination.write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"Evidence saved: {destination}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
