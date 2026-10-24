"""Fault catalog, user-facing endpoints and the service topology layout.

Every scenario is a bounded, reversible change inside the sandbox:
  flag        flagd feature flag built into the OpenTelemetry demo services
  kill        container crash (docker kill), restored with docker start
  pause       hung / unresponsive service (docker pause), restored with unpause
  stall       slow responses: repeated short freezes (pause/unpause cycle), restored by unpausing
  partition   network partition (docker network disconnect), restored with connect
  cpu         CPU starvation (CFS quota), restored to unlimited
  db_overload expensive parallel queries inside PostgreSQL, restored by cancelling them
  traffic     sudden traffic spike from concurrent synthetic users
  combined    traffic spike while a service is down
  net         Linux network fault in one container's namespace (tc netem / tbf, iptables):
              latency + jitter, packet loss, bandwidth limit, corruption, a dropped or reset link
              between two services, or a DNS outage; restored by removing the qdisc / rules
"""
from __future__ import annotations

from .netem import LINK_CALLER

# Services the agent and operator may target with container faults. The proxy and
# the observability stack are excluded: breaking them would blind the experiment.
CONTAINER_TARGETS = ["ad", "cart", "checkout", "currency", "email", "frontend", "image-provider",
                     "payment", "product-catalog", "quote", "recommendation", "shipping",
                     "valkey-cart", "astronomy-db"]
EXCLUDED_TARGETS = {
    "frontend-proxy": "entry point used to observe the experiment",
    "otel-collector": "telemetry pipeline (would blind the evidence)",
    "prometheus": "metrics store (would blind the evidence)",
    "jaeger": "trace store (would blind the evidence)",
    "opensearch": "log store (would blind the evidence)",
    "grafana": "dashboards only", "opamp-server": "collector management only",
    "flagd": "fault-injection control plane", "flagd-ui": "flag editor only",
    "load-generator": "not used (synthetic workload is built in)",
    "telemetry-docs": "documentation only",
}

