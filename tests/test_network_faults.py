"""Network faults (tc / iptables in one container's namespace) and network evidence.

No Docker is needed: the helper container and docker calls are replaced by a fake namespace that
records every command and keeps the qdisc / iptables state it would have.
"""
import subprocess

import pytest

from demo import catalog, faults, netem, network


class FakeNamespace:
    """Records helper commands per container and tracks the fault qdisc and tagged rules."""

    def __init__(self, journal):
        """Start with clean namespaces; `journal` returns the fault journal for ordering checks."""
        self.calls, self.qdisc, self.rules, self.journal = [], {}, {}, journal

    def run(self, cid, *cmd, admin=False, timeout=30):
        """Pretend to run one command in a container's namespace."""
        self.calls.append((cid, cmd, admin))
        out, rc = "", 0
        if cmd[:3] == ("tc", "qdisc", "replace"):
            assert self.journal(), "the undo record must be journaled before the namespace changes"
            self.qdisc[cid] = " ".join(cmd[6:])
        elif cmd[:3] == ("tc", "qdisc", "del"):
            self.qdisc.pop(cid, None)
        elif cmd[:3] == ("tc", "qdisc", "show"):
            out = f"qdisc {self.qdisc[cid]} 8001: root refcnt 2\n" if cid in self.qdisc else "qdisc noqueue 0: root\n"
        elif cmd[:2] == ("iptables", "-I"):
            assert self.journal(), "the undo record must be journaled before the namespace changes"
            self.rules.setdefault(cid, []).append(list(cmd[2:]))
        elif cmd[:2] == ("iptables", "-D"):
            rule = list(cmd[2:])
            if rule in self.rules.get(cid, []):
                self.rules[cid].remove(rule)
            else:
                rc = 1
        elif cmd[:2] == ("iptables", "-S"):
            out = "-P OUTPUT ACCEPT\n" + "".join("-A " + " ".join(r) + "\n" for r in self.rules.get(cid, []))
        return subprocess.CompletedProcess(cmd, rc, out, "")


@pytest.fixture
def ns(tmp_path, monkeypatch):
    """Fixture: a fake sandbox where every service is a running container with an IP on eth0."""
    monkeypatch.setattr(faults, "JOURNAL", tmp_path / ".active-faults.json")
    fake = FakeNamespace(faults.active_faults)
    monkeypatch.setattr(netem, "ensure_image", lambda: None)
    monkeypatch.setattr(netem, "netns_run", fake.run)
    monkeypatch.setattr(netem, "interface", lambda cid: "eth0")
    monkeypatch.setattr(netem, "_run", lambda args, timeout: subprocess.CompletedProcess(args, 0, "true\n", ""))
    monkeypatch.setattr(faults, "container", lambda service: f"cid-{service}")
    monkeypatch.setattr(faults, "container_ip", lambda service: f"10.0.0.{len(service)}")
    return fake


@pytest.mark.parametrize("mechanism,variant,expected", [
    ("delay", "300ms", ["netem", "delay", "300ms", "100ms", "distribution", "normal"]),
    ("loss", "15%", ["netem", "loss", "15%"]),
    ("corrupt", "5%", ["netem", "corrupt", "5%", "duplicate", "5%"]),
    ("bandwidth", "256kbit", ["tbf", "rate", "256kbit", "burst", "32kbit", "latency", "400ms"]),
])
def test_tc_commands(mechanism, variant, expected):
    """Each traffic-control fault becomes one root qdisc replace on the container's interface."""
    assert netem.tc_command(mechanism, variant, "eth0") == ["tc", "qdisc", "replace", "dev", "eth0", "root", *expected]


def test_firewall_rules_are_tagged_and_scoped():
    """Link faults match only the peer; the DNS fault matches Docker's resolver for UDP and TCP."""
    (drop,) = netem.iptables_rules("blackhole", "10.0.0.7")
    (reset,) = netem.iptables_rules("reset", "10.0.0.7")
    dns = netem.iptables_rules("dns")
    assert drop[:3] == ["OUTPUT", "-d", "10.0.0.7"] and drop[-2:] == ["-j", "DROP"]
    assert reset[-4:] == ["-j", "REJECT", "--reject-with", "tcp-reset"]
    assert [r[r.index("-p") + 1] for r in dns] == ["udp", "tcp"] and all(netem.DOCKER_DNS in r for r in dns)
    assert all(netem.COMMENT in r for r in [drop, reset, *dns])


