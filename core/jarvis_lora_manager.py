"""
JARVIS Core Module: LoRA Manager
Version: 1.1.0
Role: Dynamic lifecycle management of LoRA adapters for fine-tuned LLM inference.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("jarvis.inference.lora_manager")

class LoraStatus(Enum):
    UNLOADED = "unloaded"
    LOADING = "loading"
    ACTIVE = "active"
    ERROR = "error"
    DEPRECATED = "deprecated"

@dataclass
class LoraAdapter:
    adapter_id: str
    name: str
    base_model: str
    path: str
    rank: int = 16
    alpha: float = 32.0
    task_type: str = "general"
    status: LoraStatus = LoraStatus.UNLOADED
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class LoraLoadResult:
    adapter_id: str
    success: bool
    latency_ms: float
    error: Optional[str] = None

class LoraManager:
    """
    Manages registration, loading, and unloading of LoRA adapters.
    Maintains persistent state and performance statistics.
    """

    def __init__(self, state_path: str = "/tmp/jarvis_lora_state.json"):
        self.state_path = state_path
        self.adapters: Dict[str, LoraAdapter] = {}
        self.stats = {
            "total_loads": 0,
            "total_errors": 0,
            "avg_load_time_ms": 0.0
        }
        self._load_state()

    def _load_state(self):
        """Loads manager state from disk if exists."""
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, 'r') as f:
                    data = json.load(f)
                    # We only restore the stats and known adapters registry
                    # but we reset all statuses to UNLOADED on startup
                    self.stats = data.get("stats", self.stats)
                    for aid, a_data in data.get("adapters", {}).items():
                        # Enum restoration
                        a_data['status'] = LoraStatus.UNLOADED
                        self.adapters[aid] = LoraAdapter(**a_data)
            except Exception as e:
                logger.error(f"Failed to load LoRA state: {e}")

    def _save_state(self):
        """Saves current state to disk."""
        try:
            data = {
                "stats": self.stats,
                "adapters": {aid: asdict(a) for aid, a in self.adapters.items()}
            }
            # Handle Enums for JSON serialization
            for aid in data["adapters"]:
                data["adapters"][aid]["status"] = data["adapters"][aid]["status"].value
                
            with open(self.state_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save LoRA state: {e}")

    def register(self, adapter: LoraAdapter):
        """Registers a new adapter without loading it."""
        self.adapters[adapter.adapter_id] = adapter
        logger.info(f"Registered LoRA adapter: {adapter.adapter_id} for {adapter.base_model}")
        self._save_state()

    async def load(self, adapter_id: str) -> LoraLoadResult:
        """Simulates loading a LoRA adapter into VRAM."""
        if adapter_id not in self.adapters:
            return LoraLoadResult(adapter_id, False, 0, "Adapter not found")
            
        adapter = self.adapters[adapter_id]
        if adapter.status == LoraStatus.ACTIVE:
            return LoraLoadResult(adapter_id, True, 0)
            
        start_time = time.time()
        adapter.status = LoraStatus.LOADING
        logger.info(f"Loading LoRA {adapter_id}...")
        
        try:
            # Simulate I/O and GPU overhead
            await asyncio.sleep(0.5) 
            
            # Implementation would call LM Link or Ollama API here
            # mock failure case
            if "fail" in adapter_id:
                raise Exception("GPU Out of Memory or File not found")
                
            adapter.status = LoraStatus.ACTIVE
            success = True
            error = None
        except Exception as e:
            adapter.status = LoraStatus.ERROR
            success = False
            error = str(e)
            self.stats["total_errors"] += 1
            
        latency = (time.time() - start_time) * 1000
        
        # Update stats
        self.stats["total_loads"] += 1
        n = self.stats["total_loads"]
        self.stats["avg_load_time_ms"] = (self.stats["avg_load_time_ms"] * (n-1) + latency) / n
        
        self._save_state()
        return LoraLoadResult(adapter_id, success, latency, error)

    async def unload(self, adapter_id: str):
        """Unloads an adapter from memory."""
        if adapter_id in self.adapters:
            logger.info(f"Unloading LoRA {adapter_id}...")
            await asyncio.sleep(0.1)
            self.adapters[adapter_id].status = LoraStatus.UNLOADED
            self._save_state()

    async def switch(self, from_id: str, to_id: str) -> LoraLoadResult:
        """Unloads one adapter and loads another (atomic-like switch)."""
        await self.unload(from_id)
        return await self.load(to_id)

    def list_active(self) -> List[LoraAdapter]:
        """Returns list of currently active adapters."""
        return [a for a in self.adapters.values() if a.status == LoraStatus.ACTIVE]

    def list_available(self) -> List[LoraAdapter]:
        """Returns all registered adapters."""
        return list(self.adapters.values())

    def get_by_task(self, task_type: str) -> List[LoraAdapter]:
        """Filters adapters by task type."""
        return [a for a in self.adapters.values() if a.task_type == task_type]

def build_jarvis_lora_manager() -> LoraManager:
    """Factory with example adapters."""
    manager = LoraManager()
    manager.register(LoraAdapter(
        "lora-trading-v1", "Trading Expert", "qwen3.5-9b", 
        "/mnt/storage/loras/trading_v1", task_type="trading"
    ))
    manager.register(LoraAdapter(
        "lora-code-py-v2", "Python Specialist", "qwen3.5-9b", 
        "/mnt/storage/loras/python_v2", task_type="coding"
    ))
    manager.register(LoraAdapter(
        "lora-med-analysis", "Medical Triage", "gemma3", 
        "/mnt/storage/loras/med_v1", task_type="analysis"
    ))
    return manager

async def demo():
    manager = build_jarvis_lora_manager()
    
    print("\n--- INITIAL STATE ---")
    for a in manager.list_available():
        print(f"[{a.adapter_id}] Status: {a.status.name} | Task: {a.task_type}")
        
    print("\n--- LOADING ADAPTERS ---")
    res1 = await manager.load("lora-trading-v1")
    res2 = await manager.load("lora-code-py-v2")
    
    print(f"Load Trading: {'OK' if res1.success else 'FAIL'} ({res1.latency_ms:.1f}ms)")
    print(f"Load Code: {'OK' if res2.success else 'FAIL'} ({res2.latency_ms:.1f}ms)")
    
    print("\n--- SWITCHING ---")
    res3 = await manager.switch("lora-trading-v1", "lora-med-analysis")
    print(f"Switch Trading -> Med: {'OK' if res3.success else 'FAIL'}")
    
    print("\n--- ACTIVE ADAPTERS ---")
    for a in manager.list_active():
        print(f"ACTIVE: {a.adapter_id} on model {a.base_model}")
        
    print("\n--- MANAGER STATS ---")
    print(json.dumps(manager.stats, indent=2))

if __name__ == "__main__":
    asyncio.run(demo())