SCENARIOS: dict[str, dict] = {
    # ------------------------------------------------------------ flag faults
    "payment-failure": {
        "kind": "flag", "flag": "paymentFailure", "target": "payment", "category": "Errors",
        "title": "Payment service rejects charges", "variants": ["25%", "50%", "75%", "100%"], "default": "50%",
        "hypothesis": "If the payment service itself fails a share of charges, only checkout is affected; "
                      "customers whose valid card hit the outage get an honest, retryable server error (5xx, "
                      "'try again'), not a card-decline (4xx) message; the shop recovers within seconds."},
    "payment-unreachable": {
        # Upstream checkout creates (and leaks) a new gRPC client per order while this is on; restart
        # checkout on restore so the leak cannot keep slowing the shop for up to 30 minutes.
        "kind": "flag", "flag": "paymentUnreachable", "target": "payment", "category": "Failed dependency",
        "post_restore": "restart_checkout",
        "title": "Checkout cannot reach payment", "variants": ["on"], "default": "on",
        "hypothesis": "If checkout loses its payment connection, checkout fails fast with a server error "
                      "while browsing and cart stay healthy."},
    "cart-failure": {
        "kind": "flag", "flag": "cartFailure", "target": "cart", "category": "Errors",
        "title": "Cart service fails requests", "variants": ["25%", "50%", "100%"], "default": "50%",
        # Upstream fires this fault only in EmptyCart; checkout ignores that error, so orders still succeed.
        "hypothesis": "If the cart cannot be emptied after checkout, the order still completes and the "
                      "customer is told their cart is clear; browsing and add-to-cart keep working."},
    "product-catalog-failure": {
        "kind": "flag", "flag": "productCatalogFailure", "target": "product-catalog", "category": "Errors",
        "title": "Product catalog fails for one product", "variants": ["on"], "default": "on",
        "hypothesis": "If one product lookup fails, only that product page breaks; the catalog, other "
                      "products and checkout stay healthy."},
    "db-lock-contention": {
        "kind": "flag", "flag": "productCatalogLockContention", "target": "astronomy-db",
        "category": "Overloaded database", "title": "Database lock contention",
        "post_restore": "release_table_lock", "variants": ["on"], "default": "on",
        # Upstream holds an exclusive table lock for 30 s, re-taken every 10 s while the flag is on.
        "hypothesis": "If the product table is exclusively locked, catalog and product pages stall while "
                      "the lock is held, then recover immediately once it is released."},
    "recommendation-cache-failure": {
        "kind": "flag", "flag": "recommendationCacheFailure", "target": "recommendation",
        "category": "Resource exhaustion", "title": "Recommendation cache failure",
        "variants": ["on"], "default": "on",
        "hypothesis": "If the recommendation cache fails, recommendations get slower but product pages "
                      "and checkout are unaffected."},
    "ad-failure": {
        "kind": "flag", "flag": "adFailure", "target": "ad", "category": "Errors",
        "title": "Ad service fails", "variants": ["on"], "default": "on",
        "hypothesis": "If ads fail, the shop degrades gracefully: only the ad slot is empty."},
    "ad-high-cpu": {
        "kind": "flag", "flag": "adHighCpu", "target": "ad", "category": "Slowdown",
        "title": "Ad service CPU saturation", "variants": ["on"], "default": "on",
        "hypothesis": "If the ad service saturates its CPU, ad latency rises but other pages are unaffected."},
    "ad-gc-pauses": {
        "kind": "flag", "flag": "adManualGc", "target": "ad", "category": "Slowdown",
        "title": "Ad service garbage-collection pauses", "variants": ["on"], "default": "on",
        "hypothesis": "If the ad JVM pauses for full GCs, ad latency spikes but nothing else is affected."},
    "image-slow-load": {
        "kind": "flag", "flag": "imageSlowLoad", "target": "image-provider", "category": "Slow network",
        "stall_target": "image-provider",
        "title": "Slow product images", "variants": ["5sec", "10sec"], "default": "5sec",
        "hypothesis": "If image delivery slows to seconds, images are slow on every page (home and product "
                      "pages) while APIs and checkout keep their normal latency."},
    "intl-shipping-slowdown": {
        "kind": "flag", "flag": "intlShippingSlowdown", "target": "shipping", "category": "Slowdown",
        "title": "International shipping slowdown", "variants": ["5sec", "10sec"], "default": "5sec",
        # Upstream delays only /ship-order (called by checkout) for non-US addresses; quotes are never slowed.
        "hypothesis": "If shipping international orders is delayed, international checkouts get slower but "
                      "still succeed, and domestic checkout is unaffected."},
    "email-memory-leak": {
        "kind": "flag", "flag": "emailMemoryLeak", "target": "email", "category": "Resource exhaustion",
        "title": "Email service memory leak", "variants": ["100x", "1000x"], "default": "1000x",
        "hypothesis": "If the email service leaks memory, order confirmation may fail but orders are "
                      "still placed successfully."},
    # ------------------------------------------------------- container faults
    "service-crash": {
        "kind": "kill", "target": "product-catalog", "targets": CONTAINER_TARGETS, "category": "Crash",
        "title": "Service crash", "variants": ["kill"], "default": "kill",
        "hypothesis": "If {target} crashes, only features that need it fail, and it recovers quickly "
                      "once restarted."},
    "service-freeze": {
        "kind": "pause", "target": "cart", "targets": CONTAINER_TARGETS, "category": "Hung dependency",
        "title": "Service hangs (frozen process)", "variants": ["pause"], "default": "pause",
        "hypothesis": "If {target} stops responding, callers time out quickly instead of hanging and "
                      "unrelated pages stay fast."},
    "network-partition": {
        "kind": "partition", "target": "cart", "targets": CONTAINER_TARGETS, "category": "Network",
        "title": "Network partition", "variants": ["disconnect"], "default": "disconnect",
        "hypothesis": "If {target} is cut off from the network, dependent features fail fast with clear "
                      "errors and recover when connectivity returns."},
    "slow-responses": {
        "kind": "stall", "target": "frontend", "targets": CONTAINER_TARGETS, "category": "Slow network",
        "title": "Slow responses (intermittent stalls)", "variants": ["1s", "3s"], "default": "3s",
        "hypothesis": "If {target} intermittently stalls for seconds, callers stay within their latency "
                      "budget or fail fast, and unrelated pages are unaffected."},
    "cpu-starvation": {
        "kind": "cpu", "target": "frontend", "targets": CONTAINER_TARGETS, "category": "Resource exhaustion",
        "title": "CPU starvation", "variants": ["10%", "5%"], "default": "5%",
        "hypothesis": "If {target} only gets a fraction of a CPU, latency rises but requests still succeed."},
    "db-overload": {
        "kind": "db_overload", "target": "astronomy-db", "category": "Overloaded database",
        "title": "Overloaded database (heavy queries)", "variants": ["2 queries", "4 queries"], "default": "4 queries",
        "hypothesis": "If the product database is saturated by heavy queries, catalog latency rises but "
                      "requests keep succeeding."},
    "traffic-spike": {
        "kind": "traffic", "target": "frontend", "category": "Traffic spike",
        "title": "Sudden traffic spike", "variants": ["20 users", "40 users"], "default": "40 users",
        "hypothesis": "If traffic suddenly multiplies, latency rises moderately and no requests fail."},
    "spike-plus-crash": {
        "kind": "combined", "target": "shipping", "targets": CONTAINER_TARGETS, "category": "Combined",
        "title": "Traffic spike while a service is down", "variants": ["20 users", "40 users"],
        "default": "20 users",
        "hypothesis": "If traffic spikes while {target} is down, the failure stays contained to features "
                      "that need {target} and the rest of the shop absorbs the load."},
    # --------------------------------------------------------- network faults
    "net-latency": {
        "kind": "net", "net": "delay", "target": "product-catalog", "targets": CONTAINER_TARGETS,
        "category": "Network", "title": "Network latency + jitter (tc netem)",
        "variants": ["100ms", "300ms", "1000ms"], "default": "300ms",
        "hypothesis": "If every packet leaving {target} is delayed (with jitter), callers see the added "
                      "round trips on each call but stay within their timeouts; unrelated paths are unaffected."},
    "net-packet-loss": {
        "kind": "net", "net": "loss", "target": "product-catalog", "targets": CONTAINER_TARGETS,
        "category": "Network", "title": "Packet loss (tc netem)", "variants": ["5%", "15%", "30%"],
        "default": "15%",
        "hypothesis": "If {target} drops a share of its packets, TCP retransmits them: calls get slower "
                      "and spikier (retransmission timeouts) but mostly still succeed."},
    "net-bandwidth": {
        "kind": "net", "net": "bandwidth", "target": "image-provider", "targets": CONTAINER_TARGETS,
        "category": "Network", "title": "Bandwidth limit (tc token bucket)",
        "variants": ["1mbit", "256kbit"], "default": "256kbit",
        "hypothesis": "If {target}'s link is throttled, large responses (images, product lists) slow down "
                      "while small API calls are barely affected."},
    "net-corruption": {
        "kind": "net", "net": "corrupt", "target": "product-catalog", "targets": CONTAINER_TARGETS,
        "category": "Network", "title": "Packet corruption + duplication (tc netem)",
        "variants": ["1%", "5%"], "default": "5%",
        "hypothesis": "If {target}'s packets are corrupted or duplicated, checksums discard the bad ones "
                      "and TCP retransmits and de-duplicates: requests slow down but data stays correct."},
    "net-link-blackhole": {
        "kind": "net", "net": "blackhole", "target": "payment", "targets": sorted(LINK_CALLER),
        "category": "Network", "title": "Silent link failure between two services (packets dropped)",
        "variants": ["drop"], "default": "drop",
        "hypothesis": "If {caller} can no longer reach {target} (packets silently dropped, other callers "
                      "unaffected), {caller}'s calls to {target} time out and fail within their deadline "
                      "instead of hanging the customer."},
    "net-link-reset": {
        "kind": "net", "net": "reset", "target": "payment", "targets": sorted(LINK_CALLER),
        "category": "Network", "title": "Connection reset between two services (TCP RST)",
        "variants": ["reset"], "default": "reset",
        "hypothesis": "If {caller}'s connections to {target} are refused with TCP resets, the failure is "
                      "immediate: calls fail fast with a clear error and recover as soon as the link is back."},
    "net-dns-outage": {
        "kind": "net", "net": "dns", "target": "checkout", "targets": CONTAINER_TARGETS,
        "category": "Network", "title": "DNS outage (resolver unreachable)", "variants": ["drop"],
        "default": "drop",
        "hypothesis": "If {target} cannot resolve service names, calls on already-open connections keep "
                      "working and only new connections fail; nothing hangs indefinitely."},
}