@pytest.mark.parametrize("sid,target,variant", [
    ("net-latency", "product-catalog", "300ms"), ("net-packet-loss", "cart", "30%"),
    ("net-bandwidth", "image-provider", "256kbit"), ("net-corruption", "currency", "1%"),
    ("net-link-blackhole", "payment", "drop"), ("net-link-reset", "cart", "reset"),
    ("net-dns-outage", "checkout", "drop"),
])
def test_network_fault_is_journaled_applied_and_fully_restored(ns, sid, target, variant):
    """Every network fault is journaled first, changes exactly one namespace, and restore leaves no
    qdisc, no tagged rule and no journal entry behind."""
    injection = faults.Injection(sid, target, variant)
    injection.apply()
    entry, = faults.active_faults()
    where = netem.LINK_CALLER[target] if sid in ("net-link-blackhole", "net-link-reset") else target
    assert entry["kind"] == "net" and entry["service"] == where
    assert {cid for cid, cmd, admin in ns.calls if admin} == {f"cid-{where}"}
    assert ns.qdisc or ns.rules[f"cid-{where}"]
    results = injection.restore()
    assert "verified" in results[-1], results
    assert not ns.qdisc and not any(ns.rules.values()) and faults.active_faults() == []


def test_link_fault_is_applied_in_the_caller_only(ns):
    """checkout -> payment: the DROP rule lives in checkout's namespace and names payment's IP."""
    injection = faults.Injection("net-link-blackhole", "payment", "drop")
    injection.apply()
    assert ns.rules["cid-checkout"][0][:3] == ["OUTPUT", "-d", "10.0.0.7"]
    assert "checkout -> payment" in injection.description
    injection.restore()


def test_restore_all_removes_a_network_fault_left_by_a_crashed_run(ns):
    """A network fault journaled by a crashed run is removed by restore_all."""
    faults.Injection("net-packet-loss", "frontend", "5%").apply()        # "crash": never restored
    assert ns.qdisc
    assert any("verified" in r for r in faults.restore_all())
    assert not ns.qdisc and faults.active_faults() == []


def test_disallowed_network_targets_are_refused(ns):
    """Observability and control-plane services cannot be given network faults."""
    with pytest.raises(PermissionError):
        faults.Injection("net-latency", "otel-collector", "300ms")
    with pytest.raises(ValueError):
        faults.Injection("net-latency", "cart", "5s")


def test_failed_rule_removal_is_reported_and_kept_journaled(ns, monkeypatch):
    """If a rule survives removal, the undo fails loudly and the journal keeps it for the next restore."""
    injection = faults.Injection("net-latency", "cart", "100ms")
    injection.apply()
    monkeypatch.setattr(netem, "active_rules", lambda cid, dev=None: {"dev": "eth0", "qdisc": ["qdisc netem"], "iptables": []})
    assert any(r.startswith("FAILED") for r in injection.restore())
    assert len(faults.active_faults()) == 1


def test_link_callers_are_real_dependencies():
    """Every link fault cuts an edge that exists in the static dependency map."""
    import json
    from demo import topology
    doc = json.loads(topology.STATIC_MAP.read_text())
    edges = {(e["from"], e["to"]) for e in doc["edges"] if e["relationship"] == "application_call"}
    for target, caller in netem.LINK_CALLER.items():
        assert caller in catalog.CONTAINER_TARGETS and target in catalog.CONTAINER_TARGETS
        assert (caller, target) in edges, (caller, target)


# ------------------------------------------------------------ observation
NSTAT = """#kernel
TcpActiveOpens                  120                0.0
TcpAttemptFails                 3                  0.0
TcpOutSegs                      1000               0.0
TcpRetransSegs                  10                 0.0
TcpExtTCPSynRetrans             2                  0.0
"""
SS = "ESTAB 0 0 10.0.0.3:5432 10.0.0.9:4100 cubic rtt:0.25/0.1 ato:40\nESTAB 0 0 x y cubic rtt:300.5/12 \n"


def test_tcp_counters_and_rtt_are_parsed():
    """nstat counters and ss RTTs are parsed; windows give rates and retransmission share."""
    start = {"at": 100.0, "counters": netem.parse_nstat(NSTAT), **netem.parse_ss_rtt(SS)}
    assert start["counters"]["TcpRetransSegs"] == 10 and start["rtt_max_ms"] == 300.5
    later = {**start, "at": 110.0, "counters": {**start["counters"], "TcpOutSegs": 3000, "TcpRetransSegs": 110}}
    window = netem.tcp_window(start, later)
    assert window["retransmits_per_s"] == 10.0 and window["retransmit_pct"] == 5.0
    assert netem.tcp_window(None, later) is None


