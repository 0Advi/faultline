"""Network faults and network observation inside the sandbox's own network namespaces.

Every command runs in a short-lived helper container (deploy/nettools) started with
`--network container:<target>`: it shares the target's network namespace, so `tc` and
`iptables` change only that container's interface and firewall, and `ss` / `nstat` read only
its sockets and TCP counters. Nothing is installed into the shop's containers, and removing the
qdisc / rules (or restarting the container, which creates a fresh namespace) undoes the fault.

Fault mechanisms (Linux traffic control and netfilter):
  delay      tc netem delay <d> <jitter> distribution normal   added latency with jitter
  loss       tc netem loss <p>%                                  random packet loss
  bandwidth  tc tbf rate <r>                                     token-bucket rate limit
  corrupt    tc netem corrupt <p>% duplicate <p>%                bit errors + duplicated packets
  blackhole  iptables OUTPUT -d <peer> -j DROP                   silent link failure (SYNs vanish)
  reset      iptables OUTPUT -d <peer> -p tcp -j REJECT tcp-reset  connection refused / reset
  dns        iptables OUTPUT -d 127.0.0.11 -j DROP               Docker's embedded DNS unreachable
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "faultline-nettools:1"
DOCKERFILE_DIR = ROOT / "deploy" / "nettools"
COMMENT = "faultline-fault"            # tags every iptables rule this module adds
DOCKER_DNS = "127.0.0.11"              # Docker's embedded resolver inside each container

# Which caller is cut off from a dependency for the two-service link faults. The fault is
# applied in the caller's namespace and only for packets to the target, so every other path
# (including other callers of the same target) keeps working: a partial partition.
LINK_CALLER = {
    "payment": "checkout", "shipping": "checkout", "email": "checkout", "quote": "shipping",
    "cart": "frontend", "product-catalog": "frontend", "currency": "frontend",
    "recommendation": "frontend", "ad": "frontend", "valkey-cart": "cart",
    "astronomy-db": "product-catalog",
}


# -------------------------------------------------------------------- helper
def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run a command and return the completed process (never raises on a non-zero exit)."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def ensure_image() -> None:
    """Build the nettools helper image once if it is not present locally."""
    if _run(["docker", "image", "inspect", IMAGE], 20).returncode == 0:
        return
    built = _run(["docker", "build", "-q", "-t", IMAGE, str(DOCKERFILE_DIR)], 300)
    if built.returncode != 0:
        raise RuntimeError(f"could not build {IMAGE}: {built.stderr.strip()[-300:]}")


def netns_run(cid: str, *cmd: str, admin: bool = False, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run a command in a container's network namespace via the helper; NET_ADMIN only when the
    command reads or changes netfilter / traffic control."""
    ensure_image()
    args = ["docker", "run", "--rm", "--network", f"container:{cid}", "--label", "faultline.tool=nettools"]
    if admin:
        args += ["--cap-add", "NET_ADMIN"]
    return _run(args + [IMAGE, *cmd], timeout)


def in_netns(cid: str, *cmd: str, admin: bool = False, timeout: float = 30, check: bool = True) -> str:
    """Like netns_run, but return stdout and (by default) raise if the command failed."""
    result = netns_run(cid, *cmd, admin=admin, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} in {cid[:12]} failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def interface(cid: str) -> str:
    """The container's sandbox interface (the one carrying its default route, normally eth0)."""
    out = in_netns(cid, "ip", "-o", "route", "show", "default")
    match = re.search(r"\bdev (\S+)", out)
    if match:
        return match.group(1)
    out = in_netns(cid, "ip", "-o", "-4", "addr", "show")
    names = [line.split()[1] for line in out.splitlines() if line.split()[1] != "lo"]
    if not names:
        raise RuntimeError(f"no network interface in {cid[:12]}")
    return names[0]


