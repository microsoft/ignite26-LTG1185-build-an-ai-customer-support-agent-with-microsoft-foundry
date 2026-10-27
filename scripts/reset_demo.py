"""Reset the demo: reload the sample tickets, wipe and reseed customer memory, and (optionally) start runs.

    python scripts/reset_demo.py          # reset only
    python scripts/reset_demo.py --run    # reset, then hand every New ticket to the Case Manager (like a real queue)

Memory is wiped on every reset because each run writes what it learned to the shared customer-memory scope.
With --run, the ticket system also wipes and reseeds memory again about 15 minutes later, after those runs'
memory writes have landed, so the next demo starts clean.
"""

import sys

from setup import FUNCTION_ID, TICKETS, arm, http


def main() -> None:
    run = "--run" in sys.argv[1:]
    key = arm("POST", f"{FUNCTION_ID}/host/default/listkeys", "2024-04-01")["functionKeys"]["default"]
    result = http("POST", f"{TICKETS}/api/demo/reset?run={'true' if run else 'false'}",
                  headers={"x-functions-key": key}, timeout=180)
    print(result.get("result", result))
    print(result.get("memory", ""))
    if result.get("case_manager_started"):
        print("Case Manager started for: " + ", ".join(result["case_manager_started"]))
    print(f"Support console: {TICKETS}/api/console?code={key}")


if __name__ == "__main__":
    main()
