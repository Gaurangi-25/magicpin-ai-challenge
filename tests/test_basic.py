"""Unit and contract tests for Vera message engine and FastAPI service."""

from __future__ import annotations

import json
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from bot import app, compose
from storage import storage


@pytest.fixture(autouse=True)
def reset_storage():
    """Ensure clean storage before every test."""
    storage.clear()
    yield
    storage.clear()


@pytest.fixture
def client():
    return TestClient(app)


def test_healthz(client):
    response = client.get("/v1/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data
    assert "contexts_loaded" in data
    assert data["contexts_loaded"]["category"] == 0


def test_metadata(client):
    response = client.get("/v1/metadata")
    assert response.status_code == 200
    data = response.json()
    assert "team_name" in data
    assert "version" in data
    assert "model" in data


def test_context_push_and_idempotency(client):
    cat_payload = {
        "slug": "dentists",
        "voice": {"tone": "peer_clinical"},
        "offer_catalog": [{"title": "Dental Cleaning @ ₹299"}],
        "peer_stats": {"avg_ctr": 0.030},
        "digest": [{"id": "d_1", "title": "JIDA research", "source": "JIDA 2026"}],
    }

    # 1. First push
    res = client.post(
        "/v1/context",
        json={
            "scope": "category",
            "context_id": "dentists",
            "version": 1,
            "payload": cat_payload,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["accepted"] is True
    assert "ack_dentists_v1" in data["ack_id"]

    # Verify healthz count increased
    h = client.get("/v1/healthz").json()
    assert h["contexts_loaded"]["category"] == 1

    # 2. Re-push same version (idempotency check) -> 409 Conflict
    res_stale = client.post(
        "/v1/context",
        json={
            "scope": "category",
            "context_id": "dentists",
            "version": 1,
            "payload": cat_payload,
        },
    )
    assert res_stale.status_code == 409
    data_stale = res_stale.json()
    assert data_stale["accepted"] is False
    assert data_stale["reason"] == "stale_version"
    assert data_stale["current_version"] == 1

    # 3. Version bump replaces atomically -> 200
    cat_payload["voice"]["tone"] = "updated_tone"
    res_v2 = client.post(
        "/v1/context",
        json={
            "scope": "category",
            "context_id": "dentists",
            "version": 2,
            "payload": cat_payload,
        },
    )
    assert res_v2.status_code == 200
    assert res_v2.json()["accepted"] is True

    # 4. Invalid scope -> 400
    res_bad = client.post(
        "/v1/context",
        json={
            "scope": "invalid_scope",
            "context_id": "bad",
            "version": 1,
            "payload": {},
        },
    )
    assert res_bad.status_code == 400


def test_tick_and_suppression(client):
    # Setup category, merchant, and trigger
    client.post(
        "/v1/context",
        json={
            "scope": "category",
            "context_id": "dentists",
            "version": 1,
            "payload": {
                "slug": "dentists",
                "voice": {"tone": "peer_clinical"},
                "digest": [
                    {
                        "id": "d_1",
                        "title": "3-month recall trial",
                        "source": "JIDA Oct 2026",
                        "trial_n": 2100,
                    }
                ],
            },
        },
    )

    client.post(
        "/v1/context",
        json={
            "scope": "merchant",
            "context_id": "m_001",
            "version": 1,
            "payload": {
                "merchant_id": "m_001",
                "category_slug": "dentists",
                "identity": {"name": "Dr. Meera's Clinic", "owner_first_name": "Meera", "locality": "Lajpat Nagar"},
                "performance": {"views": 2400, "calls": 18, "delta_7d": {"views_pct": 0.18}},
                "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}],
                "signals": ["high_risk_adult_cohort"],
            },
        },
    )

    client.post(
        "/v1/context",
        json={
            "scope": "trigger",
            "context_id": "trg_001",
            "version": 1,
            "payload": {
                "id": "trg_001",
                "scope": "merchant",
                "kind": "research_digest",
                "merchant_id": "m_001",
                "payload": {"category": "dentists", "top_item_id": "d_1"},
                "suppression_key": "research:dentists:2026-W17",
            },
        },
    )

    # First tick -> action generated
    res = client.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": ["trg_001"]})
    assert res.status_code == 200
    actions = res.json()["actions"]
    assert len(actions) == 1
    act = actions[0]
    assert act["merchant_id"] == "m_001"
    assert act["send_as"] == "vera"
    assert act["trigger_id"] == "trg_001"
    assert "Dr. Meera" in act["body"]
    assert "JIDA" in act["body"]
    assert act["suppression_key"] == "research:dentists:2026-W17"

    # Second tick with same trigger -> suppressed (empty actions)
    res2 = client.post("/v1/tick", json={"now": "2026-04-26T10:05:00Z", "available_triggers": ["trg_001"]})
    assert res2.status_code == 200
    assert len(res2.json()["actions"]) == 0


def test_reply_auto_reply_handling(client):
    conv_id = "conv_test_auto"
    auto_msg = "Thank you for contacting Dr. Meera's Clinic! Our team will respond shortly."

    # Turn 1: Bot detects auto-reply -> action: wait
    res1 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_001",
            "from_role": "merchant",
            "message": auto_msg,
            "received_at": "2026-04-26T10:10:00Z",
            "turn_number": 2,
        },
    )
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["action"] == "wait"
    assert data1["wait_seconds"] > 0

    # Turn 2: Second auto-reply -> action: end (graceful exit)
    res2 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_001",
            "from_role": "merchant",
            "message": auto_msg,
            "received_at": "2026-04-26T10:15:00Z",
            "turn_number": 3,
        },
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["action"] == "end"


