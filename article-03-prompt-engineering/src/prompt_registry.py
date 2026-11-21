"""Versioned prompt registry (YAML in Git, S3 as the runtime read target).

Design (see Article 3, Stage 1):
  * Git is the source of truth: humans edit ``prompts/registry.yaml`` in a PR.
  * S3 is the deployment target: Airflow reads from there at runtime.
  * ``active_version`` is the pointer; rollback = move the pointer.
  * Published versions are immutable. Changing the text of an existing version
    is rejected; you must add a new, higher version.
"""

import copy
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import yaml

from .output_contracts import CONTRACTS, get_contract

logger = logging.getLogger(__name__)


class RegistryError(Exception):
    pass


def parse_version(v) -> tuple:
    """'1.3' -> (1, 3). Numeric compare so 1.10 > 1.9."""
    try:
        return tuple(int(p) for p in str(v).split("."))
    except ValueError:
        raise RegistryError(f"Invalid version string: {v!r}")


def release_tag(name: str, version: str) -> str:
    """Git tag convention applied on merge to main."""
    return f"{name}-v{version}"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str
    model: str
    system_prompt: str
    contract: str
    validation_schema: dict
    max_tokens: int = 300
    changelog: str = ""
    status: str = "active"


class PromptRegistry:
    """In-memory view of the registry YAML."""

    def __init__(self, data: Dict[str, dict]):
        self._data = copy.deepcopy(data)

    # ── construction / serialization ──────────────────────────────────
    @classmethod
    def from_yaml(cls, text: str) -> "PromptRegistry":
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise RegistryError("Registry root must be a mapping")
        return cls(data)

    @classmethod
    def from_file(cls, path: str) -> "PromptRegistry":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_yaml(f.read())

    def to_yaml(self) -> str:
        return yaml.safe_dump(self._data, sort_keys=False, default_flow_style=False)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_yaml())

    def copy(self) -> "PromptRegistry":
        return PromptRegistry(self._data)

    # ── queries ───────────────────────────────────────────────────────
    def names(self) -> List[str]:
        return list(self._data.keys())

    def _entry(self, name: str) -> dict:
        if name not in self._data:
            raise RegistryError(f"Unknown prompt: {name}")
        return self._data[name]

    def versions(self, name: str) -> List[str]:
        return sorted(self._entry(name).get("versions", {}).keys(), key=parse_version)

    def active_version(self, name: str) -> str:
        return str(self._entry(name)["active_version"])

    def history(self, name: str) -> List[dict]:
        return list(self._entry(name).get("history", []))

    def get(self, name: str, version: Optional[str] = None) -> PromptSpec:
        entry = self._entry(name)
        version = str(version) if version is not None else self.active_version(name)
        raw = entry.get("versions", {}).get(version)
        if raw is None:
            raise RegistryError(f"{name} has no version {version}")
        return PromptSpec(
            name=name,
            version=version,
            model=raw["model"],
            system_prompt=raw["system_prompt"],
            contract=raw["contract"],
            validation_schema=raw.get("validation_schema", {}),
            max_tokens=raw.get("max_tokens", 300),
            changelog=raw.get("changelog", ""),
            status=raw.get("status", "active"),
        )

    def pin(self) -> Dict[str, str]:
        """Snapshot of active versions taken at DAG-run start.

        A run that pinned ``sentiment_classifier=1.3`` keeps using 1.3 even if
        the registry moves to 1.4 mid-run (idempotent deployments).
        """
        return {n: self.active_version(n) for n in self.names()}

    # ── mutations ─────────────────────────────────────────────────────
    def add_version(self, name: str, spec: dict, activate: bool = False) -> str:
        """Add a new immutable version. Version must be strictly higher."""
        entry = self._entry(name)
        version = str(spec["version"])
        existing = entry["versions"]
        if version in existing:
            raise RegistryError(f"{name} v{version} already exists (versions are immutable)")
        if existing and parse_version(version) <= parse_version(self.versions(name)[-1]):
            raise RegistryError(f"{name} v{version} must be higher than {self.versions(name)[-1]}")
        body = {k: v for k, v in spec.items() if k != "version"}
        existing[version] = body
        if activate:
            self.set_active(name, version, reason="activated on add")
        return version

    def set_active(self, name: str, version: str, reason: str = "") -> None:
        entry = self._entry(name)
        version = str(version)
        if version not in entry["versions"]:
            raise RegistryError(f"{name} has no version {version}")
        entry["active_version"] = version
        entry.setdefault("history", []).append(
            {"version": version, "activated_at": _now(), "reason": reason}
        )

    def last_activated_at(self, name: str) -> Optional[dt.datetime]:
        hist = self.history(name)
        if not hist:
            return None
        ts = hist[-1]["activated_at"]
        # Hand-edited YAML may carry an unquoted timestamp, which safe_load already
        # turns into a datetime (often naive). Normalise to an aware UTC datetime so
        # window arithmetic never raises TypeError.
        if not isinstance(ts, dt.datetime):
            ts = dt.datetime.fromisoformat(str(ts))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return ts

    def rollback(self, name: str, reason: str = "") -> str:
        """Move the pointer to the newest earlier version not marked rolled_back.

        The bad version is flagged ``status: rolled_back`` so a second rollback
        never bounces forward to it.
        """
        entry = self._entry(name)
        current = self.active_version(name)
        candidates = [
            v
            for v in self.versions(name)
            if parse_version(v) < parse_version(current)
            and entry["versions"][v].get("status") != "rolled_back"
        ]
        if not candidates:
            raise RegistryError(f"{name}: no earlier version to roll back to from {current}")
        target = candidates[-1]
        entry["versions"][current]["status"] = "rolled_back"
        self.set_active(name, target, reason=f"rollback from {current}: {reason}")
        logger.warning("Rolled back %s: %s -> %s (%s)", name, current, target, reason)
        return target

    # ── validation ────────────────────────────────────────────────────
    def validate(self) -> List[str]:
        """Static checks run in CI. Returns a list of problems (empty == ok)."""
        problems: List[str] = []
        for name in self.names():
            entry = self._entry(name)
            versions = entry.get("versions", {})
            if not versions:
                problems.append(f"{name}: no versions defined")
                continue
            # Unquoted YAML versions become floats (1.10 -> 1.1!) and no longer
            # match the string lookups used everywhere else.
            bad_keys = [v for v in versions if not isinstance(v, str)]
            if bad_keys:
                problems.append(f"{name}: version keys must be quoted strings, got {bad_keys!r}")
                continue
            if not isinstance(entry.get("active_version"), str):
                problems.append(f"{name}: active_version must be a quoted string")
            if str(entry.get("active_version")) not in versions:
                problems.append(f"{name}: active_version points at a missing version")
            for v in versions:
                try:
                    parse_version(v)
                    spec = self.get(name, v)
                except KeyError as e:
                    problems.append(f"{name} v{v}: missing field {e}")
                    continue
                except RegistryError as e:
                    problems.append(f"{name} v{v}: {e}")
                    continue
                if spec.contract not in CONTRACTS:
                    problems.append(f"{name} v{v}: unknown contract {spec.contract}")
                    continue
                problems.extend(f"{name} v{v}: {p}" for p in check_schema_consistency(spec))
        return problems