# User-facing endpoints exercised by every synthetic journey, and the services they use.
ENDPOINTS: dict[str, dict] = {
    "home":            {"label": "Home page",           "services": ["frontend-proxy", "frontend"]},
    "catalog":         {"label": "Product list (EUR)",  "services": ["frontend-proxy", "frontend", "product-catalog", "astronomy-db", "currency"]},
    "product":         {"label": "Product page",        "services": ["frontend-proxy", "frontend", "product-catalog", "astronomy-db", "currency"]},
    "recommendations": {"label": "Recommendations",     "services": ["frontend-proxy", "frontend", "recommendation", "product-catalog", "astronomy-db"]},
    "ads":             {"label": "Ads",                 "services": ["frontend-proxy", "frontend", "ad"]},
    "image":           {"label": "Product image",       "services": ["frontend-proxy", "image-provider"]},
    "shipping":        {"label": "Shipping quote", "services": ["frontend-proxy", "frontend", "shipping", "quote", "currency"]},
    "cart_add":        {"label": "Add to cart",         "services": ["frontend-proxy", "frontend", "cart", "valkey-cart"]},
    "cart_get":        {"label": "View cart",           "services": ["frontend-proxy", "frontend", "cart", "valkey-cart", "product-catalog", "astronomy-db", "currency"]},
    "checkout":        {"label": "Checkout",            "services": ["frontend-proxy", "frontend", "checkout", "cart", "valkey-cart", "product-catalog", "astronomy-db", "currency", "shipping", "quote", "payment", "email"]},
}

