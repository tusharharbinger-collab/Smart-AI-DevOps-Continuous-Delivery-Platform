#!/usr/bin/env python3
"""
scripts/demo/load_generator.py

Generates continuous HTTP POST traffic against a project's /api/checkout endpoint
to produce real sample volume (N >= 100) for CloudWatch metric ingestion and
statistical canary verification (Wald SPRT).
"""
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
import threading

def send_one_request(url):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        return False
    except Exception:
        return False

def main():
    if len(sys.argv) < 2:
        url = "http://smartcd-platform-alb-1806553190.us-east-1.elb.amazonaws.com/api/v1/testing"
    else:
        url = sys.argv[1].rstrip("/")

    duration_seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    end_time = time.time() + duration_seconds

    print(f"[*] Starting concurrent load generation against: {url}")
    print(f"[*] Target duration: {duration_seconds}s | Concurrency: {workers} workers")

    success_count = 0
    error_count = 0
    total = 0
    lock = threading.Lock()

    def worker_loop():
        nonlocal success_count, error_count, total
        while time.time() < end_time:
            ok = send_one_request(url)
            with lock:
                total += 1
                if ok:
                    success_count += 1
                else:
                    error_count += 1
                if total % 50 == 0:
                    print(f"[*] Sent: {total} requests | 200 OK: {success_count} | Error: {error_count} (Rate: {total/(time.time() - (end_time - duration_seconds)):.1f} req/s)")
            time.sleep(0.05)

    threads = []
    for _ in range(workers):
        t = threading.Thread(target=worker_loop, daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    print(f"[+] Finished load generation. Total: {total}, Success: {success_count}, Errors: {error_count}")

if __name__ == "__main__":
    main()

