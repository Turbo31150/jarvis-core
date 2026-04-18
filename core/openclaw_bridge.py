"""
JARVIS-OpenClaw Bridge — Interface logic for OpenClaw integration.
Handles CLI calls, gateway status, and agent dispatching.
"""

import logging
import subprocess
import json
import time
from typing import Dict, Any, Optional, List

logger = logging.getLogger("jarvis.openclaw")

class OpenClawBridge:
    """Bridge for interacting with OpenClaw system."""

    def __init__(self, gateway_url: str = "http://127.0.0.1:18789"):
        self.gateway_url = gateway_url
        self.cli_path = "openclaw"

    def is_healthy(self) -> bool:
        """Check if OpenClaw Gateway is responsive."""
        try:
            # Check gateway via CLI
            proc = subprocess.run(
                [self.cli_path, "health"],
                capture_output=True,
                text=True,
                timeout=5
            )
            return "✅" in proc.stdout or "OK" in proc.stdout.upper()
        except Exception as e:
            logger.error(f"OpenClaw health check failed: {e}")
            return False

    def run_agent(self, message: str, agent_id: str = "openclaw-master", sandbox: Optional[str] = None, timeout: int = 60) -> Dict[str, Any]:
        """Run an agent turn through OpenClaw."""
        start_time = time.time()
        try:
            cmd = [
                self.cli_path, "agent",
                "--agent", agent_id,
                "--message", message,
                "--log-level", "error"
            ]
            
            if sandbox:
                cmd.extend(["--sandbox", sandbox])
            
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            duration = time.time() - start_time
            
            if proc.returncode == 0:
                output = proc.stdout.strip()
                # OpenClaw sometimes outputs additional info, try to find the actual response
                return {
                    "success": True,
                    "output": output,
                    "agent": agent_id,
                    "duration": duration
                }
            else:
                return {
                    "success": False,
                    "error": proc.stderr.strip() or proc.stdout.strip() or "Unknown error",
                    "agent": agent_id,
                    "duration": duration
                }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"OpenClaw agent {agent_id} timed out after {timeout}s",
                "agent": agent_id,
                "duration": time.time() - start_time
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "agent": agent_id,
                "duration": time.time() - start_time
            }

    def list_agents(self) -> List[Dict[str, Any]]:
        """List registered OpenClaw agents."""
        try:
            proc = subprocess.run(
                [self.cli_path, "agents", "list"],
                capture_output=True,
                text=True,
                timeout=10
            )
            # This is usually a text table, parsing might be tricky but we can return it
            return [{"raw": proc.stdout.strip()}]
        except Exception:
            return []

    def search_memory(self, query: str) -> Optional[str]:
        """Search OpenClaw memory."""
        try:
            proc = subprocess.run(
                [self.cli_path, "memory", "search", "--query", query],
                capture_output=True,
                text=True,
                timeout=10
            )
            if proc.returncode == 0:
                return proc.stdout.strip()
        except Exception:
            pass
        return None

def build_openclaw_bridge() -> OpenClawBridge:
    return OpenClawBridge()
