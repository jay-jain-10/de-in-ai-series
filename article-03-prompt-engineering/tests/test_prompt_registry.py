import copy
import datetime as dt

import pytest
import yaml

from tests.conftest import FakeS3
from src.prompt_registry import (
    PromptRegistry, RegistryError, S3RegistryStore, check_schema_consistency,
    diff_registries, parse_version, release_tag,
)

NEW_SENTIMENT = {
    "version": "1.4",
    "model": "claude-haiku-4-5-20251001",
    "contract": "SentimentOutput",
    "system_prompt": "Be concise. Return JSON.",
    "validation_schema": {"type": "object", "properties": {}, "required": []},
}


def test_shipped_registry_is_valid(registry):
    assert registry.validate() == []
    assert registry.pin() == {
        "sentiment_classifier": "1.3", "escalation_classifier": "2.1", "vendor_extractor": "1.0",
    }


def test_uses_article1_model_id(registry):
    assert registry.get("sentiment_classifier").model == "claude-haiku-4-5-20251001"


def test_get_specific_version(registry):
    assert registry.get("sentiment_classifier", "1.2").version == "1.2"
    with pytest.raises(RegistryError):
        registry.get("sentiment_classifier", "9.9")
    with pytest.raises(RegistryError):
        registry.get("nope")


def test_version_ordering_is_numeric():
    assert parse_version("1.10") > parse_version("1.9")
    with pytest.raises(RegistryError):
        parse_version("abc")


def test_pin_is_unaffected_by_later_pointer_move(registry):
    pinned = registry.pin()
    registry.add_version("sentiment_classifier", {**NEW_SENTIMENT, "validation_schema": registry.get("sentiment_classifier").validation_schema}, activate=True)
    assert registry.active_version("sentiment_classifier") == "1.4"
    assert pinned["sentiment_classifier"] == "1.3"  # in-flight run keeps 1.3


def test_add_version_must_increase_and_be_new(registry):
    with pytest.raises(RegistryError):
        registry.add_version("sentiment_classifier", {**NEW_SENTIMENT, "version": "1.3"})
    with pytest.raises(RegistryError):
        registry.add_version("sentiment_classifier", {**NEW_SENTIMENT, "version": "1.1"})


def test_yaml_roundtrip(registry):
    again = PromptRegistry.from_yaml(registry.to_yaml())
    assert again.pin() == registry.pin()
    assert again.get("escalation_classifier").system_prompt == registry.get("escalation_classifier").system_prompt


def test_rollback_moves_pointer_and_marks_bad_version(registry):
    target = registry.rollback("sentiment_classifier", reason="test")
    assert target == "1.2"
    assert registry.active_version("sentiment_classifier") == "1.2"
    assert registry.get("sentiment_classifier", "1.3").status == "rolled_back"
    assert "rollback from 1.3" in registry.history("sentiment_classifier")[-1]["reason"]


def test_rollback_without_earlier_version_fails(registry):
    with pytest.raises(RegistryError):
        registry.rollback("vendor_extractor")


def test_rollback_skips_rolled_back_versions(registry):
    schema = registry.get("sentiment_classifier").validation_schema
    registry.add_version("sentiment_classifier", {**NEW_SENTIMENT, "validation_schema": schema}, activate=True)  # 1.4
    registry.rollback("sentiment_classifier")           # -> 1.3
    registry.rollback("sentiment_classifier")           # -> 1.2, never forward to 1.4
    assert registry.active_version("sentiment_classifier") == "1.2"


def test_validate_catches_schema_contract_mismatch(registry):
    registry._data["sentiment_classifier"]["versions"]["1.3"]["validation_schema"]["properties"].pop("confidence")
    problems = registry.validate()
    assert any("sentiment_classifier v1.3" in p for p in problems)


def test_validate_catches_unknown_contract_and_bad_pointer(registry):
    registry._data["vendor_extractor"]["versions"]["1.0"]["contract"] = "Ghost"
    registry._data["escalation_classifier"]["active_version"] = "9.9"
    problems = registry.validate()
    assert any("unknown contract" in p for p in problems)
    assert any("missing version" in p for p in problems)


def test_diff_flags_in_place_edit(registry):
    head = registry.copy()
    head._data["sentiment_classifier"]["versions"]["1.3"]["system_prompt"] = "Extract vendor info. Return JSON."
    d = diff_registries(registry, head)
    assert any("immutable" in v for v in d["violations"])


def test_diff_flags_deleted_version_and_pointer_change(registry):
    head = registry.copy()
    del head._data["sentiment_classifier"]["versions"]["1.2"]
    head._data["escalation_classifier"]["active_version"] = "2.0"
    d = diff_registries(registry, head)
    assert any("deleted" in v for v in d["violations"])
    assert ("escalation_classifier", "2.1", "2.0") in d["changed"]


def test_release_tag():
    assert release_tag("sentiment_classifier", "1.3") == "sentiment_classifier-v1.3"


def test_s3_publish_and_runtime_load(registry):
    s3 = FakeS3()
    store = S3RegistryStore("bucket", client=s3)
    assert store.publish(registry) == "v1"
    loaded = store.load()
    assert loaded.pin() == registry.pin()
    # rollback = pointer update + republish; next runtime load sees it
    registry.rollback("escalation_classifier")
    store.publish(registry)
    assert store.load().active_version("escalation_classifier") == "2.0"


def test_diff_flags_deleted_prompt(registry):
    head = registry.copy()
    del head._data["vendor_extractor"]
    d = diff_registries(registry, head)
    assert any("vendor_extractor was deleted" in v for v in d["violations"])


def test_diff_flags_in_place_schema_or_max_tokens_edit(registry):
    head = registry.copy()
    head._data["vendor_extractor"]["versions"]["1.0"]["max_tokens"] = 20
    assert any("vendor_extractor v1.0" in v for v in diff_registries(registry, head)["violations"])
    head = registry.copy()
    head._data["vendor_extractor"]["versions"]["1.0"]["validation_schema"]["properties"]["currency"] = {"type": "integer"}
    assert any("vendor_extractor v1.0" in v for v in diff_registries(registry, head)["violations"])


def test_validate_reports_unquoted_float_versions_instead_of_crashing():
    text = open(__import__("tests.conftest", fromlist=["ROOT"]).ROOT + "/prompts/registry.yaml").read()
    text = text.replace("'1.2'", "1.2").replace("'1.3'", "1.3")
    problems = PromptRegistry.from_yaml(text).validate()
    assert any("sentiment_classifier: version keys must be quoted" in p for p in problems)


def test_last_activated_at_handles_unquoted_yaml_timestamp(registry):
    text = registry.to_yaml().replace("'2026-09-01T09:00:00+00:00'", "2026-09-01 09:00:00")
    r = PromptRegistry.from_yaml(text)
    assert r.last_activated_at("sentiment_classifier") == dt.datetime(2026, 9, 1, 9, tzinfo=dt.timezone.utc)