# Fixed layout for the 25-service dependency graph (viewBox 0 0 1000 560).
LAYOUT = {
    "users": (60, 280), "load-generator": (60, 420),
    "frontend-proxy": (200, 280),
    "frontend": (350, 220), "image-provider": (350, 420),
    "ad": (530, 40), "recommendation": (530, 110), "product-catalog": (530, 190), "currency": (530, 270),
    "cart": (530, 350), "shipping": (530, 430), "checkout": (530, 510),
    "astronomy-db": (720, 150), "valkey-cart": (720, 350), "quote": (720, 430),
    "payment": (720, 500), "email": (720, 545),
    "flagd": (900, 40), "flagd-ui": (900, 95), "otel-collector": (900, 170), "prometheus": (900, 240),
    "jaeger": (900, 300), "opensearch": (900, 360), "grafana": (900, 420), "opamp-server": (900, 480),
    "telemetry-docs": (900, 535),
}
PLATFORM = ["flagd", "flagd-ui", "otel-collector", "prometheus", "jaeger", "opensearch", "grafana",
            "opamp-server", "telemetry-docs"]

# Interpreted services whose source can be hot-patched into the running container
# (bind-mounted over the image file) for the auto-fix demonstration.
HOT_PATCH = {
    "payment/charge.js": "/usr/src/app/charge.js",
    "payment/index.js": "/usr/src/app/index.js",
    "recommendation/recommendation_server.py": "/app/recommendation_server.py",
    "email/email_server.rb": "/email_server/email_server.rb",
}


def scenario_view(sid: str, target: str | None = None) -> dict:
    """Return a copy of a scenario with its id, the chosen (or default) target and the hypothesis
    text filled in for that target.
    """
    spec = dict(SCENARIOS[sid])
    spec["id"] = sid
    spec["target"] = target or spec["target"]
    spec["hypothesis"] = spec["hypothesis"].replace("{target}", spec["target"]).replace(
        "{caller}", LINK_CALLER.get(spec["target"], "its caller"))
    return spec


