"""
ARCHITECTURE_SUMMARY.md - LLM Orchestrator Router Technical Overview
"""

# LLM Orchestrator Router - Architecture Summary

## System Design

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ORCHESTRATION LAYER                           │
│  LLMOrchestrator (Coordinator & State Manager)                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌─────────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ IntentRouter    │  │ ClaudeClient │  │ DecisionEngine       │  │
│  │ • Intent detect │  │ • API calls  │  │ • Rule evaluation    │  │
│  │ • Confidence    │  │ • Response   │  │ • Circuit breaker    │  │
│  │ • Suggestions   │  │   parsing    │  │ • Escalation logic   │  │
│  └────────┬────────┘  └──────┬───────┘  └──────┬────────────────┘  │
│           │                   │                   │                  │
│           └───────────────────┼───────────────────┘                  │
│                               │                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │        ContextManager (Session & State Tracking)            │   │
│  │ • Session isolation                                         │   │
│  │ • History management                                        │   │
│  │ • State injection for LLM                                   │   │
│  │ • Workflow coordination                                     │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │        FeedbackLoop (Learning & Adaptation)                 │   │
│  │ • Result recording                                          │   │
│  │ • Pattern recognition                                       │   │
│  │ • Threshold adaptation                                      │   │
│  │ • Improvement suggestions                                   │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

## Data Flow

### Scenario: Email from Prospect

```
┌──────────────┐
│ Email Input  │
└──────┬───────┘
       │
       ▼
┌──────────────────────────────────────────┐
│ IntentRouter.route_task()                │
│ • Pattern matching: keywords, source     │
│ • Analysis: content, state               │
│ • Decision: prospection, confidence=0.92 │
└──────┬───────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────┐
│ ClaudeClient.suggest_parameters()        │
│ • Enhance analysis with Claude           │
│ • Generate execution parameters          │
│ • Confidence validation                  │
└──────┬───────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────┐
│ DecisionEngine.make_decision()           │
│ • Evaluate matching rules                │
│ • Check escalation threshold             │
│ • Resolve conflicts                      │
│ • Decision: send_auto_reply (auto-exec)  │
└──────┬───────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────┐
│ Executor Function                        │
│ • Send email via mail service            │
│ • Log to CRM                             │
│ • Schedule follow-up                     │
│ • Return result + metrics                │
└──────┬───────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────┐
│ FeedbackLoop.record_result()             │
│ • Track success (true)                   │
│ • Record parameters used                 │
│ • Extract patterns                       │
│ • Update thresholds                      │
└──────┬───────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────┐
│ Final Result                             │
│ • Status: completed                      │
│ • Decision: prospection → auto reply     │
│ • Confidence: 0.92                       │
│ • Learning: pattern recorded             │
└──────────────────────────────────────────┘
```

## Component Interactions

### 1. Intent Detection Pipeline

```python
# IntentRouter performs multi-stage detection:

Stage 1: Pattern Matching (Fast)
  - Regex against keywords
  - Source-based routing
  - State-based indicators
  → Confidence: 0.4-0.7

Stage 2: Content Analysis (Medium)
  - Error type detection
  - Metric change analysis
  - CRM context checking
  → Confidence: 0.5-0.8

Stage 3: Claude Enhancement (Slower but more accurate)
  - NLP-based intent analysis
  - Context understanding
  - Complex reasoning
  → Confidence: 0.7-0.95

Final: Combine scores, select best match
```

### 2. Decision Making Flow

```
Input: intent, confidence, options, state

                    ↓
    ┌───────────────────────────────┐
    │ Check Circuit Breaker         │
    │ (Is context in failure state?)│
    └───────┬───────────────────────┘
            │
         Yes│ Open → Escalate
            │
            ▼
    ┌───────────────────────────────┐
    │ Check Confidence Threshold    │
    │ (> 0.6 ?)                     │
    └───────┬───────────────────────┘
            │
         No │ Escalate
            │
            ▼
    ┌───────────────────────────────┐
    │ Evaluate Category Rules       │
    │ Match rules for intent        │
    └───────┬───────────────────────┘
            │
    ┌───────▼──────────┬──────────────┐
    │ Any matches?     │ Yes: Sort by  │
    │                  │ confidence    │
    │ No: Use options  │               │
    └────────┬─────────┴───────┬───────┘
             │                 │
             ▼                 ▼
    ┌────────────────┐ ┌──────────────────┐
    │ Return action  │ │ Auto-correct?    │
    │ + alternatives │ │ (conf > 0.95)    │
    └────────────────┘ │                  │
                       └──────┬───┬───────┘
                            Yes│ No│
                              │  │
                      Return  │  ▼
                      action  │ Return action
                      + ask   │ + ask user
```

