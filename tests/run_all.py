"""
tests/run_all.py
Runs every test script one after another and prints a summary.

Each script is a plain Python program that exits non-zero on failure, so
they run in separate processes: several of them import server.py, which
loads the models and probes the cameras, and that should start clean.

Run:  python tests/run_all.py
"""

import os
import re
import subprocess
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)

SCRIPTS = [
    "test_core.py",
    "test_live_monitor.py",
    "test_attendance_dashboard.py",
    "test_attendance_api.py",
    "test_registration.py",
    "test_settings.py",
    "test_db_status.py",
    "test_dashboard_period.py",
    "test_delete_records.py",
    "test_assets.py",
    "test_auth.py",
    "test_audit_log.py",
    "test_preferences.py",
    "test_daily_summary.py",
    "test_migration.py",
    "test_people.py",
    "test_reports.py",
    "test_schedules.py",
    "test_review_queue.py",
    "test_cooldown_persistence.py",
]

RESULT = re.compile(r"RESULTS: (\d+) passed, (\d+) failed")


def main():
    total_passed = total_failed = 0
    broken = []

    for name in SCRIPTS:
        proc = subprocess.run(
            [sys.executable, os.path.join(TESTS_DIR, name)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = proc.stdout + proc.stderr
        match = RESULT.search(output)
        if match:
            passed, failed = int(match.group(1)), int(match.group(2))
            total_passed += passed
            total_failed += failed
            print(f"{name:32} {passed:4} passed  {failed:3} failed")
            for line in output.splitlines():
                if "[FAIL]" in line:
                    print("    " + line.strip())
        else:
            passed = failed = 0
            print(f"{name:32} did not finish (exit code {proc.returncode})")
            print("\n".join("    " + line for line in output.splitlines()[-15:]))
        if proc.returncode != 0 or not match or failed:
            broken.append(name)

    print("-" * 56)
    print(f"{'total':32} {total_passed:4} passed  {total_failed:3} failed")
    if broken:
        print("problems in: " + ", ".join(broken))
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
