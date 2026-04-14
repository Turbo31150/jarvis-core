"""
JARVIS Core Module: Model Committee
Version: 1.1.0
Role: Multi-model consensus and voting for high-reliability decision making in JARVIS OMEGA.
"""

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("jarvis.consensus.committee")

class VotingStrategy(Enum):
    MAJORITY = "majority"
    WEIGHTED = "weighted"
    UNANIMOUS = "unanimous"
    BEST_SCORE = "best_score"
    RANKED_CHOICE = "ranked_choice"

@dataclass
class CommitteeMember:
    model_id: str
    weight: float = 1.0
    node: str = "local"
    specialties: List[str] = field(default_factory=list)

@dataclass
class MemberVote:
    member_id: str
    response: str
    confidence: float
    latency_ms: float
    timestamp: float = field(default_factory=time.time)

@dataclass
class CommitteeVerdict:
    question: str
    votes: List[MemberVote]
    consensus: str
    agreement_score: float
    strategy_used: VotingStrategy
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        data = asdict(self)
        data['strategy_used'] = self.strategy_used.value
        return json.dumps(data, indent=2)

class ModelCommittee:
    """
    Orchestrates multiple LLMs to reach a consensus on a given query.
    Uses various voting strategies to ensure reliability.
    """

    def __init__(self):
        self.members: Dict[str, CommitteeMember] = {}
        
    def add_member(self, member: CommitteeMember):
        """Adds a model to the committee."""
        self.members[member.model_id] = member
        logger.info(f"Added committee member: {member.model_id} (weight={member.weight})")

    async def _get_member_vote(self, member: CommitteeMember, question: str, llm_fn: Callable) -> MemberVote:
        """Helper to get a single vote from a member."""
        start_time = time.time()
        try:
            # We assume llm_fn takes (model_id, prompt)
            if asyncio.iscoroutinefunction(llm_fn):
                resp_data = await llm_fn(member.model_id, question)
            else:
                resp_data = llm_fn(member.model_id, question)
                
            # Expected format: {"text": "...", "confidence": 0.9}
            text = resp_data.get("text", str(resp_data))
            conf = resp_data.get("confidence", 0.5)
        except Exception as e:
            logger.error(f"Error getting vote from {member.model_id}: {e}")
            text = f"ERROR: {str(e)}"
            conf = 0.0
            
        latency = (time.time() - start_time) * 1000
        return MemberVote(member.model_id, text, conf, latency)

    async def query(self, question: str, strategy: VotingStrategy, llm_fn: Callable) -> CommitteeVerdict:
        """Queries the committee and applies the voting strategy."""
        if not self.members:
            raise ValueError("Committee has no members")
            
        logger.info(f"Querying committee with strategy: {strategy.name}")
        tasks = [self._get_member_vote(m, question, llm_fn) for m in self.members.values()]
        votes = await asyncio.gather(*tasks)
        
        consensus = ""
        agreement = 0.0
        
        if strategy == VotingStrategy.MAJORITY:
            consensus, agreement = self._resolve_majority(votes)
        elif strategy == VotingStrategy.WEIGHTED:
            consensus, agreement = self._resolve_weighted(votes)
        elif strategy == VotingStrategy.UNANIMOUS:
            consensus, agreement = self._resolve_unanimous(votes)
        elif strategy == VotingStrategy.BEST_SCORE:
            consensus, agreement = self._resolve_best_score(votes)
        else:
            consensus = votes[0].response # Fallback
            agreement = 1.0 / len(votes)
            
        return CommitteeVerdict(
            question=question,
            votes=votes,
            consensus=consensus,
            agreement_score=agreement,
            strategy_used=strategy
        )

    def _resolve_majority(self, votes: List[MemberVote]) -> Tuple[str, float]:
        """Simple count-based majority."""
        counts: Dict[str, int] = {}
        for v in votes:
            counts[v.response] = counts.get(v.response, 0) + 1
            
        winner = max(counts, key=counts.get)
        agreement = counts[winner] / len(votes)
        return winner, agreement

    def _resolve_weighted(self, votes: List[MemberVote]) -> Tuple[str, float]:
        """Weighted majority based on member weights and confidence."""
        scores: Dict[str, float] = {}
        total_weight = 0.0
        
        for v in votes:
            weight = self.members[v.member_id].weight * v.confidence
            scores[v.response] = scores.get(v.response, 0.0) + weight
            total_weight += weight
            
        winner = max(scores, key=scores.get)
        agreement = scores[winner] / total_weight if total_weight > 0 else 0.0
        return winner, agreement

    def _resolve_unanimous(self, votes: List[MemberVote]) -> Tuple[str, float]:
        """Requires all votes to be identical."""
        responses = set(v.response for v in votes)
        if len(responses) == 1:
            return votes[0].response, 1.0
        return "NO_CONSENSUS", 0.0

    def _resolve_best_score(self, votes: List[MemberVote]) -> Tuple[str, float]:
        """Selects the response with the highest confidence."""
        best = max(votes, key=lambda x: x.confidence)
        return best.response, best.confidence

def build_jarvis_model_committee() -> ModelCommittee:
    """Factory with default members."""
    committee = ModelCommittee()
    committee.add_member(CommitteeMember("qwen3.5-9b", weight=1.5, specialties=["code", "logic"]))
    committee.add_member(CommitteeMember("deepseek-r1", weight=1.2, specialties=["reasoning"]))
    committee.add_member(CommitteeMember("gemma3", weight=1.0, specialties=["general"]))
    return committee

# Mock multi-model LLM for demo
async def mock_cluster_llm(model_id: str, prompt: str) -> Dict[str, Any]:
    await asyncio.sleep(0.05)
    # Simulate slightly different opinions
    if "health" in prompt.lower():
        if model_id == "qwen3.5-9b":
            return {"text": "Cluster is HEALTHY", "confidence": 0.95}
        if model_id == "deepseek-r1":
            return {"text": "Cluster is HEALTHY", "confidence": 0.92}
        return {"text": "Cluster might be STABLE", "confidence": 0.7}
    return {"text": "I don't know", "confidence": 0.1}

async def demo():
    committee = build_jarvis_model_committee()
    question = "What is the current health status of the JARVIS cluster?"
    
    for strategy in [VotingStrategy.MAJORITY, VotingStrategy.WEIGHTED, VotingStrategy.BEST_SCORE]:
        print(f"\n--- TESTING STRATEGY: {strategy.name} ---")
        verdict = await committee.query(question, strategy, mock_cluster_llm)
        
        print(f"Consensus: {verdict.consensus}")
        print(f"Agreement Score: {verdict.agreement_score:.2%}")
        for vote in verdict.votes:
            print(f"  [{vote.member_id}] -> \"{vote.response}\" (conf: {vote.confidence})")

if __name__ == "__main__":
    asyncio.run(demo())
