"""Allowlisted fault adapters.

Every action is built from the catalog's fixed adapter_config plus parameters
already validated against the capability. Nothing here accepts a command,
path, URL or container name from a plan or model.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import threading
import time

from ..observer.http import FAKE_CARD, Shop
from ..schemas import AdapterKind, Capability
from .flag_store import FlagSnapshot, FlagStore

ROOT = Path(__file__).resolve().parents[2]
PROTO_DIR = ROOT / "vendor" / "opentelemetry-demo" / "pb"
MONEY_MODES = {
    "valid_usd": {"currencyCode": "USD", "units": "10", "nanos": 0},
    "negative_amount": {"currencyCode": "USD", "units": "-100", "nanos": 0},
    "nanos_out_of_range": {"currencyCode": "USD", "units": "10", "nanos": 2000000000},
    "sign_mismatch": {"currencyCode": "USD", "units": "10", "nanos": -500000000},
    "unknown_currency": {"currencyCode": "XXX", "units": "10", "nanos": 0},
    "zero_amount": {"currencyCode": "USD", "units": "0", "nanos": 0},
}


# ------------------------------------------------------------------- flags
def flag_variant(cap: Capability, params: dict) -> tuple[str, str]:
    """The flag and variant a flag capability sets (the failure_rate parameter, or the catalog's
    default variant).
    """
    cfg = cap.adapter_config
    return cfg["flag"], params.get("failure_rate", cfg.get("fault_variant"))


def flag_apply(store: FlagStore, cap: Capability, params: dict) -> tuple[FlagSnapshot, FlagSnapshot]:
    """Set a flag capability's variant, guarded by the flag file's current hash; returns the
    snapshots before and after.
    """
    flag, variant = flag_variant(cap, params)
    prior = store.snapshot(flag)
    applied = store.set_variant(flag, variant, prior.sha256)
    return prior, applied


# -------------------------------------------------------- checkout fixture
def currency_mismatch(shop: Shop, cap: Capability, params: dict) -> list[dict]:
    """Fixture: place checkouts in the given (mismatched) currency and record each outcome and
    whether the cart was kept.
    """
    cfg, results = cap.adapter_config, []
    for _ in range(params["attempts"]):
        user = shop.new_cart(cfg["product_id"], cfg["quantity"])
        outcome = shop.checkout(user, params["currency_code"])
        outcome["cart_items_after"] = shop.cart_items(user)
        results.append(outcome)
    return results


def duplicate_submit(shop: Shop, cap: Capability, params: dict) -> list[dict]:
    """Fixture: submit the same cart's checkout several times concurrently and record how many
    orders were accepted (duplicate-order risk).
    """
    cfg, n = cap.adapter_config, params["concurrency"]
    user = shop.new_cart(cfg["product_id"], cfg["quantity"])
    gate = threading.Barrier(n)

    def fire(_):
        """One concurrent checkout, released together with the others by a barrier."""
        gate.wait(timeout=5)
        return shop.checkout(user, cfg["currency_code"])

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(fire, range(n)))
    accepted = [r["order_id"] for r in results if r["http_status"] == 200 and r["order_id"]]
    return [{"logical_cart": user, "requests": n, "accepted_orders": accepted,
             "duplicate_accepted": len(accepted) > 1,
             "cart_items_after": shop.cart_items(user), "attempts": results}]


def valid_checkout(shop: Shop, product_id: str = "66VCHSJNUP") -> dict:
    """Place one valid checkout and mark whether an order was created."""
    user = shop.new_cart(product_id, 1)
    result = shop.checkout(user, "USD")
    result["ok"] = result["http_status"] == 200 and bool(result["order_id"])
    return result


# ------------------------------------------------------ payment gRPC fixture
def grpc_argv(cap: Capability, params: dict, network: str, run_id: str, timeout: float) -> list[str]:
    """The docker command that calls the payment Charge RPC through grpcurl inside the sandbox
    network, with the chosen amount mode and the fake card.
    """
    cfg = cap.adapter_config
    payload = json.dumps({"amount": MONEY_MODES[params["mode"]], "creditCard": FAKE_CARD})
    return ["docker", "run", "--rm", "--network", network, "--label", f"faultline.run={run_id}",
            "--label", "faultline.tool=grpcurl",
            "-v", f"{PROTO_DIR}:/protos:ro", cfg["grpcurl_image"],
            "-plaintext", "-max-time", str(int(timeout)), "-format-error",
            "-import-path", "/protos", "-proto", "demo.proto",
            "-H", "baggage: synthetic_request=true", "-d", payload,
            cfg["endpoint"], cfg["method"]]


def payment_charge(cap: Capability, params: dict, network: str, run_id: str, timeout: float) -> list[dict]:
    """Fixture: call payment Charge directly (repeated as configured) and record exit code,
    transaction id, gRPC code, message and latency.
    """
    argv, results = grpc_argv(cap, params, network, run_id, timeout), []
    for attempt in range(params.get("repeat", 1)):
        started = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 30)
        try:
            body = json.loads(proc.stdout or proc.stderr or "{}")
        except ValueError:
            body = {"raw": (proc.stdout + proc.stderr)[-500:]}
        results.append({"attempt": attempt + 1, "mode": params["mode"], "exit_code": proc.returncode,
                        "transaction_id": body.get("transactionId"),
                        "rpc_code": body.get("code"), "rpc_message": body.get("message"),
                        "latency_ms": round((time.monotonic() - started) * 1000, 1)})
    ids = [r["transaction_id"] for r in results if r["transaction_id"]]
    if len(results) > 1:
        results.append({"summary": True, "identical_requests": len(results),
                        "distinct_transaction_ids": len(set(ids))})
    return results


# --------------------------------------------------------------- describe
def describe(cap: Capability, params: dict, network: str = "faultline-sandbox",
             app_url: str = "http://127.0.0.1:8080") -> list[str]:
    """Human-readable dry-run of exactly what a live run would do."""
    if cap.adapter == AdapterKind.FLAG:
        flag, variant = flag_variant(cap, params)
        return [f"snapshot deploy/flagd/demo.flagd.json and {flag} prior variant",
                f"atomically set {flag}.defaultVariant={variant!r} (hash-guarded)",
                "hold for duration_seconds while probing checkout",
                f"restore {flag} prior variant (hash-guarded) and verify valid checkouts"]
    if cap.id == "checkout-currency-mismatch":
        return [f"{params['attempts']}x: new synthetic cart, POST {app_url}/api/checkout "
                f"userCurrency={params['currency_code']!r}, then read cart"]
    if cap.id == "checkout-duplicate-submit":
        return [f"one synthetic cart; {params['concurrency']} identical POST {app_url}/api/checkout "
                "released together by a barrier; record accepted order IDs and cart"]
    if cap.adapter == AdapterKind.PAYMENT_GRPC:
        return [" ".join(grpc_argv(cap, params, network, "<run-id>", 10))
                + f"  (x{params.get('repeat', 1)})"]
    return [f"{cap.adapter.value}: no executable adapter"]
