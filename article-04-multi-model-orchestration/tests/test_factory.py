from src.cache import InMemoryCache
from src.factory import build_orchestrator
from tests.conftest import FakeAnthropic


def test_build_orchestrator_end_to_end_with_injected_fakes(config):
    client = FakeAnthropic()
    orch = build_orchestrator(client=client, cache_backend=InMemoryCache(), config=config)
    r = {"review_id": "r1", "text": "Lovely"}
    first = orch.run_chain(r)
    second = orch.run_chain({"review_id": "r2", "text": "Lovely"})  # duplicate content
    assert len(client.calls) == 1
    assert second.results["sentiment"].cache_hit and second.total_cost == 0
    assert first.total_cost > 0


def test_unreachable_redis_falls_back_to_in_memory(config, monkeypatch):
    from src import factory

    class DeadClient:
        def ping(self):
            raise ConnectionError("refused")

    monkeypatch.setattr(factory.RedisCache, "from_url",
                        classmethod(lambda cls, url: cls(DeadClient())))
    orch = build_orchestrator(client=FakeAnthropic(), config=config)
    assert isinstance(orch.gateway.cache.backend, InMemoryCache)
