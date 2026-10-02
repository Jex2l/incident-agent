#!/usr/bin/env python3
"""Runs all 6 fault scenarios end to end: inject, wait for the expected
alert to reach the webhook receiver, reset, and report pass/fail. This is
the Phase 2 acceptance check: each fault must trigger its alert within a
few minutes and reset must restore health.
"""
import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCENARIOS = ["oom_kill", "bad_deploy", "cpu_throttle", "kafka_lag", "crash_loop", "dependency_down"]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    results = {}
    for name in SCENARIOS:
        print(f"\n{'=' * 70}\nRunning fault scenario: {name}\n{'=' * 70}")
        start = time.time()
        try:
            mod = load(name)
            ok = mod.run()
        except Exception as e:
            print(f"[run_all] {name} raised an exception: {e}")
            ok = False
        results[name] = (ok, time.time() - start)

    print(f"\n{'=' * 70}\n== Fault injection summary ==\n{'=' * 70}")
    passed = 0
    for name, (ok, elapsed) in results.items():
        status = "ok" if ok else "FAIL"
        print(f"  [{status}] {name} ({elapsed:.0f}s)")
        passed += int(ok)
    print(f"\n{passed}/{len(results)} scenarios triggered their alert and reset cleanly")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
