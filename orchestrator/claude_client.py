"""
Claude Client - API wrapper for Claude-powered analysis
Handles all communication with Claude API for intent analysis, parameter generation,
error diagnosis, and optimization suggestions.
"""

import requests
import json
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from datetime import datetime
import os


logger = logging.getLogger(__name__)


@dataclass
class ClaudeResponse:
    """Structured response from Claude"""
    intent: Optional[str] = None
    confidence: float = 0.0
    reasoning: Optional[str] = None
    suggestions: List[str] = None
    parameters: Dict[str, Any] = None
    diagnosis: Optional[str] = None
    raw_text: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "suggestions": self.suggestions or [],
            "parameters": self.parameters or {},
            "diagnosis": self.diagnosis
        }


class ClaudeClient:
    """
    Claude API client for intelligent task analysis.
    
    Responsibilities:
    - Analyze task intent using Claude
    - Generate task parameters
    - Diagnose errors
    - Suggest optimizations
    - Parse structured responses
    
    Example:
        client = ClaudeClient(api_key="your-key")
        response = client.analyze_intent(task_context)
        params = client.suggest_parameters(task_type, context)
    """
    
    # Prompt templates
    INTENT_ANALYSIS_PROMPT = """Analyze the following task context and determine:
1. The primary intent/task type
2. Confidence score (0-1)
3. Suggested actions

Context:
{context}

Respond ONLY with valid JSON in this format:
{{
    "intent": "task_type",
    "confidence": 0.85,
    "reasoning": "Why this classification",
    "suggested_actions": ["action1", "action2"]
}}"""

    PARAMETER_GENERATION_PROMPT = """Generate optimal parameters for this task:
Task Type: {task_type}
Context: {context}

Respond ONLY with valid JSON parameters:
{{
    "template_id": "value",
    "priority": "high|medium|low",
    "timeout": 60,
    "retry_enabled": true,
    "other_params": "..."
}}"""

    ERROR_DIAGNOSIS_PROMPT = """Analyze this error and suggest fixes:
Error Type: {error_type}
Error Message: {error_message}
Failed Task: {task}
Context: {context}

Respond ONLY with valid JSON:
{{
    "diagnosis": "Root cause analysis",
    "confidence": 0.95,
    "suggestions": ["fix1", "fix2", "fix3"],
    "should_retry": true,
    "retry_strategy": "exponential_backoff"
}}"""

    OPTIMIZATION_PROMPT = """Suggest optimizations based on current performance:
Current Metrics: {metrics}
Recent Issues: {issues}
System State: {state}

Respond ONLY with valid JSON:
{{
    "opportunities": ["opportunity1", "opportunity2"],
    "recommended_actions": ["action1", "action2"],
    "expected_impact": "Increase engagement by 20-30%",
    "priority": "high"
}}"""
    
    def __init__(self, api_key: str = None, model: str = "claude-3-5-sonnet-20241022"):
        """
        Initialize Claude client.
        
        Args:
            api_key: Anthropic API key (or from env ANTHROPIC_API_KEY)
            model: Claude model to use
        """
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model
        self.base_url = "https://api.anthropic.com/v1"
        self.timeout = 30
        self.max_tokens = 1024
        
        if not self.api_key:
            logger.warning("No Anthropic API key found. Claude features disabled.")
    
    def analyze_intent(self, task_context: Dict[str, Any]) -> ClaudeResponse:
        """
        Analyze task intent using Claude.
        
        Args:
            task_context: Task metadata and context
            
        Returns:
            ClaudeResponse with intent analysis
        """
        if not self.api_key:
            logger.warning("Claude not configured")
            return ClaudeResponse(intent="unknown")
        
        prompt = self.INTENT_ANALYSIS_PROMPT.format(
            context=json.dumps(task_context, indent=2)
        )
        
        response_text = self._call_claude(prompt)
        
        try:
            data = json.loads(response_text)
            return ClaudeResponse(
                intent=data.get("intent"),
                confidence=data.get("confidence", 0.0),
                reasoning=data.get("reasoning"),
                suggestions=data.get("suggested_actions", []),
                raw_text=response_text
            )
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response: {e}")
            return ClaudeResponse(raw_text=response_text)
    
    def suggest_parameters(self, task_type: str, 
                         task_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate task parameters via Claude.
        
        Args:
            task_type: Type of task
            task_context: Task context
            
        Returns:
            Dict of suggested parameters
        """
        if not self.api_key:
            logger.warning("Claude not configured")
            return {}
        
        prompt = self.PARAMETER_GENERATION_PROMPT.format(
            task_type=task_type,
            context=json.dumps(task_context, indent=2)
        )
        
        response_text = self._call_claude(prompt)
        
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            logger.error("Failed to parse parameter suggestions")
            return {}
    
    def diagnose_error(self, error_type: str, error_message: str, 
                      failed_task: str = "", context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Analyze error and suggest fixes.
        
        Args:
            error_type: Type of error
            error_message: Error message
            failed_task: Task that failed
            context: Additional context
            
        Returns:
            Dict with diagnosis and suggestions
        """
        if not self.api_key:
            logger.warning("Claude not configured")
            return {"diagnosis": "Unknown", "suggestions": []}
        
        prompt = self.ERROR_DIAGNOSIS_PROMPT.format(
            error_type=error_type,
            error_message=error_message,
            task=failed_task,
            context=json.dumps(context or {}, indent=2)
        )
        
        response_text = self._call_claude(prompt)
        
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            logger.error("Failed to parse error diagnosis")
            return {
                "diagnosis": error_message,
                "suggestions": ["retry", "check_logs"]
            }
    
    def suggest_optimizations(self, metrics: Dict[str, Any], 
                            issues: List[str] = None,
                            state: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Suggest performance optimizations.
        
        Args:
            metrics: Current performance metrics
            issues: Known issues
            state: System state
            
        Returns:
            Dict with opportunities and actions
        """
        if not self.api_key:
            logger.warning("Claude not configured")
            return {"opportunities": []}
        
        prompt = self.OPTIMIZATION_PROMPT.format(
            metrics=json.dumps(metrics, indent=2),
            issues=json.dumps(issues or [], indent=2),
            state=json.dumps(state or {}, indent=2)
        )
        
        response_text = self._call_claude(prompt)
        
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            logger.error("Failed to parse optimization suggestions")
            return {"opportunities": [], "recommended_actions": []}
    
    def _call_claude(self, prompt: str, max_retries: int = 2) -> str:
        """
        Make API call to Claude.
        
        Args:
            prompt: Prompt text
            max_retries: Max retry attempts
            
        Returns:
            Claude response text
            
        Raises:
            Exception: If API call fails
        """
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "system": "You are an expert task router and decision engine. Respond with ONLY valid JSON, no markdown formatting or extra text."
        }
        
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    f"{self.base_url}/messages",
                    headers=headers,
                    json=payload,
                    timeout=self.timeout
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return data["content"][0]["text"]
                
                elif response.status_code == 429:  # Rate limited
                    logger.warning("Claude API rate limited")
                    raise Exception("Rate limited")
                
                elif response.status_code >= 500:  # Server error
                    if attempt < max_retries - 1:
                        logger.warning(f"Claude API error, retrying... ({attempt + 1}/{max_retries})")
                        continue
                    raise Exception(f"Claude API server error: {response.status_code}")
                
                else:
                    raise Exception(f"Claude API error: {response.status_code} - {response.text}")
            
            except requests.exceptions.Timeout:
                logger.error("Claude API request timeout")
                if attempt < max_retries - 1:
                    continue
                raise
            
            except requests.exceptions.RequestException as e:
                logger.error(f"Claude API request failed: {e}")
                if attempt < max_retries - 1:
                    continue
                raise
        
        raise Exception("Claude API call failed after retries")
    
    def batch_analyze(self, contexts: List[Dict[str, Any]]) -> List[ClaudeResponse]:
        """
        Analyze multiple contexts efficiently.
        
        Args:
            contexts: List of task contexts
            
        Returns:
            List of ClaudeResponse objects
        """
        results = []
        for context in contexts:
            try:
                result = self.analyze_intent(context)
                results.append(result)
            except Exception as e:
                logger.error(f"Failed to analyze context: {e}")
                results.append(ClaudeResponse())
        
        return results
    
    def validate_response_structure(self, data: Dict[str, Any], 
                                   expected_keys: List[str]) -> bool:
        """
        Validate Claude response has expected structure.
        
        Args:
            data: Response data
            expected_keys: Keys that should exist
            
        Returns:
            True if valid, False otherwise
        """
        if not isinstance(data, dict):
            return False
        
        return all(key in data for key in expected_keys)


class PromptBuilder:
    """Helper for building complex prompts"""
    
    @staticmethod
    def build_context_prompt(context: Dict[str, Any], 
                            instruction: str) -> str:
        """Build a contextual prompt"""
        parts = [instruction, "\n\nContext:"]
        
        for key, value in context.items():
            if isinstance(value, dict):
                parts.append(f"{key}: {json.dumps(value, indent=2)}")
            else:
                parts.append(f"{key}: {value}")
        
        return "\n".join(parts)
    
    @staticmethod
    def build_workflow_prompt(workflow_state: Dict[str, Any]) -> str:
        """Build prompt for workflow analysis"""
        return f"""Analyze the following workflow state and suggest next steps:

Workflow: {workflow_state.get('workflow_id')}
Type: {workflow_state.get('workflow_type')}
Current Step: {workflow_state.get('current_step')}
Completed: {workflow_state.get('completed_steps')}
Issues: {workflow_state.get('issues', [])}

Respond with JSON containing:
- next_steps: recommended next actions
- risks: potential issues
- optimizations: improvements
"""


__all__ = ["ClaudeClient", "ClaudeResponse", "PromptBuilder"]