def check_schema_consistency(spec: PromptSpec) -> List[str]:
    """YAML ``validation_schema`` must agree with the Pydantic contract."""
    problems = []
    model = get_contract(spec.contract)
    fields = set(model.model_fields.keys())
    props = set(spec.validation_schema.get("properties", {}).keys())
    required = set(spec.validation_schema.get("required", []))
    if props != fields:
        problems.append(f"schema properties {sorted(props)} != contract fields {sorted(fields)}")
    pyd_required = {n for n, f in model.model_fields.items() if f.is_required()}
    if required != pyd_required:
        problems.append(f"schema required {sorted(required)} != contract required {sorted(pyd_required)}")
    return problems


def diff_registries(base: PromptRegistry, head: PromptRegistry) -> dict:
    """What a PR changed: immutability violations and pointer moves."""
    violations: List[str] = []
    changed = []
    for name in base.names():
        if name not in head.names():
            violations.append(f"{name} was deleted; published prompts are immutable")
    for name in head.names():
        if name not in base.names():
            changed.append((name, None, head.active_version(name)))
            continue
        for v in base.versions(name):
            if v not in head.versions(name):
                violations.append(f"{name} v{v} was deleted")
                continue
            b, h = base.get(name, v), head.get(name, v)
            # Everything that changes what the model sees or how output is checked.
            if (b.system_prompt, b.model, b.contract, b.validation_schema, b.max_tokens) != (
                h.system_prompt, h.model, h.contract, h.validation_schema, h.max_tokens
            ):
                violations.append(
                    f"{name} v{v} was edited in place; published versions are immutable, add a new version"
                )
        if base.active_version(name) != head.active_version(name):
            changed.append((name, base.active_version(name), head.active_version(name)))
    return {"violations": violations, "changed": changed}


class S3RegistryStore:
    """Runtime read/write of the registry in a *versioned* S3 bucket."""

    def __init__(self, bucket: str, key: str = "prompts/registry.yaml", client=None, region: str = "us-east-1"):
        self.bucket = bucket
        self.key = key
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=region)
        self.client = client

    def publish(self, registry: PromptRegistry) -> Optional[str]:
        """Deploy step: upload registry; returns the S3 VersionId."""
        resp = self.client.put_object(
            Bucket=self.bucket,
            Key=self.key,
            Body=registry.to_yaml().encode("utf-8"),
            ContentType="application/x-yaml",
        )
        return resp.get("VersionId")

    def load(self) -> PromptRegistry:
        resp = self.client.get_object(Bucket=self.bucket, Key=self.key)
        return PromptRegistry.from_yaml(resp["Body"].read().decode("utf-8"))
