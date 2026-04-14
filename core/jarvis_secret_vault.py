#!/usr/bin/env python3
"""JARVIS Secret Vault — Encrypted secret storage with access control and audit trail"""

import redis
import json
import time
import hashlib
import base64

r = redis.Redis(decode_responses=True)

VAULT_PREFIX = "jarvis:vault:secret:"
ACL_PREFIX = "jarvis:vault:acl:"
AUDIT_KEY = "jarvis:vault:audit"
STATS_KEY = "jarvis:vault:stats"
INDEX_KEY = "jarvis:vault:index"


def _derive_key(master_key: str, salt: str) -> bytes:
    """Derive a deterministic key from master + salt."""
    return hashlib.pbkdf2_hmac("sha256", master_key.encode(), salt.encode(), 10000)


def _xor_encrypt(data: bytes, key: bytes) -> bytes:
    """Simple XOR cipher (for demo — use Fernet/AES in prod)."""
    key_stream = (key * (len(data) // len(key) + 1))[: len(data)]
    return bytes(a ^ b for a, b in zip(data, key_stream))


def store_secret(
    name: str,
    value: str,
    master_key: str,
    owner: str = "system",
    acl: list = None,
    ttl_days: int = 90,
) -> str:
    sid = hashlib.md5(f"{owner}:{name}".encode()).hexdigest()[:12]
    salt = hashlib.md5(f"{sid}:{time.time()}".encode()).hexdigest()
    key = _derive_key(master_key, salt)
    encrypted = base64.b64encode(_xor_encrypt(value.encode(), key)).decode()

    entry = {
        "id": sid,
        "name": name,
        "owner": owner,
        "encrypted": encrypted,
        "salt": salt,
        "created_at": time.time(),
        "access_count": 0,
    }
    ttl = ttl_days * 86400
    r.setex(f"{VAULT_PREFIX}{sid}", ttl, json.dumps(entry))
    r.sadd(INDEX_KEY, sid)

    # ACL
    allowed = list(set([owner] + (acl or [])))
    r.setex(f"{ACL_PREFIX}{sid}", ttl, json.dumps(allowed))

    _audit(sid, "store", owner, name)
    r.hincrby(STATS_KEY, "secrets_stored", 1)
    return sid


def get_secret(
    name: str, master_key: str, requester: str = "system", owner: str = "system"
) -> str | None:
    sid = hashlib.md5(f"{owner}:{name}".encode()).hexdigest()[:12]

    # ACL check
    acl_raw = r.get(f"{ACL_PREFIX}{sid}")
    if acl_raw:
        allowed = json.loads(acl_raw)
        if requester not in allowed:
            _audit(sid, "access_denied", requester, name)
            r.hincrby(STATS_KEY, "access_denied", 1)
            return None

    raw = r.get(f"{VAULT_PREFIX}{sid}")
    if not raw:
        return None

    entry = json.loads(raw)
    key = _derive_key(master_key, entry["salt"])
    try:
        decrypted = _xor_encrypt(base64.b64decode(entry["encrypted"]), key).decode()
    except UnicodeDecodeError:
        return None  # wrong master key → garbled bytes

    entry["access_count"] += 1
    entry["last_accessed"] = time.time()
    entry["last_accessor"] = requester
    r.setex(f"{VAULT_PREFIX}{sid}", r.ttl(f"{VAULT_PREFIX}{sid}"), json.dumps(entry))

    _audit(sid, "get", requester, name)
    r.hincrby(STATS_KEY, "secrets_retrieved", 1)
    return decrypted


def list_secrets(owner: str = None) -> list:
    sids = r.smembers(INDEX_KEY)
    result = []
    for sid in sids:
        raw = r.get(f"{VAULT_PREFIX}{sid}")
        if raw:
            entry = json.loads(raw)
            if owner is None or entry.get("owner") == owner:
                result.append(
                    {
                        "id": sid,
                        "name": entry["name"],
                        "owner": entry["owner"],
                        "access_count": entry.get("access_count", 0),
                        "ttl_s": r.ttl(f"{VAULT_PREFIX}{sid}"),
                    }
                )
    return result


def _audit(sid: str, action: str, actor: str, name: str):
    r.lpush(
        AUDIT_KEY,
        json.dumps(
            {
                "sid": sid,
                "action": action,
                "actor": actor,
                "name": name,
                "ts": time.time(),
            }
        ),
    )
    r.ltrim(AUDIT_KEY, 0, 999)


def get_audit_log(limit: int = 10) -> list:
    return [json.loads(x) for x in r.lrange(AUDIT_KEY, 0, limit - 1)]


def stats() -> dict:
    s = r.hgetall(STATS_KEY)
    return {"vault_size": r.scard(INDEX_KEY), **{k: int(v) for k, v in s.items()}}


if __name__ == "__main__":
    MASTER = "jarvis-master-key-2026"

    store_secret(
        "hl_private_key",
        "0xFAKE_PK_TEST_123",
        MASTER,
        owner="trading",
        acl=["turbo"],
        ttl_days=365,
    )
    store_secret(
        "telegram_token",
        "bot:TOKEN_FAKE_456",
        MASTER,
        owner="comms",
        acl=["turbo", "notif_agent"],
    )
    store_secret("openai_key", "sk-FAKE_OPENAI_789", MASTER, owner="turbo")

    # Retrieve
    pk = get_secret("hl_private_key", MASTER, requester="turbo", owner="trading")
    print(f"hl_private_key retrieved: {pk[:20]}...")

    # Denied access
    blocked = get_secret("openai_key", MASTER, requester="unknown_agent", owner="turbo")
    print(
        f"Access by unknown_agent: {'denied' if blocked is None else 'ALLOWED (bug!)'}"
    )

    # Wrong key
    wrong = get_secret(
        "hl_private_key", "wrong-key", requester="turbo", owner="trading"
    )
    print(
        f"Wrong master key: {'garbled (expected)' if wrong and wrong != '0xFAKE_PK_TEST_123' else wrong}"
    )

    print("\nVault contents:")
    for s in list_secrets():
        print(
            f"  [{s['owner']:8s}] {s['name']:20s} accessed={s['access_count']}x  ttl={s['ttl_s']}s"
        )

    print("\nAudit log:")
    for log in get_audit_log(6):
        print(f"  {log['action']:14s} {log['name']} by {log['actor']}")

    print(f"\nStats: {stats()}")
