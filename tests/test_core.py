"""Tests for JARVIS core modules — tasks, services, security, network, memory."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tasks.models import TaskRequest, TaskResult, TaskStatus, TaskPriority
from core.services import ServiceRegistry, ServiceInfo
from core.security import check_action, ActionLevel, detect_secrets
from core.network.health import check_port


def test_task_request_creation():
    t = TaskRequest(prompt="hello", task_type="generic")
    assert t.prompt == "hello"
    assert t.task_type == "generic"
    assert t.priority == TaskPriority.MEDIUM
    assert len(t.id) == 8
    assert t.created_at > 0
    print("  ✅ test_task_request_creation")


def test_task_result_creation():
    r = TaskResult(request_id="abc123", status=TaskStatus.COMPLETED, output="done")
    assert r.request_id == "abc123"
    assert r.status == TaskStatus.COMPLETED
    assert r.output == "done"
    assert r.error is None
    assert r.completed_at > 0
    print("  ✅ test_task_result_creation")


def test_task_status_enum():
    assert TaskStatus.PENDING.value == "pending"
    assert TaskStatus.RUNNING.value == "running"
    assert TaskStatus.COMPLETED.value == "completed"
    assert TaskStatus.FAILED.value == "failed"
    assert TaskStatus.CANCELLED.value == "cancelled"
    assert TaskStatus.TIMEOUT.value == "timeout"
    assert len(TaskStatus) == 6
    print("  ✅ test_task_status_enum")


def test_service_registry_get():
    reg = ServiceRegistry()
    m1 = reg.get("M1")
    assert m1 is not None
    assert m1.host == "127.0.0.1"
    assert m1.port == 1234
    assert m1.type == "lmstudio"
    assert "gpu" in m1.tags
    assert reg.get("NONEXISTENT") is None
    print("  ✅ test_service_registry_get")


def test_service_registry_health():
    reg = ServiceRegistry()
    results = reg.health_check_all()
    assert isinstance(results, dict)
    assert "M1" in results
    assert "OL1" in results
    assert all(isinstance(v, bool) for v in results.values())
    print("  ✅ test_service_registry_health")


def test_service_registry_fallback():
    reg = ServiceRegistry()
    m1 = reg.get("M1")
    assert m1.fallback == "M3"
    fb = reg.fallback("M1")
    # fb may be None if M3 is offline — just verify type
    assert fb is None or isinstance(fb, ServiceInfo)
    # OL1 fallback chain
    ol1 = reg.get("OL1")
    assert ol1.fallback == "M1"
    print("  ✅ test_service_registry_fallback")


def test_action_policy_dangerous():
    pol = check_action("test_agent", "rm -rf /tmp/stuff")
    assert pol.level == ActionLevel.DANGEROUS
    assert pol.requires_confirmation is True
    pol2 = check_action("test_agent", "DROP TABLE users")
    assert pol2.level == ActionLevel.DANGEROUS
    print("  ✅ test_action_policy_dangerous")


def test_action_policy_allowed():
    pol = check_action("network_operator", "ping 127.0.0.1")
    assert pol.level == ActionLevel.READ_ONLY
    assert pol.requires_confirmation is False
    # Unknown agent, harmless command → CONFIRM_REQUIRED
    pol2 = check_action("unknown_agent", "ls -la")
    assert pol2.level == ActionLevel.CONFIRM_REQUIRED
    print("  ✅ test_action_policy_allowed")


def test_secret_detection():
    assert len(detect_secrets("no secrets here")) == 0
    assert len(detect_secrets("key=sk-abc12345678901234567890")) > 0
    assert len(detect_secrets("token=ghp_abcdefghijklmnopqrstuvwxyz1234567890")) > 0
    assert len(detect_secrets("AKIAIOSFODNN7EXAMPLE")) > 0
    assert len(detect_secrets("password = hunter2")) > 0
    print("  ✅ test_secret_detection")


def test_network_port_check():
    # Localhost port 1 should be closed
    assert check_port("127.0.0.1", 1, timeout=1) is False
    # check_port returns bool
    result = check_port("127.0.0.1", 22, timeout=1)
    assert isinstance(result, bool)
    print("  ✅ test_network_port_check")


def test_memory_facade_stats():
    from pathlib import Path
    db_path = Path.home() / "IA" / "Core" / "jarvis" / "data" / "jarvis-master.db"
    if not db_path.exists():
        print("  ⏭️  test_memory_facade_stats (DB not found, skipped)")
        return
    from core.memory.facade import MemoryFacade
    facade = MemoryFacade(read_only=True)
    stats = facade.get_stats()
    assert isinstance(stats, dict)
    assert "master" in stats
    facade.close_all()
    print("  ✅ test_memory_facade_stats")


def test_schema_inspector():
    import sqlite3, tempfile
    from core.memory.schema_inspector import SchemaInspector
    # Create a temp DB with a known schema
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp = f.name
    conn = sqlite3.connect(tmp)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, msg TEXT)")
    conn.close()
    schema = SchemaInspector.inspect(tmp)
    assert "users" in schema
    assert "logs" in schema
    assert any(c["name"] == "name" and c["notnull"] for c in schema["users"])
    # Diff test
    diff = SchemaInspector.diff(tmp, {"users": ["id", "name", "email"], "orders": ["id"]})
    assert "orders" in diff["missing_tables"]
    assert "email" in diff["missing_columns"].get("users", [])
    os.unlink(tmp)
    print("  ✅ test_schema_inspector")


ALL_TESTS = [
    test_task_request_creation,
    test_task_result_creation,
    test_task_status_enum,
    test_service_registry_get,
    test_service_registry_health,
    test_service_registry_fallback,
    test_action_policy_dangerous,
    test_action_policy_allowed,
    test_secret_detection,
    test_network_port_check,
    test_memory_facade_stats,
    test_schema_inspector,
]

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print("JARVIS Core Tests\n")
    passed = failed = 0
    for test in ALL_TESTS:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            failed += 1
    print(f"\nResults: {passed} passed, {failed} failed")
