"""
JARVIS Core Module: Golden Dataset
Version: 1.1.0
Role: Management of high-quality reference datasets for LLM evaluation and regression testing.
"""

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("jarvis.quality.golden_dataset")

class Difficulty(Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"

@dataclass
class GoldenSample:
    sample_id: str
    input: str
    expected_output: str
    category: str
    difficulty: Difficulty = Difficulty.MEDIUM
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    validated: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class DatasetStats:
    n_samples: int
    by_category: Dict[str, int]
    by_difficulty: Dict[str, int]
    validated_pct: float

class GoldenDataset:
    """
    Manages a collection of validated reference samples for LLM benchmarking.
    Supports stratified sampling and dataset splits.
    """

    def __init__(self):
        self.samples: Dict[str, GoldenSample] = {}

    def add(self, sample: GoldenSample):
        """Adds a new sample to the dataset."""
        self.samples[sample.sample_id] = sample
        logger.debug(f"Added sample {sample.sample_id} to category {sample.category}")

    def get(self, sample_id: str) -> Optional[GoldenSample]:
        """Retrieves a sample by ID."""
        return self.samples.get(sample_id)

    def validate(self, sample_id: str):
        """Marks a sample as validated."""
        if sample_id in self.samples:
            self.samples[sample_id].validated = True

    def filter(self, category: Optional[str] = None, 
               difficulty: Optional[Difficulty] = None, 
               tags: Optional[List[str]] = None, 
               validated_only: bool = False) -> List[GoldenSample]:
        """Filters samples based on multiple criteria."""
        results = list(self.samples.values())
        
        if validated_only:
            results = [s for s in results if s.validated]
        if category:
            results = [s for s in results if s.category == category]
        if difficulty:
            results = [s for s in results if s.difficulty == difficulty]
        if tags:
            tag_set = set(tags)
            results = [s for s in results if tag_set.issubset(set(s.tags))]
            
        return results

    def sample(self, n: int, stratified: bool = True) -> List[GoldenSample]:
        """Returns a random sample of size n."""
        all_samples = list(self.samples.values())
        if not all_samples: return []
        
        if not stratified:
            return random.sample(all_samples, min(n, len(all_samples)))
            
        # Stratified sampling by category
        categories = {}
        for s in all_samples:
            if s.category not in categories:
                categories[s.category] = []
            categories[s.category].append(s)
            
        samples_per_cat = max(1, n // len(categories))
        result = []
        for cat_list in categories.values():
            result.extend(random.sample(cat_list, min(samples_per_cat, len(cat_list))))
            
        return result[:n]

    def stats(self) -> DatasetStats:
        """Computes summary statistics for the dataset."""
        n = len(self.samples)
        if n == 0:
            return DatasetStats(0, {}, {}, 0.0)
            
        by_cat = {}
        by_diff = {}
        validated = 0
        
        for s in self.samples.values():
            by_cat[s.category] = by_cat.get(s.category, 0) + 1
            by_diff[s.difficulty.name] = by_diff.get(s.difficulty.name, 0) + 1
            if s.validated:
                validated += 1
                
        return DatasetStats(
            n_samples=n,
            by_category=by_cat,
            by_difficulty=by_diff,
            validated_pct=(validated / n) * 100
        )

    def split(self, train_ratio: float = 0.8) -> Tuple[List[GoldenSample], List[GoldenSample]]:
        """Splits the dataset into training and test sets."""
        all_samples = list(self.samples.values())
        random.shuffle(all_samples)
        split_idx = int(len(all_samples) * train_ratio)
        return all_samples[:split_idx], all_samples[split_idx:]

    def export_json(self, path: str):
        """Exports dataset to a JSON file."""
        data = {sid: asdict(s) for sid, s in self.samples.items()}
        # Handle enums
        for sid in data:
            data[sid]["difficulty"] = data[sid]["difficulty"].value
            
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
        logger.info(f"Exported {len(self.samples)} samples to {path}")

    def import_json(self, path: str):
        """Imports dataset from a JSON file."""
        try:
            with open(path, 'r') as f:
                data = json.load(f)
                for sid, s_data in data.items():
                    s_data["difficulty"] = Difficulty(s_data["difficulty"])
                    self.samples[sid] = GoldenSample(**s_data)
            logger.info(f"Imported {len(self.samples)} samples from {path}")
        except Exception as e:
            logger.error(f"Failed to import dataset: {e}")

def build_jarvis_golden_dataset() -> GoldenDataset:
    """Factory function with 20 default samples for JARVIS OMEGA."""
    ds = GoldenDataset()
    
    # 5 Cluster Query Samples
    for i in range(5):
        ds.add(GoldenSample(
            f"CLUST-{i:03}", "What is the status of node M1?", "Node M1 is active and healthy.",
            "cluster_ops", Difficulty.EASY, ["monitoring", "status"]
        ))
        
    # 5 GPU Health Samples
    for i in range(5):
        ds.add(GoldenSample(
            f"GPU-{i:03}", "Check VRAM on GPU 0", "GPU 0 has 4GB used out of 12GB.",
            "hardware", Difficulty.MEDIUM, ["gpu", "thermal"]
        ))
        
    # 5 Model Selection Samples
    for i in range(5):
        ds.add(GoldenSample(
            f"MODEL-{i:03}", "Which model is best for coding?", "Qwen3.5-9b is recommended for coding tasks.",
            "orchestration", Difficulty.MEDIUM, ["selection", "llm"]
        ))
        
    # 5 Complex Logical Samples
    for i in range(5):
        ds.add(GoldenSample(
            f"LOGIC-{i:03}", "If node M2 fails, what is the failover path?", "Failover will route through M3 then OL1.",
            "resilience", Difficulty.HARD, ["failover", "logic"]
        ))
        
    # Validate some
    for sid in list(ds.samples.keys())[:10]:
        ds.validate(sid)
        
    return ds

def demo():
    ds = build_jarvis_golden_dataset()
    
    print("\n--- DATASET STATISTICS ---")
    st = ds.stats()
    print(f"Total Samples: {st.n_samples}")
    print(f"By Category: {st.by_category}")
    print(f"Validated: {st.validated_pct:.1f}%")
    
    print("\n--- FILTERING (Hardware, Validated) ---")
    hw_samples = ds.filter(category="hardware", validated_only=True)
    for s in hw_samples:
        print(f"[{s.sample_id}] {s.input[:30]}... -> {s.expected_output}")
        
    print("\n--- STRATIFIED SAMPLING (4 samples) ---")
    batch = ds.sample(4, stratified=True)
    for s in batch:
        print(f"[{s.category}] {s.sample_id}: {s.input[:40]}")
        
    print("\n--- EXPORTING DATASET ---")
    ds.export_json("/tmp/jarvis_golden_test.json")

if __name__ == "__main__":
    demo()
