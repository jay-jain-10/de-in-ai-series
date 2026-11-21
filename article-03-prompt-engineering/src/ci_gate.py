"""CI entry point: ``python -m src.ci_gate --base-registry base.yaml``.

Steps (mirrors Stage 4 of the article):
  1. Static checks: registry valid, schema == contract, no in-place edits.
  2. For every prompt whose active version moved vs. the base branch, run the
     golden dataset on baseline and candidate and compute the gate decision.
  3. Exit 1 if blocked (fails the required GitHub check => PR cannot merge).

``--static-only`` skips the Claude calls (used for forked PRs without secrets).
"""

import argparse
import json
import os
import sys
from typing import List, Optional

from .config import GovernanceConfig
from .golden import compare, load_golden, run_suite
from .prompt_registry import PromptRegistry, diff_registries
from .prompt_runner import PromptRunner


def golden_path(directory: str, name: str) -> str:
    for ext in (".json", ".yaml", ".yml"):
        p = os.path.join(directory, name + ext)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f"No golden dataset for {name} in {directory}")


def run_gate(head: PromptRegistry, base: Optional[PromptRegistry], golden_dir: str,
             client=None, cfg: Optional[GovernanceConfig] = None) -> dict:
    cfg = cfg or GovernanceConfig()
    report = {"static_problems": [], "decisions": [], "blocked": False}

    report["static_problems"] += head.validate()
    changed: list = []
    if base is not None:
        d = diff_registries(base, head)
        report["static_problems"] += d["violations"]
        changed = d["changed"]
    if report["static_problems"]:
        report["blocked"] = True

    if client is None:
        report["skipped_golden"] = [c[0] for c in changed]
        return report

    runner = PromptRunner(client, head)
    for name, base_v, head_v in changed:
        cases = load_golden(golden_path(golden_dir, name))
        cand = run_suite(runner, name, head_v, cases)
        if base_v is None:  # brand-new prompt: gate on absolute fail rate only
            baseline = cand
        else:
            baseline = run_suite(runner, name, base_v, cases)
        decision = compare(baseline, cand, cfg)
        report["decisions"].append(decision.to_dict())
        if decision.blocked:
            report["blocked"] = True
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default="prompts/registry.yaml")
    ap.add_argument("--base-registry", help="registry YAML from the base branch (git show origin/main:...)")
    ap.add_argument("--golden-dir", default="golden")
    ap.add_argument("--report", default="gate_report.json")
    ap.add_argument("--static-only", action="store_true")
    args = ap.parse_args(argv)

    head = PromptRegistry.from_file(args.registry)
    if args.base_registry and not os.path.exists(args.base_registry):
        # Silently treating this as "no base" would skip the immutability checks
        # and the golden gate entirely, letting any prompt change through.
        print(f"ERROR: base registry {args.base_registry} not found", file=sys.stderr)
        return 2
    base = PromptRegistry.from_file(args.base_registry) if args.base_registry else None

    client = None
    if not args.static_only:
        import anthropic

        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    report = run_gate(head, base, args.golden_dir, client)
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    for p in report["static_problems"]:
        print(f"STATIC: {p}")
    for d in report["decisions"]:
        status = "BLOCKED" if d["blocked"] else ("PASS (green flag)" if d["green_flag"] else "PASS")
        print(f"{d['prompt_name']} v{d['baseline_version']} -> v{d['candidate_version']}: {status}")
        for r in d["reasons"]:
            print(f"   - {r}")
    return 1 if report["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
