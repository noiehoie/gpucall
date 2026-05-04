from __future__ import annotations

from gpucall.domain import ProviderSpec
from gpucall.provider_catalog import live_provider_catalog_findings


def test_hyperstack_live_catalog_check_rejects_unknown_image(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeRequests:
        @staticmethod
        def get(url: str, **_kwargs):
            if url.endswith("/core/images"):
                return FakeResponse(
                    {
                        "images": [
                            {
                                "region_name": "CANADA-1",
                                "images": [{"name": "Ubuntu Server 22.04 LTS R570 CUDA 12.8 with Docker"}],
                            }
                        ]
                    }
                )
            if url.endswith("/core/flavors"):
                return FakeResponse(
                    {"data": [{"region_name": "CANADA-1", "flavors": [{"name": "n3-A100x1"}]}]}
                )
            if url.endswith("/core/environments"):
                return FakeResponse({"environments": [{"name": "default-CANADA-1"}]})
            raise AssertionError(url)

    monkeypatch.setitem(__import__("sys").modules, "requests", FakeRequests)
    providers = {
        "hyperstack-a100": ProviderSpec(
            name="hyperstack-a100",
            adapter="hyperstack",
            gpu="A100",
            vram_gb=80,
            max_model_len=32768,
            cost_per_second=0.001,
            target="default-CANADA-1",
            model="Qwen/Qwen2.5-1.5B-Instruct",
            instance="n3-A100x1",
            image="Ubuntu 22.04 LTS",
        )
    }

    findings = live_provider_catalog_findings(providers, {"hyperstack": {"api_key": "test"}})

    assert findings
    assert findings[0]["field"] == "image"