# ------------------------------------------------------------------ commands
def tc_command(mechanism: str, variant: str, dev: str) -> list[str]:
    """The tc command for a traffic-control fault at the given intensity."""
    base = ["tc", "qdisc", "replace", "dev", dev, "root"]
    if mechanism == "delay":
        ms = int(variant.removesuffix("ms"))
        return base + ["netem", "delay", f"{ms}ms", f"{max(1, ms // 3)}ms", "distribution", "normal"]
    if mechanism == "loss":
        return base + ["netem", "loss", variant]
    if mechanism == "corrupt":
        return base + ["netem", "corrupt", variant, "duplicate", variant]
    if mechanism == "bandwidth":
        return base + ["tbf", "rate", variant, "burst", "32kbit", "latency", "400ms"]
    raise ValueError(mechanism)


def iptables_rules(mechanism: str, peer_ip: str | None = None) -> list[list[str]]:
    """The OUTPUT-chain rule specs (without -I/-D) for a firewall fault; each carries a comment so
    leftovers can always be found and removed."""
    tag = ["-m", "comment", "--comment", COMMENT]
    if mechanism == "blackhole":
        return [["OUTPUT", "-d", peer_ip, *tag, "-j", "DROP"]]
    if mechanism == "reset":
        return [["OUTPUT", "-d", peer_ip, "-p", "tcp", *tag, "-j", "REJECT", "--reject-with", "tcp-reset"]]
    if mechanism == "dns":
        # Docker DNATs 127.0.0.11:53 to a random port before the filter table, so match the
        # resolver address, not port 53.
        return [["OUTPUT", "-d", DOCKER_DNS, "-p", proto, *tag, "-j", "DROP"] for proto in ("udp", "tcp")]
    raise ValueError(mechanism)


# ---------------------------------------------------------------- apply/undo
def apply(undo: dict, mechanism: str, variant: str, peer_ip: str | None = None) -> str:
    """Apply a network fault to the container named in the (already journaled) undo record and
    fill in what was changed; returns a readable description of the rule."""
    cid = undo["container"]
    if mechanism in ("delay", "loss", "corrupt", "bandwidth"):
        dev = undo.get("dev") or interface(cid)
        undo["dev"] = dev
        cmd = tc_command(mechanism, variant, dev)
        in_netns(cid, *cmd, admin=True)
        return " ".join(cmd[3:])
    rules = iptables_rules(mechanism, peer_ip)
    undo["iptables"] = rules
    for rule in rules:
        in_netns(cid, "iptables", "-I", *rule, admin=True)
    return "; ".join("iptables -I " + " ".join(r) for r in rules)


def undo(record: dict) -> str:
    """Remove a network fault and verify the namespace is clean: no netem/tbf root qdisc and no
    tagged iptables rule. A replaced or restarted container already has a fresh namespace."""
    cid = record["container"]
    probe = _run(["docker", "inspect", "-f", "{{.State.Running}}", cid], 20)
    if probe.returncode != 0:
        return f"{record['service']} was replaced; network fault no longer applies"
    if probe.stdout.strip() != "true":
        return f"{record['service']} is not running; its network namespace is gone with the fault"
    if record.get("dev"):
        in_netns(cid, "tc", "qdisc", "del", "dev", record["dev"], "root", admin=True, check=False)
    for rule in record.get("iptables", []):
        for _ in range(5):          # delete every copy (an apply retried after a crash may add twice)
            if netns_run(cid, "iptables", "-D", *rule, admin=True).returncode != 0:
                break
    left = active_rules(cid, record.get("dev"))
    if left["qdisc"] or left["iptables"]:
        raise RuntimeError(f"network rules still present on {record['service']}: {left}")
    return f"{record['service']} network restored (no netem/tbf qdisc, no fault rules; verified)"


def active_rules(cid: str, dev: str | None = None) -> dict:
    """Fault-related qdiscs and tagged iptables rules currently in a container's namespace."""
    dev = dev or interface(cid)
    qdisc = [line.strip() for line in in_netns(cid, "tc", "qdisc", "show", "dev", dev).splitlines()
             if line.startswith("qdisc") and re.search(r"\b(netem|tbf)\b", line)]
    rules = [line.strip() for line in in_netns(cid, "iptables", "-S", "OUTPUT", admin=True).splitlines()
             if COMMENT in line]
    return {"dev": dev, "qdisc": qdisc, "iptables": rules}


