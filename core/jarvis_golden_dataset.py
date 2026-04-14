from dataclasses import dataclass, field
from enum import Enum
import json
from random import sample as random_sample
from typing import List, Optional, Dict

class Difficulty(Enum):
    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"

@dataclass
class GoldenSample:
    sample_id: str
    input: str
    expected_output: str
    category: str
    difficulty: Difficulty
    tags: List[str]
    created_at: float
    validated: bool

@dataclass
class DatasetStats:
    n_samples: int
    by_category: Dict[str, int]
    by_difficulty: Dict[str, int]
    validated_pct: float

class GoldenDataset:
    def __init__(self):
        self.samples = {}

    def add(self, sample: GoldenSample) -> GoldenSample:
        self.samples[sample.sample_id] = sample
        return sample

    def get(self, sample_id: str) -> Optional[GoldenSample]:
        return self.samples.get(sample_id)

    def filter(
        self,
        category: Optional[str] = None,
        difficulty: Optional[Difficulty] = None,
        tags: Optional[List[str]] = None,
        validated_only: bool = False
    ) -> List[GoldenSample]:
        filtered_samples = list(self.samples.values())
        
        if category:
            filtered_samples = [s for s in filtered_samples if s.category == category]
        
        if difficulty:
            filtered_samples = [s for s in filtered_samples if s.difficulty == difficulty]
        
        if tags:
            filtered_samples = [s for s in filtered_samples if all(tag in s.tags for tag in tags)]
        
        if validated_only:
            filtered_samples = [s for s in filtered_samples if s.validated]
        
        return filtered_samples

    def sample(self, n: int, stratified: bool = False) -> List[GoldenSample]:
        if not stratified:
            return random_sample(list(self.samples.values()), min(n, len(self.samples)))
        
        categories = set(s.category for s in self.samples.values())
        sampled_samples = []
        
        for category in categories:
            category_samples = [s for s in self.samples.values() if s.category == category]
            n_to_sample = max(1, int(len(category_samples) * (n / len(self.samples))))
            sampled_samples.extend(random_sample(category_samples, min(n_to_sample, len(category_samples))))
        
        return random_sample(sampled_samples, min(n, len(sampled_samples)))

    def validate(self, sample_id: str):
        if sample_id in self.samples:
            self.samples[sample_id].validated = True

    def export_json(self, path: str):
        with open(path, 'w') as f:
            json.dump([sample.__dict__ for sample in self.samples.values()], f)

    def import_json(self, path: str):
        with open(path, 'r') as f:
            data = json.load(f)
            for item in data:
                sample = GoldenSample(**item)
                self.add(sample)

    def stats(self) -> DatasetStats:
        n_samples = len(self.samples)
        by_category = {}
        by_difficulty = {}
        
        for s in self.samples.values():
            if s.category not in by_category:
                by_category[s.category] = 0
            by_category[s.category] += 1
            
            if s.difficulty.value not in by_difficulty:
                by_difficulty[s.difficulty.value] = 0
            by_difficulty[s.difficulty.value] += 1
        
        validated_pct = sum(s.validated for s in self.samples.values()) / n_samples if n_samples > 0 else 0.0
        
        return DatasetStats(n_samples, by_category, by_difficulty, validated_pct)

    def split(self, train_ratio: float = 0.8) -> tuple:
        sample_ids = list(self.samples.keys())
        random_sample.shuffle(sample_ids)
        
        train_size = int(len(sample_ids) * train_ratio)
        train_set = GoldenDataset()
        test_set = GoldenDataset()
        
        for i, sample_id in enumerate(sample_ids):
            if i < train_size:
                train_set.add(self.get(sample_id))
            else:
                test_set.add(self.get(sample_id))
        
        return train_set, test_set

def build_jarvis_golden_dataset() -> GoldenDataset:
    dataset = GoldenDataset()
    
    samples = [
        GoldenSample(
            sample_id=f"sample_{i+1}",
            input=f"Query {i+1} about cluster queries GPU health model selection",
            expected_output="Expected output for query {i+1}",
            category="Cluster Queries",
            difficulty=Difficulty.EASY,
            tags=["GPU", "Health", "Model Selection"],
            created_at=i + 1.0,
            validated=False
        ) for i in range(15)
    ]
    
    for sample in samples:
        dataset.add(sample)
    
    return dataset

if __name__ == "__main__":
    # Main demo
    dataset = build_jarvis_golden_dataset()
    
    print("Dataset Stats:")
    print(dataset.stats())
    
    print("\nSampled 5 samples (stratified):")
    for s in dataset.sample(5, stratified=True):
        print(f"Sample ID: {s.sample_id}, Input: {s.input}")
    
    print("\nFiltered by category 'Cluster Queries' and difficulty 'EASY':")
    for s in dataset.filter(category="Cluster Queries", difficulty=Difficulty.EASY):
        print(f"Sample ID: {s.sample_id}, Input: {s.input}")
    
    # Validate a sample
    dataset.validate("sample_1")
    print("\nAfter validating sample_1:")
    print(dataset.stats())
    
    # Export and import JSON
    dataset.export_json("dataset.json")
    new_dataset = GoldenDataset()
    new_dataset.import_json("dataset.json")
    print("\nImported Dataset Stats:")
    print(new_dataset.stats())
    
    # Split dataset
    train_set, test_set = dataset.split(train_ratio=0.8)
    print(f"\nTrain Set Size: {len(train_set.samples)}, Test Set Size: {len(test_set.samples)}")