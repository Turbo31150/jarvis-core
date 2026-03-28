"""Tests for JARVIS agents — import checks and method validation."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_github_operator_import():
    from agents.github_operator import GitHubOperator
    op = GitHubOperator()
    assert hasattr(op, "list_repos")
    assert hasattr(op, "get_issues")
    assert hasattr(op, "get_prs")
    assert hasattr(op, "daily_summary")
    assert op.owner == "Turbo31150"
    print("  ✅ test_github_operator_import")


def test_browseros_operator_import():
    from agents.browseros_operator import BrowserOSOperator
    assert hasattr(BrowserOSOperator, "list_pages")
    assert hasattr(BrowserOSOperator, "navigate")
    assert hasattr(BrowserOSOperator, "snapshot")
    assert hasattr(BrowserOSOperator, "click")
    assert hasattr(BrowserOSOperator, "fill")
    assert hasattr(BrowserOSOperator, "health")
    assert hasattr(BrowserOSOperator, "summary")
    print("  ✅ test_browseros_operator_import")


def test_telegram_operator_import():
    from agents.telegram_operator import TelegramOperator
    assert hasattr(TelegramOperator, "send")
    assert hasattr(TelegramOperator, "send_markdown")
    assert hasattr(TelegramOperator, "cmd_health_full")
    assert hasattr(TelegramOperator, "cmd_sql_stats")
    assert hasattr(TelegramOperator, "cmd_agents_status")
    assert hasattr(TelegramOperator, "daily_digest")
    print("  ✅ test_telegram_operator_import")


def test_network_operator_import():
    from agents.network_operator import NetworkOperator
    op = NetworkOperator()
    assert hasattr(op, "scan_ports")
    assert hasattr(op, "check_dns")
    assert hasattr(op, "latency_map")
    assert hasattr(op, "full_report")
    assert hasattr(op, "detect_conflicts")
    print("  ✅ test_network_operator_import")


def test_sql_operator_import():
    from agents.sql_operator import SQLOperator
    assert hasattr(SQLOperator, "get_stats")
    assert hasattr(SQLOperator, "query")
    assert hasattr(SQLOperator, "health_check")
    assert hasattr(SQLOperator, "export_table")
    assert hasattr(SQLOperator, "list_tables")
    assert hasattr(SQLOperator, "row_counts")
    assert hasattr(SQLOperator, "close")
    print("  ✅ test_sql_operator_import")


def test_voice_router_parse_intent():
    """Voice router may not exist yet — test gracefully."""
    try:
        from voice.router import VoiceRouter
        assert hasattr(VoiceRouter, "parse_intent")
        print("  ✅ test_voice_router_parse_intent")
    except ImportError:
        print("  ⏭️  test_voice_router_parse_intent (voice.router not found, skipped)")


def test_container_operator_import():
    """Container operator may not exist yet — test gracefully."""
    try:
        from agents.container_operator import ContainerOperator
        assert hasattr(ContainerOperator, "list_containers")
        print("  ✅ test_container_operator_import")
    except ImportError:
        print("  ⏭️  test_container_operator_import (not implemented yet, skipped)")


def test_all_agents_have_required_methods():
    """Every agent must have a docstring and at least one public method."""
    agent_modules = [
        ("agents.github_operator", "GitHubOperator"),
        ("agents.browseros_operator", "BrowserOSOperator"),
        ("agents.telegram_operator", "TelegramOperator"),
        ("agents.network_operator", "NetworkOperator"),
        ("agents.sql_operator", "SQLOperator"),
    ]
    for module_name, class_name in agent_modules:
        mod = __import__(module_name, fromlist=[class_name])
        cls = getattr(mod, class_name)
        assert cls.__doc__ is not None, f"{class_name} missing docstring"
        public = [m for m in dir(cls) if not m.startswith("_") and callable(getattr(cls, m))]
        assert len(public) >= 1, f"{class_name} has no public methods"
    print("  ✅ test_all_agents_have_required_methods")


ALL_TESTS = [
    test_github_operator_import,
    test_browseros_operator_import,
    test_telegram_operator_import,
    test_network_operator_import,
    test_sql_operator_import,
    test_voice_router_parse_intent,
    test_container_operator_import,
    test_all_agents_have_required_methods,
]

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print("JARVIS Agent Tests\n")
    passed = failed = 0
    for test in ALL_TESTS:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            failed += 1
    print(f"\nResults: {passed} passed, {failed} failed")
