"""Run the sample's agent evaluations in Foundry.

    python evals/run_evals.py triage              # severity accuracy for triage-agent
    python evals/run_evals.py drafting            # "Would you send this?" for drafting-agent
    python evals/run_evals.py drafting --repeat 3 # each case three times (LLM output varies run to run)
    python evals/run_evals.py all

Each suite resets the demo data first (triage writes severities; the drafting cases expect fresh tickets
and clean customer memory), registers the custom evaluator, creates the eval, starts a run against the
latest agent version, waits, and prints the per-criterion pass counts plus a link to the portal report.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from setup import ENV, FUNCTION_ID, PROJECT, TICKETS, HttpError, arm, http  # noqa: E402

HERE = Path(__file__).resolve().parent
SCOPE = "https://ai.azure.com/.default"
JUDGE = ENV.get("TRIAGE_MODEL_DEPLOYMENT", "gpt-5.4-mini")


def mapping(response: str) -> dict:
    return {
        "query": "{{item.query}}",
        "response": response,
        "ground_truth": "{{item.ground_truth}}",
        "tool_calls": "{{sample.tool_calls}}",
        "tool_definitions": "{{sample.tool_definitions}}",
    }


def builtin(name: str, evaluator: str, response: str, threshold: int | None = None, judge: bool = True) -> dict:
    criterion = {"type": "azure_ai_evaluator", "name": name, "evaluator_name": evaluator,
                 "data_mapping": mapping(response)}
    if judge:
        criterion["initialization_parameters"] = {"deployment_name": JUDGE}
        if threshold is not None:
            criterion["initialization_parameters"]["threshold"] = threshold
    return criterion


SUITES = {
    "triage": {
        "agent": "triage-agent",
        "dataset": "triage-severity.jsonl",
        "criteria": [
            # Deterministic: the triage note must contain the expected severity.
            {"type": "string_check", "name": "SeverityMatch", "input": "{{sample.output_text}}",
             "operation": "ilike", "reference": "Severity:** {{item.ground_truth}}"},
            builtin("ToolUseQuality", "builtin.tool_use_quality", "{{sample.output_items}}", threshold=1),
            builtin("OutputQuality", "builtin.output_quality", "{{sample.output_text}}", threshold=3),
        ],
    },
    "drafting": {
        "agent": "drafting-agent",
        "dataset": "drafting-send-ready.jsonl",
        "criteria": [
            {"type": "azure_ai_evaluator", "name": "Would you send this?", "evaluator_name": "send-ready",
             "initialization_parameters": {"deployment_name": JUDGE}, "data_mapping": mapping("{{sample.output_text}}")},
            builtin("Groundedness", "builtin.groundedness", "{{sample.output_items}}", threshold=3),
            builtin("IndirectAttack", "builtin.indirect_attack", "{{sample.output_text}}", judge=False),
        ],
    },
}


def openai(method: str, path: str, body=None) -> dict:
    return http(method, f"{PROJECT}/openai/v1{path}", SCOPE, body)


def register_evaluator() -> None:
    spec = json.loads((HERE / "evaluators" / "send-ready.json").read_text(encoding="utf-8"))
    name = spec.pop("name")
    headers = {"Foundry-Features": "Evaluations=V1Preview"}
    url = f"{PROJECT}/evaluators/{name}/versions?api-version=v1"
    try:
        current = http("GET", url, SCOPE, headers=headers).get("value", [])
    except HttpError as e:
        if e.status != 404:
            raise
        current = []
    if current and current[0].get("definition", {}).get("prompt_text") == spec["definition"]["prompt_text"]:
        print(f"Evaluator {name} v{current[0]['version']} is current")
        return
    created = http("POST", url, SCOPE, {**spec, "evaluator_type": "custom"}, headers=headers)
    print(f"Evaluator {name} v{created.get('version')} registered")


def reset_demo() -> None:
    key = arm("POST", f"{FUNCTION_ID}/host/default/listkeys", "2024-04-01")["functionKeys"]["default"]
    http("POST", f"{TICKETS}/api/demo/reset?run=false", headers={"x-functions-key": key}, timeout=180)
    print("Demo data reset")


def latest_version(agent: str) -> str:
    return http("GET", f"{PROJECT}/agents/{agent}?api-version=v1", SCOPE)["versions"]["latest"]["version"]


def run_suite(name: str, repeat: int) -> dict:
    suite = SUITES[name]
    rows = [json.loads(line) for line in (HERE / suite["dataset"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    version = latest_version(suite["agent"])
    print(f"\n==> {name}: {suite['agent']} v{version}, {len(rows)} cases x {repeat}")

    evaluation = openai("POST", "/evals", {
        "name": f"{suite['agent']}-v{version}-{time.strftime('%Y%m%d-%H%M')}",
        "data_source_config": {"type": "custom", "item_schema": {
            "type": "object", "properties": {"query": {"type": "string"}, "ground_truth": {"type": "string"}},
            "required": ["query", "ground_truth"]}, "include_sample_schema": True},
        "testing_criteria": suite["criteria"],
    })
    run = openai("POST", f"/evals/{evaluation['id']}/runs", {
        "name": f"run-{time.strftime('%H%M%S')}",
        "data_source": {
            "type": "azure_ai_target_completions",
            "source": {"type": "file_content", "content": [{"item": r} for r in rows * repeat]},
            "input_messages": {"type": "template", "template": [
                {"type": "message", "role": "user", "content": "{{item.query}}"}]},
            "target": {"type": "azure_ai_agent", "name": suite["agent"], "version": version},
            # Low concurrency keeps the agents' MCP tools and model quota from throttling.
            "item_generation_params": {"type": "target_completions", "max_concurrency": 3},
        },
    })

    while run.get("status") in ("queued", "in_progress", "running"):
        time.sleep(20)
        run = openai("GET", f"/evals/{evaluation['id']}/runs/{run['id']}")
        counts = run.get("result_counts") or {}
        print(f"   {run.get('status')}: {counts.get('passed', 0)} passed, {counts.get('failed', 0)} failed, "
              f"{counts.get('errored', 0)} errored of {counts.get('total', '?')}", flush=True)

    for criterion in run.get("per_testing_criteria_results") or []:
        total = criterion.get("passed", 0) + criterion.get("failed", 0)
        print(f"   {criterion.get('testing_criteria', '?'):<22} {criterion.get('passed', 0)}/{total}")
    if run.get("report_url"):
        print(f"   Report: {run['report_url']}")
    if run.get("status") != "completed":
        print(f"   Run ended with status {run.get('status')}: {run.get('error')}")
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("suite", choices=[*SUITES, "all"])
    parser.add_argument("--repeat", type=int, default=1, help="run each case this many times")
    args = parser.parse_args()

    try:
        register_evaluator()
        for name in SUITES if args.suite == "all" else [args.suite]:
            reset_demo()
            run_suite(name, args.repeat)
    except HttpError as e:
        sys.exit(str(e))
    print("\nThe runs wrote to customer memory. Run `python scripts/reset_demo.py` before your next demo.")


if __name__ == "__main__":
    main()
