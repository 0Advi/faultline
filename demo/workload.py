"""Synthetic customers on every user-facing path of the shop.

Paths: home, product list, product page, recommendations, ads, image,
international shipping quote, and a buyer flow (add to cart -> view cart ->
checkout). Every request is recorded with its endpoint, phase, status,
latency and W3C trace id.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from controller.observer.endpoints import app_url
from controller.observer.http import Shop

PRODUCTS = ["66VCHSJNUP", "OLJCESPC7Z", "1YMWWN1N4O", "L9ECAV7KIM", "2ZYFJ3GM2N", "OLJCESPC7Z"]
PICTURES = ["NationalParkFoundationExplorascope.jpg", "SolarSystemColorImager.jpg", "RedFlashlight.jpg"]
INTL_ADDRESS = {"streetAddress": "Königstraße 1", "city": "Stuttgart", "state": "BW",
                "country": "Germany", "zipCode": "70839"}
TIMEOUT_S = 8


class Workload:
    """One independent probe loop per user path, so a hung dependency on one path
    cannot starve measurement of the others; plus a buyer loop for cart+checkout."""

    def __init__(self, emit: Callable[[dict], None], pause_s: float = 0.6):
        """Prepare the synthetic customers (shop client, pacing, phase label, request log)."""
        self.shop = Shop(app_url(), timeout=TIMEOUT_S)
        self.emit, self.pause_s = emit, pause_s
        self.phase = "baseline"
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.records: list[dict] = []
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.journeys = 0
        self._image_delay, self._image_flag_at = 0, -10.0

    def t(self) -> float:
        """Seconds since the workload was created."""
        return round(time.monotonic() - self.started, 2)

    def start(self):
        """Start one probe thread per browse path plus the buyer thread."""
        loops = [(name, call) for name, call in self._browse_calls()] + [("buyer", None)]
        for name, call in loops:
            target = self._buyer if call is None else self._probe
            args = () if call is None else (name, call)
            thread = threading.Thread(target=target, args=args, daemon=True)
            thread.start()
            self.threads.append(thread)
            time.sleep(0.05)

    def halt(self):
        """Stop all customers and wait for their threads."""
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=TIMEOUT_S * 2)

    # ---------------------------------------------------------------- record
    def _record(self, endpoint: str, result: dict, started: tuple[float, str], ok: bool | None = None, **extra):
        """Record one request with the phase it started in, its status, latency, timeout flag and
        trace id, and emit it to the dashboard.
        """
        # Phase is the one in force when the request *started*.
        status = result.get("http_status")
        row = {"t": self.t(), "t0": round(started[0], 2), "phase": started[1], "endpoint": endpoint,
               "ok": (status == 200) if ok is None else ok, "status": status,
               "timeout": result.get("transport_error") == "timeout",
               "error": result.get("transport_error"), "latency_ms": result.get("latency_ms"),
               "trace_id": result.get("trace_id"), **extra}
        with self.lock:
            self.records.append(row)
        self.emit(row)
        return row

    # ----------------------------------------------------------------- loops
    def _browse_calls(self):
        """The browse paths and how each one is requested (home, product list in EUR, product page,
        recommendations, ads, image, shipping quote).
        """
        shop = self.shop

        def product(i):
            """Request a product page, rotating through a fixed product list."""
            return shop.call(f"/api/products/{PRODUCTS[i % len(PRODUCTS)]}?currencyCode=EUR")

        def recommendations(i):
            """Request recommendations for a rotating product."""
            return shop.call("/api/recommendations?" + urlencode(
                {"productIds": PRODUCTS[i % len(PRODUCTS)], "sessionId": f"chaos-{i}", "currencyCode": "USD"}))

        def shipping(i):
            """Request an international shipping quote for one item."""
            return shop.call("/api/shipping?" + urlencode({
                "itemList": json.dumps([{"productId": PRODUCTS[i % len(PRODUCTS)], "quantity": 1}]),
                "currencyCode": "EUR", "address": json.dumps(INTL_ADDRESS)}))

        return [("home", lambda i: shop.call("/")),
                ("catalog", lambda i: shop.call("/api/products?currencyCode=EUR")),
                ("product", product), ("recommendations", recommendations),
                ("ads", lambda i: shop.call("/api/data?contextKeys=telescopes")),
                ("image", lambda i: self._image(PICTURES[i % len(PICTURES)])),
                ("shipping", shipping)]

    def _image(self, picture: str) -> dict:
        """Load an image the way the browser's ProductCard does: it reads the imageSlowLoad flag
        and sends it to Envoy as x-envoy-fault-delay-request (the delay is applied by Envoy)."""
        now = time.monotonic()
        if now - self._image_flag_at > 2:
            from .faults import live_flags
            variant = (live_flags() or {}).get("imageSlowLoad", "off")
            self._image_delay = {"5sec": 5000, "10sec": 10000}.get(variant, 0)
            self._image_flag_at = now
        trace_id = secrets.token_hex(16)
        req = Request(app_url() + f"/images/products/{picture}", headers={
            "x-envoy-fault-delay-request": str(self._image_delay), "Cache-Control": "no-cache",
            "traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01", "baggage": "synthetic_request=true"})
        result = {"trace_id": trace_id, "http_status": None, "transport_error": None}
        started = time.monotonic()
        try:
            with urlopen(req, timeout=TIMEOUT_S) as resp:
                resp.read()
                result["http_status"] = resp.status
        except HTTPError as error:
            result["http_status"] = error.code
        except Exception as error:
            result["transport_error"] = "timeout" if "timed out" in str(error) else type(error).__name__
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        return result

    def _checkout_intl(self, session: str) -> dict:
        """Place an order with an international (German) address, as the shop's checkout API would
        receive it.
        """
        from controller.observer.http import FAKE_CARD, SYNTHETIC_EMAIL
        result = self.shop.call("/api/checkout?currencyCode=USD", {
            "userId": session, "email": SYNTHETIC_EMAIL, "userCurrency": "USD",
            "address": INTL_ADDRESS, "creditCard": FAKE_CARD})
        body = result.pop("body") or {}
        result.update(order_id=body.get("orderId"), error=body.get("error"), error_code=body.get("code"))
        return result

    def _probe(self, endpoint: str, call):
        """Loop for one browse path: request it, record the result, pause, until stopped."""
        i = 0
        while not self.stop.is_set():
            started = (self.t(), self.phase)
            try:
                result = call(i)
            except Exception as error:
                result = {"http_status": None, "transport_error": type(error).__name__, "latency_ms": None}
            result.pop("body", None)
            self._record(endpoint, result, started)
            i += 1
            self.stop.wait(self.pause_s)

    def _buyer(self):
        """Buyer loop: add to cart, view cart, then check out (alternating domestic and
        international orders), until stopped.
        """
        shop = self.shop
        while not self.stop.is_set():
            session = "chaos-" + uuid4().hex[:12]
            started = (self.t(), self.phase)
            added = shop.add_to_cart(session, "66VCHSJNUP", 1)
            added.pop("body", None)
            self._record("cart_add", added, started)
            started = (self.t(), self.phase)
            cart = shop.read_cart(session)
            self._record("cart_get", cart, started, ok=cart["http_status"] == 200 and bool(cart.get("items")))
            if added["http_status"] == 200 and not self.stop.is_set():
                started = (self.t(), self.phase)
                # Alternate domestic and international orders: upstream slows shipping only for
                # non-US addresses, so an all-US buyer could never see that fault.
                order = shop.checkout(session, "USD") if self.journeys % 2 == 0 else self._checkout_intl(session)
                self._record("checkout", order, started,
                             ok=order["http_status"] == 200 and bool(order.get("order_id")),
                             error_code=order.get("error_code"))
            with self.lock:
                self.journeys += 1
            self.stop.wait(self.pause_s)

    # ------------------------------------------------------------- snapshots
    def window(self, seconds: float) -> list[dict]:
        """Requests completed in the last given number of seconds."""
        cutoff = self.t() - seconds
        with self.lock:
            return [r for r in self.records if r["t"] >= cutoff]
