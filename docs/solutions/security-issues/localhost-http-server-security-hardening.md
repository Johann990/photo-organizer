---
title: "Localhost HTTP Server: DNS Rebinding, Content-Length Cap, and Input Membership Validation"
date: 2026-06-12
category: docs/solutions/security-issues
module: webreview
problem_type: security_issue
component: tooling
symptoms:
  - Local stdlib HTTP server accepted requests with any Host header value
  - No Content-Length limit — oversized POST body could exhaust memory
  - POST /decision accepted file_ids outside the claimed cluster
root_cause: missing_validation
resolution_type: code_fix
severity: high
tags:
  - http-server
  - dns-rebinding
  - localhost
  - input-validation
  - content-length
  - security-hardening
---

# Localhost HTTP Server: DNS Rebinding, Content-Length Cap, and Input Membership Validation

## Problem

`webreview.py` runs a local `http.server.BaseHTTPRequestHandler` bound to `127.0.0.1`. A code review identified three security gaps: no Host header validation (DNS rebinding exposure), no limit on POST body size (memory exhaustion), and no check that the `kept`/`dropped` file_ids in a `/decision` POST actually belong to the claimed cluster.

## Symptoms

- Server accepted requests with any `Host:` header, not just `127.0.0.1` or `localhost`
- A malicious page on any domain could trick a browser into sending requests to the local server (DNS rebinding attack)
- An oversized `Content-Length` would cause `rfile.read(n)` to buffer that many bytes before any rejection
- A crafted payload could supply file_ids from other clusters and affect unrelated decision state

## What Didn't Work

No prior runtime failure — these were proactive findings from a structured code review. The key insight: **binding to `127.0.0.1` alone does not prevent DNS rebinding**. The OS enforces the bind address, but DNS rebinding exploits the browser's Same-Origin Policy by pointing a domain's DNS to `127.0.0.1`, then sending requests with that domain as the `Host` header. The server sees a localhost connection and processes it normally.

## Solution

### 1. Host header validation (DNS rebinding fix)

```python
# Strip port: "127.0.0.1:8080" → "127.0.0.1"
host = self.headers.get("Host", "").split(":")[0]
if host not in ("127.0.0.1", "localhost"):
    self._send(403, b"forbidden", "text/plain")
    return
```

The check runs at the top of both `do_GET` and `do_POST` before any state is read.

### 2. Content-Length cap

```python
_MAX_POST_BYTES = 64 * 1024  # 64 KiB — far larger than any valid decision payload

length = min(
    int(self.headers.get("Content-Length", 0)),
    _MAX_POST_BYTES,
)
payload = json.loads(self.rfile.read(length) or b"{}")
```

Define the constant at module level so the limit is visible and easy to adjust.

### 3. Cluster membership validation

```python
member_set = set(members)  # authoritative id set for this cluster
kept    = {int(f) for f in payload.get("kept",    [])} & member_set
dropped = {int(f) for f in payload.get("dropped", [])} & member_set
```

The `& member_set` intersection is a pure allow-list: only ids that are both in the payload _and_ in the cluster are acted on. Any out-of-cluster id is silently discarded before lock acquisition.

## Why This Works

**DNS rebinding**: A rebinding attack uses the attacker's domain as the `Host` header value. Checking that the hostname portion is exactly `127.0.0.1` or `localhost` blocks the attack at handler entry before any resource is accessed. Stripping the port first (`split(":")[0]`) handles `Host: 127.0.0.1:8080` correctly without needing to know the port at check time.

**Content-Length cap**: `rfile.read(n)` eagerly allocates `n` bytes. A forged `Content-Length: 2147483647` header would cause the server to try to allocate 2 GiB. Capping to 64 KiB costs nothing for legitimate payloads and removes the exhaustion vector entirely.

**Membership validation**: The intersection ensures the server only acts on ids it knows about in the current cluster. A payload naming ids from a different cluster index cannot affect those clusters because the ids are not in `member_set`. This also protects against integer coercion bugs where a client sends a string that happens to match a real id.

## Prevention

- Apply all three defenses to any `http.server`-based local tool, even when bound to `127.0.0.1`. Localhost binding is necessary but not sufficient.
- Order the checks: Host validation first (cheapest, rejects before any parsing), then Content-Length cap, then payload parsing, then id validation.
- Define `_MAX_POST_BYTES` as a named module constant — magic numbers in `min()` calls are easy to miss in review.
- For every request that references an entity by id, intersect or validate the supplied ids against the server's own authoritative set before touching shared state.
- Acquire locks only after all input validation passes.

## Related Issues

- `photo_organizer/webreview.py` — `_make_handler` → `do_POST`, `_MAX_POST_BYTES`
- DNS rebinding is documented in the OWASP Testing Guide (WSTG-CONF-09)
