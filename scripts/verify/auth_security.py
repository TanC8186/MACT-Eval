#!/usr/bin/env python
"""Authentication hardening checks (OPS03 / FE01 server contract)."""
from __future__ import annotations

import argparse
import sys
import time

import requests


def call(base: str, method: str, path: str, token: str | None = None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, base + path, json=body, headers=headers, timeout=15)
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    return resp.status_code, payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:18001/api")
    args = parser.parse_args()
    suffix = str(int(time.time() * 1000))[-8:]
    failures = []
    passed = 0

    def expect(name, actual, expected):
        nonlocal passed
        if actual == expected:
            passed += 1
            print(f"  PASS {name}: {actual}")
        else:
            failures.append(f"{name}: expected {expected}, got {actual}")
            print(f"  FAIL {name}: expected {expected}, got {actual}")

    username = f"auth-sec-{suffix}"
    status, body = call(args.base, "POST", "/auth/register", body={
        "username": username, "password": "secret123", "name": "安全测试", "role": "teacher"})
    expect("register", status, 200)
    refresh0 = body["data"]["refreshToken"]

    status, body = call(args.base, "PUT", "/auth/password", token=body["data"]["token"], body={
        "oldPassword": "secret123", "newPassword": "newsecret123"})
    expect("change password", status, 200)
    refresh1 = body["data"]["refreshToken"]

    status, _ = call(args.base, "POST", "/auth/refresh", body={"refreshToken": refresh0})
    expect("old refresh token revoked after password change", status, 401)
    status, _ = call(args.base, "POST", "/auth/refresh", body={"refreshToken": refresh1})
    expect("new refresh token works", status, 200)

    status, _ = call(args.base, "POST", "/auth/register", body={
        "username": f"weak-{suffix}", "password": "123", "role": "teacher"})
    expect("weak password rejected", status, 400)

    locked = f"locked-{suffix}"
    call(args.base, "POST", "/auth/register", body={
        "username": locked, "password": "secret123", "name": "锁定测试", "role": "teacher"})
    statuses = []
    for _ in range(6):
        status, _ = call(args.base, "POST", "/auth/login", body={
            "username": locked, "password": "wrong-password"})
        statuses.append(status)
    expect("five wrong passwords rejected", statuses[4], 401)
    expect("sixth attempt throttled", statuses[5], 429)

    print(f"\n{passed} checks passed, {len(failures)} failed")
    if failures:
        for failure in failures:
            print(" -", failure)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