# ----------------------------------------------------------------- shop view
# How a presenter sees each fault in the real shop tab (http://localhost:8081). Derived from the
# frontend source: the shop shows no error messages anywhere; failures appear as empty sections,
# $0.00 prices or buttons that do nothing, and the browser retries reads 3 times and keeps already
# loaded data. So: reload the shop tab after the fault is live, go to the right page, do the action.
# visibility: high (obvious), medium (noticeable if you look), low (subtle), none (telemetry only).
ORDER_STEPS = ["Open any product and click Add To Cart", "On the cart page click Place Order"]
SHOP_VIEW: dict[str, dict] = {
    "payment-failure": {"path": "/", "steps": ORDER_STEPS, "visibility": "high",
                        "expect": "Place Order does nothing: the page stays on the cart with no message.",
                        "presenter": {"variant": "100%"}},
    "payment-unreachable": {"path": "/", "steps": ORDER_STEPS, "visibility": "high",
                            "expect": "Place Order does nothing: the page stays on the cart with no message."},
    "cart-failure": {"path": "/", "steps": ORDER_STEPS + ["After the confirmation page, open the cart again",
                                                         "Click Empty Cart"], "visibility": "medium",
                     "expect": "The order completes, but the bought items are still in the cart afterwards and "
                               "Empty Cart does nothing.", "presenter": {"variant": "100%"}},
    "product-catalog-failure": {"path": "/product/OLJCESPC7Z", "steps": ["Reload the product page"],
                                "visibility": "high",
                                "expect": "This product page is blank: no name, no picture, price $0.00. "
                                          "Other products still work."},
    "db-lock-contention": {"path": "/", "steps": ["Reload the home page (wait up to ~10 s for the lock)"],
                           "visibility": "high",
                           "expect": "The product grid stays empty while the table is locked; product pages "
                                     "are blank.", "presenter": {"fault_s": 50}},
    "db-overload": {"path": "/", "steps": ["Reload the home page and open a product"], "visibility": "medium",
                    "expect": "Product list and product pages load noticeably slower."},
    "recommendation-cache-failure": {"path": "/product/66VCHSJNUP", "steps": ["Reload a product page"],
                                     "visibility": "low",
                                     "expect": "Needs minutes of traffic before 'You May Also Like' slows; mainly "
                                               "visible as growing memory in the evidence."},
    "ad-failure": {"path": "/product/66VCHSJNUP", "steps": ["Reload a product page"], "visibility": "none",
                   "expect": "Only 10% of ad calls fail and the browser retries them, so customers almost never "
                             "notice. For a visible ad outage use Service crash on ad."},
    "ad-high-cpu": {"path": "/product/66VCHSJNUP", "steps": ["Reload a product page"], "visibility": "low",
                    "expect": "Ad bar under the product may load late; mostly visible as CPU in the evidence."},
    "ad-gc-pauses": {"path": "/product/66VCHSJNUP", "steps": ["Reload a product page several times"],
                     "visibility": "low", "expect": "Ad bar under the product appears late."},
    "image-slow-load": {"path": "/", "steps": ["Reload the home page"], "visibility": "high",
                        "expect": "Hot Products images arrive in slow waves and the page feels frozen. "
                                  "(Images already in your browser cache, e.g. the product page's main image, "
                                  "may still appear instantly.)", "presenter": {"variant": "5sec"}},
    "intl-shipping-slowdown": {"path": "/", "steps": ORDER_STEPS[:1] + [
        "On the cart page change Country to Germany", "Click Place Order"], "visibility": "high",
        "expect": "Place Order pauses for 5-10 s before the confirmation page. US addresses are never slowed.",
        "presenter": {"variant": "10sec"}},
    "email-memory-leak": {"path": "/", "steps": ORDER_STEPS, "visibility": "none",
                          "expect": "Orders keep succeeding: checkout ignores email failures. Watch the email "
                                    "container's memory and restarts in the evidence instead."},
    "traffic-spike": {"path": "/", "steps": ["Browse the home and product pages"], "visibility": "low",
                      "expect": "Pages get somewhat slower.", "presenter": {"variant": "40 users"}},
}