def test_docker_netio_is_parsed():
    """docker stats NetIO strings become byte counts."""
    io = netem.parse_netio("faultline-sandbox-cart-1 1.5MB / 300kB\nfaultline-sandbox-ad-1 12KiB / 0B")
    assert io["faultline-sandbox-cart-1"] == (1.5e6, 3e5) and io["faultline-sandbox-ad-1"] == (12 * 1024, 0)


def test_hop_time_is_client_minus_server_span():
    """The network + transport time of a hop is the client span minus its child server span."""
    trace = {"processes": {"p1": {"serviceName": "frontend"}, "p2": {"serviceName": "cart"}},
             "spans": [
                 {"spanID": "a", "processID": "p1", "duration": 350_000, "references": [],
                  "tags": [{"key": "span.kind", "value": "client"}]},
                 {"spanID": "b", "processID": "p2", "duration": 40_000,
                  "references": [{"refType": "CHILD_OF", "spanID": "a"}],
                  "tags": [{"key": "span.kind", "value": "server"}]}]}
    assert network.hop_times(trace) == [{"edge": "frontend->cart", "network_ms": 310.0, "server_ms": 40.0}]


def test_network_scenarios_explain_what_the_shop_shows():
    """Degradation faults say pages get slower (not an outage); link faults name the caller."""
    assert "slower" in catalog.shop_view("net-latency", "cart")["expect"]
    assert catalog.shop_view("net-latency")["presenter"]["variant"] == "1000ms"
    assert "checkout" in catalog.scenario_view("net-link-reset", "payment")["hypothesis"]


def test_run_network_summarises_baseline_and_fault_windows(monkeypatch):
    """RunNetwork turns its three samples into baseline and fault TCP windows."""
    monkeypatch.setattr(network, "container", lambda service: "cid-x")
    counters = iter([{"TcpOutSegs": 0, "TcpRetransSegs": 0}, {"TcpOutSegs": 500, "TcpRetransSegs": 1},
                     {"TcpOutSegs": 1500, "TcpRetransSegs": 101}])
    clock = iter([0.0, 10.0, 20.0])
    monkeypatch.setattr(netem, "tcp_sample", lambda cid: {"at": next(clock), "counters": next(counters),
                                                           "connections": 3, "rtt_p50_ms": 1.0, "rtt_max_ms": 2.0})
    run = network.RunNetwork("cart")
    for name in ("baseline", "inject", "fault_end"):
        run.sample(name)
    summary = run.summary()
    assert summary["baseline"]["retransmit_pct"] == 0.2 and summary["fault"]["retransmit_pct"] == 10.0


def test_observation_failures_never_break_a_run(monkeypatch):
    """A container that cannot be read gives empty evidence instead of an exception."""
    def boom(*_):
        raise RuntimeError("no such container")
    monkeypatch.setattr(network, "container", boom)
    run = network.RunNetwork("cart")
    run.sample("baseline")
    run.capture_rules()
    assert run.summary() == {"service": "cart", "baseline": None, "fault": None, "rules": None}


def test_edge_timing_compares_phases(monkeypatch):
    """Per-hop medians are computed per phase and limited to hops touching the fault's services."""
    def trace(client_us, server_us, callee="cart"):
        return {"processes": {"p1": {"serviceName": "frontend"}, "p2": {"serviceName": callee}},
                "spans": [{"spanID": "a", "processID": "p1", "duration": client_us, "references": [],
                           "tags": [{"key": "span.kind", "value": "client"}]},
                          {"spanID": "b", "processID": "p2", "duration": server_us,
                           "references": [{"refType": "CHILD_OF", "spanID": "a"}],
                           "tags": [{"key": "span.kind", "value": "server"}]}]}
    docs = {"b1": trace(2_000, 1_000), "f1": trace(301_000, 1_000), "x": trace(5_000, 1_000, "ad")}
    monkeypatch.setattr(network, "_raw_trace", docs.get)
    records = [{"phase": "baseline", "trace_id": "b1"}, {"phase": "fault", "trace_id": "f1"},
               {"phase": "fault", "trace_id": "x"}]
    (row,) = network.edge_timing(records, {"cart"})
    assert row["edge"] == "frontend->cart"
    assert (row["baseline_network_ms"], row["fault_network_ms"]) == (1.0, 300.0)
