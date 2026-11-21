# Article 3: Prompt Governance Is the New Schema Governance

Part 3 of the [Data Engineering in the Age of AI](https://github.com/jay-jain-10/de-in-ai-series) series.

---

## Overview

Runnable companion code for treating prompts as versioned production infrastructure: a YAML prompt registry (Git as source of truth, S3 as the runtime read target), Pydantic output contracts, golden-dataset regression tests, a CI gate that blocks degrading prompt changes, a `prompt_version` lineage column in Snowflake, drift monitoring, and automated rollback by moving a registry pointer.

Numbers and thresholds follow the article:

| Control | Threshold |
|---|---|
| CI gate: golden-case failures | block if > 5% |
| CI gate: average confidence drop vs baseline | block if > 0.05 |
| CI gate: green flag | candidate passes > 5% more cases |
| Output divergence (monitoring) | warn > 10%, alert/rollback > 15% |
| Confidence degradation | alert/rollback if mean drops > 0.05 |
| Schema compliance | target > 95%, alert/rollback < 90% |
| Hallucination rate | alert/rollback > 5%, or rise > 3 points vs previous version |
| Sampling | 1% of production requests |
| Auto-rollback window | 7 days after deploy |
| Rollback SLA | < 5 minutes (pointer update + publish) |

## Architecture

```
 PR edits prompts/registry.yaml (Git = source of truth)
        |
        v
 GitHub Actions (prompt-ci.yml)
   1. pytest + static registry checks (schema == Pydantic contract, versions immutable)
   2. golden gate: run baseline vs candidate on golden/*.json|yaml
        fail >5% | regressions >5% | confidence drop >0.05  --> BLOCK PR
   3. merge to main  --> tag <prompt>-v<version>
   4. manual release --> publish registry.yaml to versioned S3 bucket
        |
        v
 S3 registry (runtime)  <---- AutoRollback moves active_version pointer
        |  pin() at DAG-run start (idempotent per run)
        v
 Airflow/MWAA --> PromptRunner --> Claude --> Pydantic contract --> Snowflake MERGE
                                                       key (input_id, prompt_name, prompt_version)
        |
        v
 Monitoring: 1% sample, re-run on previous version
   divergence | confidence histogram | schema compliance | hallucination
        |                                   |
        v                                   v
 CloudWatch custom metrics            SNS alerts + AutoRollback (7-day window)
```

## Tech Stack

| Component | Tool |
|-----------|------|
| AI Model | Claude Haiku (`claude-haiku-4-5-20251001`, same as Article 1); Sonnet for escalation (`ESCALATION_MODEL`, default `claude-sonnet-5-5`) |
| Registry | YAML in Git, versioned S3 bucket at runtime |
| Contracts | Pydantic v2 (`extra="forbid"`) |
| Tests | pytest (fully offline) |
| CI | GitHub Actions |
| Warehouse | Snowflake (MERGE with `prompt_version` key), dbt singular test |
| Monitoring | CloudWatch custom metrics, SNS |
| Language | Python 3.10+ |

## Project Structure

```
article-03-prompt-engineering/
├── prompts/
│   └── registry.yaml                 # versioned prompts + active_version pointers + history
├── golden/
│   ├── sentiment_classifier.json     # includes the article's 3 examples
│   ├── escalation_classifier.yaml    # includes the article's 3 escalation cases
│   └── vendor_extractor.json         # grounding / hallucination cases
├── src/
│   ├── config.py                     # env config + governance thresholds
│   ├── prompt_registry.py            # registry, pin(), rollback(), validation, S3 store
│   ├── output_contracts.py           # Pydantic contracts + hallucination (grounding) check
│   ├── prompt_runner.py              # Claude call, contract enforcement, retry with schema example
│   ├── golden.py                     # golden cases, suite runner, gate decision
│   ├── ci_gate.py                    # CLI used by GitHub Actions
│   ├── monitoring.py                 # sampling, drift metrics, alerts, CloudWatch/SNS publisher
│   ├── rollback.py                   # automated rollback within the 7-day window
│   └── snowflake_loader.py           # prompt_version lineage, (input_id, prompt_name, prompt_version) MERGE
├── dbt_tests/
│   └── assert_high_conf_escalations_use_current_prompt.sql
├── tests/                            # 97 offline tests
├── .github/workflows/prompt-ci.yml
├── .env.example
├── requirements.txt
└── README.md
```

## How It Works

- **Registry**: each prompt has `versions` (immutable) and an `active_version` pointer. Editing a published version in place is rejected by CI; add a new higher version instead.
- **Runtime indirection**: `registry.pin()` snapshots versions at DAG-run start, so a mid-run pointer change does not affect an in-flight run.
- **Contracts**: `SentimentOutput`, `EscalationOutput`, `VendorExtraction`. A renamed field (`vendor_company`, `sentiment_score`) fails validation instead of becoming NULLs downstream. On violation the runner retries once with the JSON schema attached.
- **Gate**: `python -m src.ci_gate --base-registry base.yaml` runs the golden suite for every prompt whose active version moved, on baseline and candidate, and exits 1 if blocked (2 if the base registry file is missing, rather than silently skipping the checks). `--static-only` skips model calls, so on forked PRs prompt changes are only statically checked. Static checks reject deleted prompts/versions and in-place edits to a published version's prompt text, model, contract, `validation_schema` or `max_tokens`, and require quoted version strings.
- **Lineage**: every row carries `prompt_version`; reprocessing with a new version inserts new rows rather than overwriting. The article keys the MERGE on `(input_id, prompt_version)`; because `prompt_classifications` is shared by all prompts, the key here also includes `prompt_name` (otherwise two prompts that both have a v1.3 would overwrite each other's rows for the same input).
- **Rollback**: `AutoRollback` flags the bad version `rolled_back`, moves the pointer to the newest earlier good version, saves the YAML, and republishes to S3.
- **Monitoring and API outages**: sampled pairs where either side never got a model response are excluded from the drift metrics, so an outage cannot trigger a rollback of a healthy prompt.

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env     # only needed for live runs
```

Run the gate locally against a base registry (needs `ANTHROPIC_API_KEY`):

```bash
git show origin/main:prompts/registry.yaml > base_registry.yaml
python -m src.ci_gate --base-registry base_registry.yaml
python -m src.ci_gate --base-registry base_registry.yaml --static-only   # no API calls
```

## Running Tests

```bash
python3 -m pytest -q
```

No API keys, AWS credentials or Snowflake account are needed.

## What Is Mocked / Not Tested Against Live Services

All tests run offline. Be aware of what that does and does not prove.

- **Anthropic**: replaced by a `FakeClient` that returns scripted JSON (an "oracle" derived from the golden files, plus deliberately broken variants). The tests prove the gate, contract, retry and metric logic. They do **not** prove that the shipped prompts actually score well on real Claude models. The golden datasets are small illustrative sets (10 / 6 / 4 cases), not production-grade.
- **S3**: an in-memory fake implementing `put_object` / `get_object`. Real bucket versioning, IAM, eventual-consistency and latency are untested.
- **CloudWatch / SNS**: `MagicMock` clients; tests assert the payload shape we send, not that AWS accepts it, nor that alarms are configured. No CloudWatch alarm or dashboard definitions (Terraform/CloudFormation) are included.
- **Snowflake**: connector is mocked (a stub module is injected if `snowflake-connector-python` is absent). SQL text is asserted but never executed, so syntax and MERGE semantics are untested. The dbt singular test is not run.
- **GitHub Actions**: the workflow YAML is parsed for validity but never executed. Required-check / branch-protection setup, the `production` environment approval, and the AWS OIDC role are assumed to be configured by you.
- **Airflow/MWAA**: no DAG is included; `pin()` is tested as the mechanism, not inside a scheduler.
- **Sonnet model id**: `claude-sonnet-5-5` is an assumption; set `ESCALATION_MODEL` to the id you use.
- **Rollback SLA**: the < 5 minute test measures only local pointer update plus a fake publish. Real end-to-end time depends on S3 and the next DAG run.
- **Not implemented**: DynamoDB/Firestore metadata store, GCP variants, the hourly production-sampling job itself (only its building blocks: sampling, metrics, alerts, publisher), and automatic reprocessing of old rows.
