#!/usr/bin/env python3
"""Diagnostic-only reproduction of overlapping-subscription registration order.

This script is deliberately not a catalog case and never produces a conformance
verdict. The locked Mosquitto 2.0.22 reference is registration-order sensitive
for one exact/plus/hash overlap, so the required MIXED matrix excludes that row.
Run this against a controlled endpoint to record the observed compatibility
boundary; do not turn the observation into PASS/FAIL or change the oracle.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import select
import time
import uuid

from wire import Blocked, Client, publication, string

PAYLOAD = b'overlap-diagnostic-message'
ORDERS = (
    ("exact-first", ("exact", "+", "#")),
    ("wildcard-first", ("#", "+", "exact")),
    ("plus-first", ("+", "#", "exact")),
)


class DiagnosticTrace:
    """Minimal trace surface for wire.Client; no schema event is emitted."""
    def event(self, *_args, **_fields):
        return None

    def check(self, condition, assertion, **_fields):
        if not condition:
            raise AssertionError(assertion)


def filters(base):
    return {
        "exact": base + "/item",
        "+": base + "/+",
        "#": base + "/#",
    }


def summarize(deliveries):
    return {str(qos): count for qos, count in sorted(Counter(d["qos"] for d in deliveries).items())}


def subscribe(client, packet_id, topic_filter, requested=2):
    mid = packet_id.to_bytes(2, "big")
    client.send(0x82, mid + string(topic_filter) + bytes([requested]))
    kind, body = client.recv()
    if kind != 0x90 or body[:2] != mid or len(body) != 3:
        raise AssertionError("invalid SUBACK")
    grant = body[2]
    if grant == 0x80:
        raise Blocked("subscription rejected; overlap diagnostic needs a positive subscription")
    return grant


def collect(client, window):
    """Collect PUBLISH copies and complete QoS handshakes until the bounded window."""
    deliveries = []
    deadline = time.monotonic() + window
    while True:
        if client.pending:
            kind, body = client.pending.popleft()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not select.select([client.stream], [], [], remaining)[0]:
                break
            kind, body = client.recv()
        if kind >> 4 == 3:
            value = publication((kind, body))
            deliveries.append(value)
            mid = value["mid"]
            if value["qos"] == 1:
                client.send(0x40, mid.to_bytes(2, "big"))
            elif value["qos"] == 2:
                client.send(0x50, mid.to_bytes(2, "big"))
        elif kind == 0x62 and len(body) >= 2:
            client.send(0x70, body[:2])
        elif kind == 0x70:
            continue
        else:
            raise AssertionError(f"unexpected control frame in overlap diagnostic: {kind:02x}")
    return deliveries


def run_order(endpoint, name, order, qos, window, requests):
    base = "validation/" + uuid.uuid4().hex + "/overlap"
    topic = base + "/item"
    selected = filters(base)
    trace = DiagnosticTrace()
    with Client(endpoint, trace) as subscriber, Client(endpoint, trace) as publisher:
        grants = [subscribe(subscriber, packet_id, selected[key], requests[index])
                  for index, (packet_id, key) in enumerate(zip(range(1, 4), order))]
        publisher.publish(topic, PAYLOAD, qos=qos)
        deliveries = collect(subscriber, window)
    return {
        "name": name,
        "registration": list(order),
        "filters": selected,
        "grants": grants,
        "deliveries": [dict(qos=d["qos"], dup=d["dup"], packet_id=d["mid"]) for d in deliveries],
        "qos_counts": summarize(deliveries),
        "copies": len(deliveries),
    }


def run(endpoint, qos=2, window=0.4, requests=(2, 2, 2)):
    if qos not in (0, 1, 2):
        raise ValueError("qos must be 0, 1 or 2")
    if window <= 0:
        raise ValueError("window must be positive")
    if len(requests) != 3 or any(value not in (0, 1, 2) for value in requests):
        raise ValueError("requests must contain three QoS values in registration order")
    return {
        "diagnostic": "mqtt311-overlap-registration-order",
        "status": "diagnostic-only",
        "conformance": "INCONCLUSIVE",
        "independent_review": "NotRun",
        "endpoint": endpoint,
        "qos": qos,
        "window_seconds": window,
        "requests": list(requests),
        "orders": [run_order(endpoint, name, order, qos, window, requests) for name, order in ORDERS],
        "note": "Registration-order sensitivity is a compatibility observation, not a protocol verdict. Do not convert it into PASS/FAIL or use it to change the oracle.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="host:port of a controlled MQTT3.1.1 broker")
    parser.add_argument("--qos", type=int, default=2, choices=(0, 1, 2))
    parser.add_argument("--window", type=float, default=0.4)
    parser.add_argument("--requests", default="2,2,2",
                        help="three requested QoS values in registration order, e.g. 0,1,2")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output path already exists")
    host, separator, port = args.endpoint.rpartition(":")
    if not separator or not host or not port.isdigit():
        raise SystemExit("endpoint must be host:port")
    try:
        requests = tuple(int(value) for value in args.requests.split(","))
    except ValueError as error:
        raise SystemExit("requests must be comma-separated integers") from error
    result = run((host, int(port)), args.qos, args.window, requests)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"output": str(args.output), "status": result["status"],
                      "conformance": result["conformance"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
