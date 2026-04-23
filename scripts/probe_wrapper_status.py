"""Probe wrapper-manager's gRPC Status RPC and emit JSON to stdout.

Used by Vinyl's AppState to decide whether a "no available instance" error reflects
a real Apple Music session expiry (needs re-auth) or a transient wrapper crash
(just retry). Must be run from AppleMusicDecrypt's poetry env so `grpc` is available.

Output (always one JSON line):
    {"ok": true,  "client_count": 1, "regions": ["US"], "ready": true}
    {"ok": false, "client_count": 0, "regions": [], "error": "<message>"}
"""

import json
import sys
from pathlib import Path

AMD_DIR = Path(__file__).resolve().parent.parent / "AppleMusicDecrypt"
sys.path.insert(0, str(AMD_DIR))

try:
    import grpc
    from google.protobuf import empty_pb2
    from src.grpc import manager_pb2_grpc
    ch = grpc.insecure_channel("127.0.0.1:8080")
    stub = manager_pb2_grpc.WrapperManagerServiceStub(ch)
    r = stub.Status(empty_pb2.Empty(), timeout=3)
    print(json.dumps({
        "ok": True,
        "client_count": r.data.client_count,
        "regions": list(r.data.regions),
        "ready": bool(r.data.ready),
    }))
except Exception as e:
    print(json.dumps({
        "ok": False,
        "client_count": 0,
        "regions": [],
        "error": str(e)[:200],
    }))