### 3. Learning Loop

```
Execution Results
        │
        ▼
┌─────────────────────┐
│ FeedbackLoop        │
│ record_result()     │
└────────┬────────────┘
         │
         ▼
┌─────────────────────────────────┐
│ Analysis Methods:               │
│ 1. get_stats() → Success rates  │
│ 2. identify_patterns() → Rules  │
│ 3. get_improvement_suggestions()│
│ 4. get_adapted_threshold()      │
└────────┬────────────────────────┘
         │
    ┌────┴────┬────────┬──────────┐
    │          │        │          │
    ▼          ▼        ▼          ▼
Success    Patterns  Changes    New Rules
Metrics    Found     Applied    Suggested
```

## Data Structures

### Core Objects

```python
# Task Context (Input)
task_context = {
    "source": "email|analytics|scheduler|...",
    "sender": str,
    "subject": str,
    "content": str,
    "system_state": dict,  # Current metrics
    "metadata": dict       # Additional data
}

# Routing Decision (Output from IntentRouter)
RoutingDecision(
    task_type: TaskType,           # Enum: PROSPECTION, OPTIMIZATION, etc.
    confidence: float,             # 0.0-1.0
    suggested_actions: List[str],  # ["send_email", "add_crm", ...]
    reasoning: str,                # Why this classification
    parameters: Dict[str, Any]     # Suggested execution params
)

# Decision (Output from DecisionEngine)
Decision = {
    "action": str,                 # What to do
    "confidence": float,           # Confidence in decision
    "escalate": bool,              # Need human review?
    "reason": str,                 # Explanation
    "rule_matched": str,           # Which rule matched (if any)
    "alternatives": List[str]      # Other options
}

# Execution Result (Feedback to system)
ExecutionResult(
    task_id: str,
    task_type: str,
    success: bool,
    duration: float,               # Execution time in seconds
    parameters_used: Dict,
    error: Optional[str],
    output: Optional[Dict],
    timestamp: datetime
)

# Session State
SessionState(
    id: str,                       # UUID
    user_id: str,
    created_at: datetime,
    last_activity: datetime,
    history: List[Dict],           # Event history
    metadata: Dict
)
```

## Rule System

### Rule Types

```python
class RuleType(Enum):
    CONDITION_BASED = "IF condition THEN action"
    THRESHOLD_BASED = "IF metric > threshold THEN action"
    TIME_BASED = "IF time condition THEN action"
    STATE_BASED = "IF state matches THEN action"
    PATTERN_BASED = "IF pattern detected THEN action"
    HYBRID = "Multiple conditions"

# Example Rules
Rule(
    name="high_engagement_drop",
    condition=lambda s: s.get("engagement_drop_pct", 0) > 30,
    action="increase_posting_frequency",
    threshold=0.85,  # Confidence required
    priority=1,      # 1=highest
    rule_type=RuleType.THRESHOLD_BASED
)
```

### Rule Matching Logic

```
1. Get rules for detected intent
2. Evaluate all rules against current state
3. Filter: only matching rules
4. Sort: by confidence threshold (highest first)
5. Select: top matching rule
6. Decision:
   - confidence >= 0.95 → Auto-correct (execute)
   - 0.6-0.95 → Propose to user (+ alternatives)
   - < 0.6 → Escalate to human
```

## Learning System

### Pattern Recognition

```python
# Three main pattern types identified:

1. Parameter Combinations
   "When template=premium AND time=morning → 90% success"
   
2. Temporal Patterns
   "Hour 9-11: 85% success vs hour 17-19: 60% success"
   
3. Correlations
   "Execution duration 1.5-2.0s correlates with success"
```

### Threshold Adaptation

```python
# Adaptive thresholds learn from accuracy

Initial: threshold = 0.60 (default)

After tracking decisions:
- Low-confidence (< 0.6) decisions: 80% correct
  → Lower threshold to 0.40 (catch more good decisions)
  
- Low-confidence decisions: 40% correct
  → Raise threshold to 0.75 (reduce false positives)
```