def test_reply_intent_commitment(client):
    conv_id = "conv_test_intent"
    commit_msg = "Ok lets do it. Whats next?"

    res = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_001",
            "from_role": "merchant",
            "message": commit_msg,
            "received_at": "2026-04-26T10:20:00Z",
            "turn_number": 2,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["action"] == "send"
    body_lower = data["body"].lower()

    # Must contain action words
    actioning = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]
    assert any(w in body_lower for w in actioning)

    # Must NOT contain qualifying words
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    assert not any(w in body_lower for w in qualifying)


def test_reply_research_acceptance_uses_abstract_and_patient_ed_whatsapp(client):
    conv_id = "conv_test_research_accept"
    message = "Yes, pull the abstract and draft the patient-ed WhatsApp."

    res = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_001",
            "from_role": "merchant",
            "message": message,
            "received_at": "2026-04-26T10:22:00Z",
            "turn_number": 2,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["action"] == "send"
    body_lower = data["body"].lower()

    assert "abstract" in body_lower
    assert "patient-ed" in body_lower or "patient ed" in body_lower or "whatsapp" in body_lower
    assert "announcement" not in body_lower
    assert "google post" not in body_lower
    assert "draft" in body_lower
    assert "confirm" in body_lower or "proceed" in body_lower


def test_reply_later_not_opt_out(client):
    conv_id = "conv_test_later"
    later_msg = "Not interested right now, maybe later."

    res = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_001",
            "from_role": "merchant",
            "message": later_msg,
            "received_at": "2026-04-26T10:25:00Z",
            "turn_number": 2,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["action"] == "wait"
    assert data["wait_seconds"] == 7200
    assert not storage.is_merchant_opted_out("m_001")


def test_reply_hostile_opt_out(client):
    conv_id = "conv_test_hostile"
    hostile_msg = "Stop messaging me. This is useless spam."

    res = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_001",
            "from_role": "merchant",
            "message": hostile_msg,
            "received_at": "2026-04-26T10:25:00Z",
            "turn_number": 2,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["action"] == "end"
    # Verify merchant is now opted out in storage
    assert storage.is_merchant_opted_out("m_001")


def test_reply_off_topic_redirect(client):
    conv_id = "conv_test_offtopic"
    offtopic_msg = "Can you also help me with my GST filing this month?"

    res = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_001",
            "from_role": "merchant",
            "message": offtopic_msg,
            "received_at": "2026-04-26T10:30:00Z",
            "turn_number": 2,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["action"] == "send"
    assert "ca" in data["body"].lower() or "outside" in data["body"].lower()


def test_compose_grounding_and_voice():
    category = {
        "slug": "dentists",
        "voice": {"tone": "peer_clinical"},
        "digest": [{"id": "d_1", "title": "Trial results", "source": "JIDA Oct 2026, p.14", "trial_n": 2100}],
    }
    merchant = {
        "merchant_id": "m_001",
        "category_slug": "dentists",
        "identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera", "locality": "Lajpat Nagar"},
        "performance": {"views": 2410, "calls": 18, "delta_7d": {"views_pct": 0.18}},
        "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}],
        "signals": ["high_risk_adult_cohort"],
    }
    trigger = {
        "id": "trg_001",
        "scope": "merchant",
        "kind": "research_digest",
        "payload": {"category": "dentists", "top_item_id": "d_1"},
    }

    result = compose(category, merchant, trigger)
    assert result is not None
    assert "Dr. Meera" in result["body"]
    assert "2,100-patient trial" in result["body"]
    assert "JIDA Oct 2026, p.14" in result["body"]
    assert result["send_as"] == "vera"
    # Taboo and url checks
    assert "guaranteed" not in result["body"].lower()
    assert "http://" not in result["body"]
    assert "https://" not in result["body"]


def test_compose_customer_facing_recall():
    category = {"slug": "dentists", "voice": {"tone": "peer_clinical"}}
    merchant = {
        "merchant_id": "m_001",
        "category_slug": "dentists",
        "identity": {"name": "Dr. Meera's Clinic", "owner_first_name": "Meera", "locality": "Lajpat Nagar"},
        "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}],
    }
    trigger = {
        "id": "trg_recall",
        "scope": "customer",
        "kind": "recall_due",
        "payload": {
            "service_due": "6_month_cleaning",
            "available_slots": [{"label": "Wed 5 Nov, 6pm"}, {"label": "Thu 6 Nov, 5pm"}],
        },
    }
    customer = {
        "customer_id": "c_001",
        "identity": {"name": "Priya", "language_pref": "hi-en mix"},
    }

    result = compose(category, merchant, trigger, customer)
    assert result["send_as"] == "merchant_on_behalf"
    assert "Priya" in result["body"]
    assert "Wed 5 Nov, 6pm" in result["body"]
    assert result["cta"] == "multi_choice_slot"
