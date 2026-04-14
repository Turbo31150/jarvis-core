import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class LoraStatus(Enum):
    UNLOADED = "UNLOADED"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"
    DEPRECATED = "DEPRECATED"


@dataclass
class LoraAdapter:
    adapter_id: str
    name: str
    base_model: str
    path: str
    rank: int
    alpha: float
    task_type: str
    status: LoraStatus
    metadata: dict


@dataclass
class LoraLoadResult:
    adapter_id: str
    success: bool
    latency_ms: float
    error: str = ""


class LoraManager:
    def __init__(self):
        self.adapters: List[LoraAdapter] = []
        self.active_adapter_id: Optional[str] = None
        self._load_state()

    async def register(self, adapter: LoraAdapter):
        if any(a.adapter_id == adapter.adapter_id for a in self.adapters):
            return  # already registered, skip silently
        self.adapters.append(adapter)
        self._save_state()

    async def load(self, adapter_id: str) -> LoraLoadResult:
        adapter = next((a for a in self.adapters if a.adapter_id == adapter_id), None)
        if not adapter:
            return LoraLoadResult(
                adapter_id=adapter_id,
                success=False,
                latency_ms=0.0,
                error="Adapter not found.",
            )

        if adapter.status == LoraStatus.LOADING:
            return LoraLoadResult(
                adapter_id=adapter_id,
                success=False,
                latency_ms=0.0,
                error="Adapter is already loading.",
            )

        adapter.status = LoraStatus.LOADING
        self._save_state()

        # Simulate loading delay
        await asyncio.sleep(1)

        if adapter.adapter_id == "error_adapter":
            adapter.status = LoraStatus.ERROR
            return LoraLoadResult(
                adapter_id=adapter_id,
                success=False,
                latency_ms=1000.0,
                error="Simulated load error.",
            )

        adapter.status = LoraStatus.ACTIVE
        self.active_adapter_id = adapter.adapter_id
        self._save_state()

        return LoraLoadResult(adapter_id=adapter_id, success=True, latency_ms=1000.0)

    async def unload(self, adapter_id: str):
        adapter = next((a for a in self.adapters if a.adapter_id == adapter_id), None)
        if not adapter:
            raise ValueError(f"Adapter with ID {adapter_id} not found.")

        if adapter.status != LoraStatus.ACTIVE:
            return

        adapter.status = LoraStatus.UNLOADED
        self.active_adapter_id = None
        self._save_state()

    async def switch(self, from_id: str, to_id: str):
        await self.unload(from_id)
        await self.load(to_id)

    def list_active(self) -> List[LoraAdapter]:
        return [a for a in self.adapters if a.status == LoraStatus.ACTIVE]

    def list_available(self) -> List[LoraAdapter]:
        return [
            a
            for a in self.adapters
            if a.status != LoraStatus.ERROR and a.status != LoraStatus.DEPRECATED
        ]

    def get_by_task(self, task_type: str) -> List[LoraAdapter]:
        return [a for a in self.adapters if a.task_type == task_type]

    def _save_state(self):
        def _serial(obj):
            if hasattr(obj, "name"):
                return obj.name
            return str(obj)

        with open("/tmp/jarvis_lora_state.json", "w") as f:
            json.dump(
                [
                    {
                        k: _serial(v) if hasattr(v, "name") else v
                        for k, v in a.__dict__.items()
                    }
                    for a in self.adapters
                ],
                f,
            )

    def _load_state(self):
        try:
            with open("/tmp/jarvis_lora_state.json", "r") as f:
                data = json.load(f)
                self.adapters = [LoraAdapter(**d) for d in data]
                self.active_adapter_id = next(
                    (
                        a.adapter_id
                        for a in self.adapters
                        if a.status == LoraStatus.ACTIVE
                    ),
                    None,
                )
        except FileNotFoundError:
            pass

    def stats(self) -> dict:
        return {
            "total_adapters": len(self.adapters),
            "active_adapters": len(
                [a for a in self.adapters if a.status == LoraStatus.ACTIVE]
            ),
            "available_adapters": len(
                [
                    a
                    for a in self.adapters
                    if a.status != LoraStatus.ERROR
                    and a.status != LoraStatus.DEPRECATED
                ]
            ),
        }


def build_jarvis_lora_manager() -> LoraManager:
    manager = LoraManager()

    adapter1 = LoraAdapter(
        adapter_id="adapter1",
        name="Adapter 1",
        base_model="base_model_1",
        path="/path/to/adapter1",
        rank=8,
        alpha=0.5,
        task_type="classification",
        status=LoraStatus.UNLOADED,
        metadata={"description": "First adapter"},
    )

    adapter2 = LoraAdapter(
        adapter_id="adapter2",
        name="Adapter 2",
        base_model="base_model_2",
        path="/path/to/adapter2",
        rank=16,
        alpha=0.75,
        task_type="regression",
        status=LoraStatus.UNLOADED,
        metadata={"description": "Second adapter"},
    )

    adapter3 = LoraAdapter(
        adapter_id="error_adapter",
        name="Error Adapter",
        base_model="base_model_3",
        path="/path/to/error_adapter",
        rank=8,
        alpha=0.5,
        task_type="classification",
        status=LoraStatus.UNLOADED,
        metadata={"description": "Adapter that simulates an error"},
    )

    asyncio.run(manager.register(adapter1))
    asyncio.run(manager.register(adapter2))
    asyncio.run(manager.register(adapter3))

    return manager


if __name__ == "__main__":
    manager = build_jarvis_lora_manager()

    print("Available adapters:")
    for adapter in manager.list_available():
        print(f" - {adapter.name} (ID: {adapter.adapter_id})")

    print("\nLoading Adapter 1...")
    result = asyncio.run(manager.load("adapter1"))
    print(
        f"Load result: Success={result.success}, Latency={result.latency_ms}ms, Error='{result.error}'"
    )

    print("\nActive adapters:")
    for adapter in manager.list_active():
        print(f" - {adapter.name} (ID: {adapter.adapter_id})")

    print("\nSwitching to Adapter 2...")
    asyncio.run(manager.switch("adapter1", "adapter2"))

    print("\nActive adapters after switch:")
    for adapter in manager.list_active():
        print(f" - {adapter.name} (ID: {adapter.adapter_id})")

    print("\nUnloading Adapter 2...")
    asyncio.run(manager.unload("adapter2"))

    print("\nActive adapters after unload:")
    for adapter in manager.list_active():
        print(f" - {adapter.name} (ID: {adapter.adapter_id})")

    print("\nManager stats:")
    print(json.dumps(manager.stats(), indent=4))