# --------------------------------------------------------------- observation
NSTAT_KEYS = ("TcpActiveOpens", "TcpAttemptFails", "TcpEstabResets", "TcpOutRsts", "TcpOutSegs",
              "TcpRetransSegs", "TcpExtTCPTimeouts", "TcpExtTCPLostRetransmit", "TcpExtTCPSynRetrans")


def parse_nstat(text: str) -> dict:
    """Parse `nstat -az` output into {counter: value} for the TCP counters we report."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in NSTAT_KEYS and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def parse_ss_rtt(text: str) -> dict:
    """Round-trip times of established TCP connections from `ss -tin` (ms): count, median, max."""
    rtts = sorted(float(m.group(1)) for m in re.finditer(r"\brtt:([\d.]+)/", text))
    if not rtts:
        return {"connections": 0, "rtt_p50_ms": None, "rtt_max_ms": None}
    return {"connections": len(rtts), "rtt_p50_ms": round(rtts[len(rtts) // 2], 2),
            "rtt_max_ms": round(rtts[-1], 2)}


def tcp_sample(cid: str) -> dict:
    """One snapshot of a container's TCP counters and connection RTTs (with a timestamp)."""
    text = in_netns(cid, "sh", "-c", "nstat -az; echo ===; ss -tinH state established", timeout=20)
    counters, _, conns = text.partition("===")
    return {"at": time.time(), "counters": parse_nstat(counters), **parse_ss_rtt(conns)}


def tcp_window(start: dict | None, end: dict | None) -> dict | None:
    """TCP behaviour between two samples: per-second rates, retransmission share and RTT at the end."""
    if not start or not end or end["at"] <= start["at"]:
        return None
    seconds = end["at"] - start["at"]
    d = {k: end["counters"].get(k, 0) - start["counters"].get(k, 0) for k in NSTAT_KEYS}
    out_segs = d["TcpOutSegs"] or 0
    return {"seconds": round(seconds, 1),
            "segments_out_per_s": round(out_segs / seconds, 1),
            "retransmits_per_s": round(d["TcpRetransSegs"] / seconds, 2),
            "retransmit_pct": round(100 * d["TcpRetransSegs"] / out_segs, 2) if out_segs else 0.0,
            "syn_retransmits": d["TcpExtTCPSynRetrans"], "timeouts": d["TcpExtTCPTimeouts"],
            "connect_failures": d["TcpAttemptFails"], "resets_sent": d["TcpOutRsts"],
            "established_resets": d["TcpEstabResets"], "new_connections": d["TcpActiveOpens"],
            "rtt_p50_ms": end.get("rtt_p50_ms"), "rtt_max_ms": end.get("rtt_max_ms"),
            "connections": end.get("connections")}


def listening_ports(cid: str) -> list[int]:
    """TCP ports a container listens on (from its own namespace)."""
    out = in_netns(cid, "ss", "-ltnH", timeout=20, check=False)
    ports = {int(m.group(1)) for m in re.finditer(r":(\d+)\s", out + " ")}
    return sorted(ports)


def parse_netio(text: str) -> dict:
    """Parse `docker stats --format '{{.Name}} {{.NetIO}}'` lines into {name: (rx_bytes, tx_bytes)}."""
    units = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "KiB": 1024, "MiB": 2**20, "GiB": 2**30}

    def size(token: str) -> float:
        """Bytes in a docker size string such as 1.2MB."""
        match = re.match(r"([\d.]+)\s*([A-Za-z]+)", token.strip())
        return float(match.group(1)) * units.get(match.group(2), 1) if match else 0.0
    out = {}
    for line in text.splitlines():
        name, _, io = line.partition(" ")
        if "/" in io:
            rx, tx = io.split("/", 1)
            out[name] = (size(rx), size(tx))
    return out