### Pattern Example: Email Tasks

```
Sample size: 50 email executions

Success rate: 78%

Patterns found:
1. Parameter: template=default: 65%, template=personalized: 92%
2. Temporal: hour 9-12: 88%, hour 17-18: 62%
3. Duration: 1.0-1.5s success rate 85%, 3.0+ seconds: 40%

Suggestions:
- Always use "personalized" template (+27%)
- Schedule execution 9-12 UTC (+26%)
- Target 1.0-1.5s execution window (+45%)
```

## Performance Characteristics

### Time Complexity

```
IntentRouter.route_task():
  Pattern matching: O(n) where n = keywords
  → Typical: 1-10ms

ClaudeClient.analyze_intent():
  API call + parsing: O(1) amortized
  → Typical: 500ms-2s (network bound)

DecisionEngine.make_decision():
  Rule evaluation: O(m) where m = matching rules
  → Typical: 0.1-1ms

FeedbackLoop.identify_patterns():
  Statistical analysis: O(n log n) where n = samples
  → Typical: 50-500ms for 100 samples
```

### Space Complexity

```
ContextManager:
  Sessions: O(s) where s = active sessions
  History per session: O(h) where h = max_history_size
  → Typical: <1MB per 100 sessions

FeedbackLoop:
  Results storage: O(r) where r = recorded results
  Pattern cache: O(p) where p = patterns
  → Typical: 50KB per 1000 results

DecisionEngine:
  Rules: O(r) where r = total rules
  Decision history: O(d) where d = decisions
  → Typical: <100KB

Total for 1000 sessions + 100K results: ~50-100MB
```

## Integration Points

### External Systems

```
Claude API
  ↓ requests.post()
  ↑ JSON response
  
Email Service
  ↓ send_email()
  ↑ message_id
  
CRM System
  ↓ add_contact()
  ↑ contact_id
  
Analytics Platform
  ↓ get_metrics()
  ↑ metric_values
  
Task Queue
  ↓ enqueue_task()
  ↑ task_id
```

### Webhook Integration

```python
# Receive events from external systems
POST /orchestrator/webhook
{
    "source": "analytics",
    "event": "metric_threshold_exceeded",
    "data": {...}
}

# Process through orchestrator
task_context = parse_webhook(event)
result = orchestrator.route_and_execute(task_context)

# Send result back
POST callback_url with result
```

## Error Handling

### Error Types & Handling

```
1. API Errors (Claude)
   - Timeout → Retry with backoff
   - Rate limit → Queue and delay
   - Auth error → Escalate to human
   
2. Execution Errors
   - Task executor throws → Diagnose + retry
   - Timeout → Escalate with backoff
   - Invalid parameters → Adjust and retry
   
3. Logic Errors
   - Malformed response → Log + fallback
   - Missing context → Use defaults
   - Rule conflict → Use highest confidence
   
4. System Errors
   - Circuit breaker open → Escalate
   - Memory limit → Cleanup old sessions
   - Concurrency issues → Use locks
```

## Scaling Considerations

### Horizontal Scaling

```
- Stateless: Each orchestrator instance is independent
- Shared storage: Sessions/results in database
- Load balancing: Distribute requests across instances
- Cache: Redis for session state
```

### Vertical Scaling

```
- Process pool: Multiple orchestrator instances
- Async I/O: For Claude API calls
- Batch processing: Group similar decisions
- In-memory cache: Fast pattern lookup
```

## Security

- **API Keys**: Environment variables, never in code
- **Session Isolation**: Each session has own context
- **Rate Limiting**: Built-in Claude API rate limit handling
- **Error Messages**: No sensitive data in logs
- **Input Validation**: Sanitize task context before processing

## Monitoring & Observability

```python
# Get full system status
report = orchestrator.get_full_report()
{
    "sessions_active": 42,
    "total_decisions": 1024,
    "total_executions": 945,
    "circuit_breakers": {...},
    "learning_report": {...},
    "rule_suggestions": [...]
}

# Per-task metrics
stats = orchestrator.get_learning_stats("email")
{
    "count": 100,
    "success_rate": 0.78,
    "avg_duration": 1.2,
    "trend": "improving"
}
```

---

**Architecture Version**: 1.0 | **Status**: Production | **Last Updated**: 2024
