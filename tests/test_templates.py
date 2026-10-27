"""Static checks on the agent templates, eval datasets, and evaluator, so a broken file fails CI, not `azd up`."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDERS = {"RAI_POLICY_ID", "CHAT_MODEL_DEPLOYMENT", "TRIAGE_MODEL_DEPLOYMENT",
                "TICKET_SYSTEM_MCP_URL", "KNOWLEDGE_BASE_MCP_URL"}  # the values scripts/setup.py fills in
AGENTS = ["triage-agent", "diagnostics-agent", "drafting-agent", "knowledge-agent", "followup-agent"]


class AgentTemplateTests(unittest.TestCase):
    def test_every_agent_has_a_valid_template(self):
        for name in AGENTS:
            with self.subTest(agent=name):
                folder = ROOT / "agents" / name
                text = (folder / "agent.json").read_text(encoding="utf-8")
                self.assertLessEqual(set(re.findall(r"\$\{([A-Z0-9_]+)\}", text)), PLACEHOLDERS)
                spec = json.loads(text)
                self.assertEqual(spec["definition"]["kind"], "prompt")
                self.assertTrue(spec["description"])
                self.assertGreater(len((folder / "instructions.md").read_text(encoding="utf-8").strip()), 200)

    def test_every_agent_uses_the_guardrail(self):
        for name in AGENTS:
            spec = json.loads((ROOT / "agents" / name / "agent.json").read_text(encoding="utf-8"))
            self.assertEqual(spec["definition"].get("rai_config", {}).get("rai_policy_name"), "${RAI_POLICY_ID}", name)

    def test_ticket_tools_exist_in_the_ticket_system(self):
        app = (ROOT / "src" / "ticket-system" / "function_app.py").read_text(encoding="utf-8")
        for name in AGENTS:
            spec = json.loads((ROOT / "agents" / name / "agent.json").read_text(encoding="utf-8"))
            for tool in spec["definition"].get("tools", []):
                if tool.get("server_label") != "ticket-system":
                    continue
                allowed_tools = tool.get("allowed_tools") or {}
                names = allowed_tools.get("tool_names", []) if isinstance(allowed_tools, dict) else allowed_tools
                for allowed in names:
                    with self.subTest(agent=name, tool=allowed):
                        self.assertIn(f'tool_name="{allowed}"', app)


class EvalAssetTests(unittest.TestCase):
    def test_datasets_have_query_and_ground_truth(self):
        for path in (ROOT / "evals").glob("*.jsonl"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip():
                    row = json.loads(line)
                    self.assertTrue(row.get("query") and row.get("ground_truth"), f"{path.name}:{n}")

    def test_eval_tickets_exist_in_seed_data(self):
        seed = (ROOT / "src" / "ticket-system" / "seed_data.py").read_text(encoding="utf-8")
        for path in (ROOT / "evals").glob("*.jsonl"):
            for ticket in set(re.findall(r"EVAL-(\d+)", path.read_text(encoding="utf-8"))):
                self.assertRegex(seed, rf"_eval\(\s*{int(ticket)},", f"EVAL-{ticket}")

    def test_send_ready_evaluator_prompt_has_inputs(self):
        spec = json.loads((ROOT / "evals" / "evaluators" / "send-ready.json").read_text(encoding="utf-8"))
        for field in ("{{query}}", "{{response}}", "{{ground_truth}}"):
            self.assertIn(field, spec["definition"]["prompt_text"])


if __name__ == "__main__":
    unittest.main()
