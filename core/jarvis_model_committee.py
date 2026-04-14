import asyncio
import random
from dataclasses import dataclass
from enum import Enum

class VotingStrategy(Enum):
    MAJORITY = "MAJORITY"
    WEIGHTED = "WEIGHTED"
    BEST_SCORE = "BEST_SCORE"

@dataclass
class CommitteeMember:
    model_id: str
    weight: float
    node: str
    specialties: list

@dataclass
class MemberVote:
    member_id: str
    response: str
    confidence: float
    latency_ms: float

@dataclass
class CommitteeVerdict:
    question: str
    votes: list[MemberVote]
    consensus: str
    agreement_score: float
    strategy_used: VotingStrategy

class ModelCommittee:
    def __init__(self):
        self.members = []

    def add_member(self, member: CommitteeMember):
        self.members.append(member)

    async def query(self, question: str, strategy: VotingStrategy) -> CommitteeVerdict:
        votes = await asyncio.gather(*(self._call_member(m, question) for m in self.members))
        
        if strategy == VotingStrategy.MAJORITY:
            consensus = self._aggregate_majority(votes)
        elif strategy == VotingStrategy.WEIGHTED:
            consensus = self._aggregate_weighted(votes)
        elif strategy == VotingStrategy.BEST_SCORE:
            consensus = self._aggregate_best_score(votes)
        else:
            raise ValueError("Invalid voting strategy")

        agreement_score = self._compute_agreement(votes)

        return CommitteeVerdict(
            question=question,
            votes=votes,
            consensus=consensus,
            agreement_score=agreement_score,
            strategy_used=strategy
        )

    async def _call_member(self, member: CommitteeMember, question: str) -> MemberVote:
        # Simulated response generation
        response = f"Response from {member.model_id}"
        confidence = random.uniform(0.5, 1.0)
        latency_ms = random.randint(10, 200)

        return MemberVote(
            member_id=member.model_id,
            response=response,
            confidence=confidence,
            latency_ms=latency_ms
        )

    def _aggregate_majority(self, votes: list[MemberVote]) -> str:
        vote_counts = {}
        for vote in votes:
            if vote.response in vote_counts:
                vote_counts[vote.response] += 1
            else:
                vote_counts[vote.response] = 1

        return max(vote_counts, key=vote_counts.get)

    def _aggregate_weighted(self, votes: list[MemberVote]) -> str:
        weighted_votes = {}
        for vote in votes:
            if vote.response in weighted_votes:
                weighted_votes[vote.response] += vote.confidence * self.members[self._get_member_index(vote.member_id)].weight
            else:
                weighted_votes[vote.response] = vote.confidence * self.members[self._get_member_index(vote.member_id)].weight

        return max(weighted_votes, key=weighted_votes.get)

    def _aggregate_best_score(self, votes: list[MemberVote]) -> str:
        best_vote = max(votes, key=lambda v: v.confidence)
        return best_vote.response

    def _compute_agreement(self, votes: list[MemberVote]) -> float:
        if not votes:
            return 0.0

        response_set = set(vote.response for vote in votes)
        if len(response_set) == 1:
            return 1.0

        max_count = max(len(list(filter(lambda v: v.response == r, votes))) for r in response_set)
        agreement_score = max_count / len(votes)

        return agreement_score

    def _get_member_index(self, member_id: str) -> int:
        for i, member in enumerate(self.members):
            if member.model_id == member_id:
                return i
        raise ValueError("Member not found")

def build_jarvis_model_committee():
    committee = ModelCommittee()
    committee.add_member(CommitteeMember(model_id="model1", weight=0.3, node="node1", specialties=["science"]))
    committee.add_member(CommitteeMember(model_id="model2", weight=0.4, node="node2", specialties=["technology"]))
    committee.add_member(CommitteeMember(model_id="model3", weight=0.3, node="node3", specialties=["art"]))
    return committee

async def main():
    committee = build_jarvis_model_committee()
    question = "What is the capital of France?"
    strategy = VotingStrategy.MAJORITY
    verdict = await committee.query(question, strategy)
    print(f"Question: {verdict.question}")
    print(f"Consensus: {verdict.consensus}")
    print(f"Agreement Score: {verdict.agreement_score:.2f}")
    print(f"Strategy Used: {verdict.strategy_used.name}")

if __name__ == "__main__":
    asyncio.run(main())