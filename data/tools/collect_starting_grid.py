"""Fetch missing questions from a private plan, with a persistent attempt budget."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
import re
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from probe_cala import BASE, ROOT


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def cached_questions(archive):
    seen = set()
    for path in archive.rglob("*.json"):
        try:
            packet = json.loads(path.read_bytes())
        except (ValueError, OSError):
            continue
        for row in packet if isinstance(packet, list) else [packet]:
            if not isinstance(row, dict) or row.get("status", 200) != 200:
                continue
            payload = row.get("payload") or {}
            value = row.get("question") or (
                payload.get("input") if isinstance(payload, dict) else None
            )
            if isinstance(value, str):
                seen.add(" ".join(value.casefold().split()))
    return seen


def collect(archive, plan):
    key = os.environ["CALA_API_KEY"].strip()
    if not key:
        raise SystemExit("Missing Cala API key")
    directory = ROOT / ".cala-private/grid"
    directory.mkdir(parents=True, exist_ok=True)
    ledger = directory / "attempts.jsonl"
    seen = cached_questions(archive)
    opener = build_opener(NoRedirect())
    gate = threading.Lock()
    budget_stop = threading.Event()

    def fetch(item):
        endpoint, question = item
        is_entity = endpoint == "entity" and re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", question
        )
        if not is_entity and endpoint not in ("query", "search"):
            raise ValueError("Unsupported endpoint")
        signature = hashlib.sha256((endpoint + "\n" + question).encode()).hexdigest()
        cache = directory / (signature + ".json")
        normalized = " ".join(question.casefold().split())
        if cache.exists() or normalized in seen:
            return
        location = (
            "/v1/entities/" + question if is_entity else "/v1/knowledge/" + endpoint
        )
        body = (
            {"properties": ["name", "headquarters_address", "lei", "employee_count"]}
            if is_entity
            else {"input": question}
        )
        request = Request(
            BASE + location,
            data=json.dumps(body).encode(),
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
        )
        for retry in range(6):
            with gate:
                if budget_stop.is_set():
                    return
                attempts = (
                    [json.loads(line) for line in ledger.read_bytes().splitlines()]
                    if ledger.exists()
                    else []
                )
                if len(attempts) >= 400:
                    budget_stop.set()
                    return
                if attempts:
                    time.sleep(max(0, 6.3 - (time.time() - attempts[-1]["time"])))
                now = datetime.now(timezone.utc)
                with ledger.open("a", encoding="utf-8") as stream:
                    print(
                        json.dumps({"id": signature, "time": now.timestamp()}),
                        file=stream,
                        flush=True,
                    )
            try:
                with opener.open(request, timeout=110) as response:
                    body = json.loads(
                        response.read().replace(key.encode(), b"[redacted]")
                    )
                    balance = {
                        k: v
                        for k, v in response.headers.items()
                        if "credit" in k.lower()
                    }
            except HTTPError as error:
                print("HTTP", error.code, flush=True)
                if error.code != 429 or retry == 5:
                    budget_stop.set()
                    raise SystemExit("Collection stopped on HTTP error") from None
                wait = error.headers.get("Retry-After", "0")
                delay = (
                    float(wait)
                    if wait.isdigit()
                    else max(0, parsedate_to_datetime(wait).timestamp() - time.time())
                )
                time.sleep(max(delay, 2 ** (retry + 1)))
                continue
            except (URLError, TimeoutError):
                print(
                    "Transport failure; request remains in ledger:",
                    signature[:10],
                    flush=True,
                )
                return
            packet = dict(
                question=question,
                endpoint=endpoint,
                retrieved_at=now.isoformat(),
                status=200,
                response=body,
                credit_headers=balance,
            )
            temp = cache.with_suffix(".tmp")
            temp.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            temp.replace(cache)
            seen.add(normalized)
            print(signature[:10], endpoint, "cached", flush=True)
            break

    items = list(dict.fromkeys(tuple(pair) for pair in json.loads(plan.read_bytes())))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fetch, items))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    lock = ROOT / ".cala-private/grid/collect.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("x"):
        pass
    try:
        collect(args.archive, args.plan)
    finally:
        lock.unlink()
