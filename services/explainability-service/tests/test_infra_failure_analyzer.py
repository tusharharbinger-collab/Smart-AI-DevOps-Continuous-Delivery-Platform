import asyncio

from src import infra_failure_analyzer as m


def ev(resource, status, reason, ts, typ="AWS::RDS::DBInstance"):
    return {"resource": resource, "type": typ, "status": status, "reason": reason, "timestamp": ts}


EVENTS = [
    ev("Cache", "CREATE_FAILED", "Resource creation cancelled", "2026-01-01T00:00:05", "AWS::ElastiCache::CacheCluster"),
    ev("Db", "CREATE_FAILED", "DB instance smartcd-db already exists (Service: Rds, Status Code: 400)", "2026-01-01T00:00:02"),
    ev("Stack", "ROLLBACK_IN_PROGRESS", "The following resource(s) failed to create: [Db]", "2026-01-01T00:00:06"),
]


def test_root_cause_filters_cascade_and_orders():
    roots = m.select_root_cause_events(EVENTS)
    assert [e["resource"] for e in roots] == ["Db"]


def test_all_cascade_falls_back_to_failed():
    only = [ev("A", "CREATE_FAILED", "Resource creation cancelled", "1")]
    assert len(m.select_root_cause_events(only)) == 1


def test_no_events():
    assert m.select_root_cause_events([]) == []
    r = m.deterministic_rca("stack", None, [])
    assert r["category"] == "unknown" and r["evidence"]


def test_deterministic_name_conflict():
    r = m.deterministic_rca("stack", None, EVENTS)
    assert r["category"] == "name_conflict"
    assert r["suggested_edit_instruction"]
    assert any("already exists" in e for e in r["evidence"])


def test_deterministic_permissions_has_no_template_edit():
    r = m.deterministic_rca("change_set", "User: arn:x is not authorized to perform: rds:CreateDBInstance", [])
    assert r["category"] == "permissions" and r["suggested_edit_instruction"] is None


def test_deterministic_quota_and_invalid():
    assert m.deterministic_rca("stack", "LimitExceeded: too many VPCs", [])["category"] == "quota"
    assert m.deterministic_rca("stack", "Invalid engine version 99", [])["category"] == "invalid_configuration"


def test_ground_evidence_drops_fabricated():
    pool = ["Db (AWS::RDS::DBInstance) CREATE_FAILED: already exists"]
    assert m.ground_evidence(["already exists", "made up line"], pool) == ["already exists"]


def test_no_api_key_uses_deterministic(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    r = asyncio.run(m.analyze_infra_failure("d", "stack", None, EVENTS))
    assert r["source"] == "deterministic" and r["category"] == "name_conflict"


class _Resp:
    def __init__(self, content):
        self._c = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


def _patch_client(monkeypatch, content=None, exc=None):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            if exc:
                raise exc
            return _Resp(content)

    monkeypatch.setattr(m.httpx, "AsyncClient", C)
    monkeypatch.setenv("GROQ_API_KEY", "test")


def test_model_grounded(monkeypatch):
    import json
    line = "Db (AWS::RDS::DBInstance) CREATE_FAILED: DB instance smartcd-db already exists (Service: Rds, Status Code: 400)"
    _patch_client(monkeypatch, json.dumps({"likely_cause": "Name taken", "evidence": [line, "invented"],
                                           "suggested_fix": "rename", "suggested_edit_instruction": None,
                                           "category": "name_conflict"}))
    r = asyncio.run(m.analyze_infra_failure("d", "stack", None, EVENTS))
    assert r["source"] == "model" and r["evidence"] == [line]


def test_model_ungrounded_discarded(monkeypatch):
    import json
    _patch_client(monkeypatch, json.dumps({"likely_cause": "x", "evidence": ["fake"], "suggested_fix": "y",
                                           "category": "bogus"}))
    r = asyncio.run(m.analyze_infra_failure("d", "stack", None, EVENTS))
    assert r["source"] == "model_ungrounded" and r["evidence"] and "fake" not in r["evidence"]


def test_model_failure_falls_back(monkeypatch):
    _patch_client(monkeypatch, exc=RuntimeError("boom"))
    r = asyncio.run(m.analyze_infra_failure("d", "stack", None, EVENTS))
    assert r["source"] == "deterministic"


def test_model_bad_json_falls_back(monkeypatch):
    _patch_client(monkeypatch, "not json")
    r = asyncio.run(m.analyze_infra_failure("d", "stack", None, EVENTS))
    assert r["source"] == "deterministic"


def test_template_format_error_is_invalid_configuration_with_edit_hint():
    r = m.deterministic_rca("change_set", "Template format error: Unresolved resource dependencies [DBSubnetGroup] in the Resources block", [])
    assert r["category"] == "invalid_configuration" and r["suggested_edit_instruction"]