# Container faults (crash, freeze, partition, stall, CPU, spike+crash): where each target shows.
TARGET_VIEW: dict[str, dict] = {
    "frontend": {"path": "/", "steps": ["Reload any page"], "visibility": "high",
                 "expect": "The whole shop is down or crawling. A full outage trips the 'shop is down' "
                           "guardrail and auto-stops within seconds - that is the safety net working."},
    "product-catalog": {"path": "/", "steps": ["Reload the home page", "Open a product"], "visibility": "high",
                        "expect": "Empty product grid, blank product pages, and the cart looks empty."},
    "astronomy-db": {"path": "/", "steps": ["Reload the home page"], "visibility": "high",
                     "expect": "Empty product grid and blank product pages."},
    "cart": {"path": "/cart", "steps": ["Reload the cart page", "Try Add To Cart on a product"],
             "visibility": "high", "expect": "Cart looks empty and Add To Cart does nothing."},
    "valkey-cart": {"path": "/cart", "steps": ["Reload the cart page", "Try Add To Cart on a product"],
                    "visibility": "high", "expect": "Cart looks empty and Add To Cart does nothing. (A crash "
                                                    "also loses carts saved since Valkey's last snapshot.)"},
    "checkout": {"path": "/", "steps": ORDER_STEPS, "visibility": "high",
                 "expect": "Place Order does nothing: the page stays on the cart."},
    "payment": {"path": "/", "steps": ORDER_STEPS, "visibility": "high",
                "expect": "Place Order does nothing: the page stays on the cart."},
    "currency": {"path": "/", "steps": ["Switch the currency (top right) to EUR", "Reload the home page"],
                 "visibility": "medium",
                 "expect": "In EUR the product grid is empty and the cart shipping shows $0.00. In USD the shop "
                           "barely needs currency, so it looks fine."},
    "shipping": {"path": "/cart", "steps": ORDER_STEPS[:1] + ["Look at Shipping on the cart page",
                                                              "Click Place Order"], "visibility": "high",
                 "expect": "Shipping shows $0.00 and Place Order does nothing."},
    "quote": {"path": "/cart", "steps": ORDER_STEPS[:1] + ["Look at Shipping on the cart page",
                                                           "Click Place Order"], "visibility": "high",
              "expect": "Shipping shows $0.00 and Place Order fails."},
    "recommendation": {"path": "/product/66VCHSJNUP", "steps": ["Reload a product page, scroll down"],
                       "visibility": "medium", "expect": "The 'You May Also Like' section is missing."},
    "ad": {"path": "/product/66VCHSJNUP", "steps": ["Reload a product page, look under the product"],
           "visibility": "medium", "expect": "The yellow ad bar under the product is empty."},
    "image-provider": {"path": "/", "steps": ["Reload the home page"], "visibility": "high",
                       "expect": "Hot Products images are blank or very slow (cached images may still show)."},
    "email": {"path": "/", "steps": ORDER_STEPS, "visibility": "none",
              "expect": "Orders keep succeeding: checkout ignores email failures."},
}
KIND_NOTE = {
    "kill": "Calls fail fast.",
    "pause": "Calls hang ~15 s before failing, so sections stay empty/loading for up to a minute. Requests "
             "queued during the freeze still run when it ends (e.g. an order the browser gave up on).",
    "partition": "New calls fail; already-open connections hang ~15 s (callers may take a while to reconnect "
                 "after the restore).",
    "stall": "Calls are delayed up to the stall time on each freeze.",
    "cpu": "Visible on frontend (whole shop slow) and ad; CPU-light services (shipping, quote, email, "
           "image-provider) barely notice.",
    "combined": "Target is down while extra traffic hits the shop.",
    "delay": "Every network round trip of the target is slower; pages that call it several times add up.",
    "loss": "Lost packets are retransmitted after a timeout (200 ms+), so responses get slow and uneven.",
    "bandwidth": "Big responses (images, product lists) crawl; tiny API calls barely notice.",
    "corrupt": "Corrupted packets are dropped by checksum and resent: slower, never wrong.",
    "blackhole": "Only the one caller loses the target; calls hang until that caller's timeout.",
    "reset": "Only the one caller loses the target; calls fail immediately (connection refused).",
    "dns": "Only NEW connections fail (name lookup); services that keep connections open look fine.",
}


def shop_view(sid: str, target: str | None = None) -> dict:
    """What to open, do and expect in the shop tab for a scenario (and target)."""
    spec = SCENARIOS[sid]
    target = target or spec["target"]
    if sid in SHOP_VIEW:
        view = dict(SHOP_VIEW[sid])
    else:
        view = dict(TARGET_VIEW.get(target, {"path": "/", "steps": ["Reload the shop"], "visibility": "low",
                                             "expect": "Effect depends on the target."}))
        note = KIND_NOTE.get(spec.get("net") or spec["kind"], "")
        if spec.get("net") in ("delay", "loss", "bandwidth", "corrupt"):
            # Degradation, not an outage: the same pages, just slower.
            view["expect"] = f"Pages that need {target} still work but load noticeably slower."
            view["visibility"] = "medium" if view["visibility"] != "none" else "none"
            view["presenter"] = {"variant": spec["variants"][-1]}     # the most visible intensity
        elif spec.get("net") == "dns":
            view["visibility"] = "low"
        view["expect"] = f"{view['expect']} {note}".strip()
        if spec["kind"] == "cpu" and target not in ("frontend", "ad", "recommendation", "product-catalog"):
            view["visibility"] = "low"
    view.setdefault("presenter", {})
    view["presenter"] = {"fault_s": 60, **view["presenter"]}
    view["target"] = target
    return view
