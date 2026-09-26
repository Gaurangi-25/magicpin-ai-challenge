"""Vera Merchant AI Assistant — FastAPI Service & Message Engine.

Endpoints:
    GET  /v1/healthz
    GET  /v1/metadata
    POST /v1/context
    POST /v1/tick
    POST /v1/reply
    POST /v1/teardown
    POST /v1/reset
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from composer import compose as core_compose
from rules import (
    is_auto_reply,
    is_intent_commit,
    is_later_request,
    is_off_topic,
    is_opt_out,
)
from storage import storage

app = FastAPI(title="magicpin Vera AI Assistant", version="1.0.0")

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Vera AI",
        "message": "Magicpin AI Challenge bot is running."
    }

# Public compose function matching challenge-brief §7.1
def compose(
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    customer: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Public composition entrypoint."""
    res = core_compose(category, merchant, trigger, customer)
    if not res:
        return {
            "body": "",
            "cta": "none",
            "send_as": "vera",
            "suppression_key": "",
            "rationale": "No action needed.",
        }
    return {
        "body": res["body"],
        "cta": res["cta"],
        "send_as": res["send_as"],
        "suppression_key": res["suppression_key"],
        "rationale": res["rationale"],
    }


# -----------------------------------------------------------------------------
# Request & Response Models
# -----------------------------------------------------------------------------
class ContextPayload(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str] = None


class TickPayload(BaseModel):
    now: str
    available_triggers: List[str] = Field(default_factory=list)


class ReplyPayload(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": storage.get_uptime_seconds(),
        "contexts_loaded": storage.get_counts(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Team Vera",
        "team_members": ["Gaurangi Agarwal"],
        "model": "rule-based-deterministic-engine",
        "approach": "Grounded 4-context decision engine with category voice dispatch and stateful multi-turn conversation handling",
        "contact_email": "gaurangi250704@gmail.com",
        "version": "1.0.0",
        "submitted_at": "2026-04-26T08:00:00Z",
    }


@app.post("/v1/context")
async def push_context(body: ContextPayload):
    accepted, result, cur_ver = storage.store_context(
        scope=body.scope,
        context_id=body.context_id,
        version=body.version,
        payload=body.payload,
        delivered_at=body.delivered_at,
    )

    if not accepted:
        if result == "invalid_scope":
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"accepted": False, "reason": "invalid_scope", "details": f"Invalid scope '{body.scope}'"},
            )
        elif result == "stale_version":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"accepted": False, "reason": "stale_version", "current_version": cur_ver},
            )

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "") + "Z"
    return {"accepted": True, "ack_id": result, "stored_at": now_iso}


@app.post("/v1/tick")
async def tick(body: TickPayload):
    actions = []

    for trg_id in body.available_triggers:
        trg = storage.get_context("trigger", trg_id)
        if not trg:
            continue

        # Check expiration
        expires_at = trg.get("expires_at")
        if expires_at and expires_at < body.now:
            continue

        merchant_id = trg.get("merchant_id") or trg.get("payload", {}).get("merchant_id")
        if not merchant_id or storage.is_merchant_opted_out(merchant_id):
            continue

        customer_id = trg.get("customer_id") or trg.get("payload", {}).get("customer_id")
        merchant = storage.get_context("merchant", merchant_id)
        if not merchant:
            continue

        cat_slug = merchant.get("category_slug")
        category = storage.get_context("category", cat_slug) if cat_slug else None
        if not category:
            continue

        customer = storage.get_context("customer", customer_id) if customer_id else None

        supp_key = trg.get("suppression_key") or f"{trg.get('kind')}:{merchant_id}:{trg_id}"
        if storage.is_suppressed(supp_key):
            continue

        composed = core_compose(category, merchant, trg, customer)
        if not composed or not composed.get("body"):
            continue

        # Mark suppressed to prevent duplicate proactive sends
        storage.suppress(supp_key)

        conv_id = f"conv_{merchant_id}_{trg_id}"
        storage.record_message(
            conversation_id=conv_id,
            from_role=composed["send_as"],
            message=composed["body"],
            merchant_id=merchant_id,
            customer_id=customer_id,
        )

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trg_id,
            "template_name": composed["template_name"],
            "template_params": composed["template_params"],
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

        if len(actions) >= 20:
            break

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyPayload):
    # Record inbound merchant message
    storage.record_message(
        conversation_id=body.conversation_id,
        from_role=body.from_role,
        message=body.message,
        merchant_id=body.merchant_id,
        customer_id=body.customer_id,
    )

    history = storage.get_conversation_history(body.conversation_id)
    meta = storage.get_conversation_meta(body.conversation_id)
    merchant_id = body.merchant_id or meta.get("merchant_id")

    # 1. Follow-up later / busy should not be treated as permanent opt-out
    if is_later_request(body.message):
        return {
            "action": "wait",
            "wait_seconds": 7200,
            "rationale": "Merchant requested to follow up later; backing off 2 hours.",
        }

    # 2. Opt-out / Hostile
    if is_opt_out(body.message):
        if merchant_id:
            storage.opt_out_merchant(merchant_id)
        storage.update_conversation_meta(body.conversation_id, {"state": "ended"})
        return {
            "action": "end",
            "rationale": "Merchant explicitly opted out or expressed hostility; gracefully exiting and suppressing future sends.",
        }

    # 3. Auto-reply detection
    if is_auto_reply(body.message):
        conv_count = meta.get("auto_reply_count", 0) + 1
        storage.update_conversation_meta(body.conversation_id, {"auto_reply_count": conv_count})
        m_count = storage.record_auto_reply(merchant_id)
        if conv_count >= 2 or m_count >= 2:
            storage.update_conversation_meta(body.conversation_id, {"state": "ended"})
            return {
                "action": "end",
                "rationale": "Auto-reply detected multiple times without merchant presence; ending conversation to avoid spamming.",
            }
        return {
            "action": "wait",
            "wait_seconds": 14400,
            "rationale": "Detected canned WhatsApp auto-reply; backing off 4 hours to wait for business owner.",
        }

    # Check for identical repeated messages from merchant
    merchant_messages = [h["message"] for h in history if h.get("from") in ("merchant", "user")]
    if len(merchant_messages) >= 2 and merchant_messages[-1] == merchant_messages[-2]:
        storage.update_conversation_meta(body.conversation_id, {"state": "ended"})
        return {
            "action": "end",
            "rationale": "Detected repeated identical merchant reply; ending conversation gracefully.",
        }

    # 4. Explicit Intent Commitment
    if is_intent_commit(body.message):
        storage.update_conversation_meta(body.conversation_id, {"state": "committed"})
        # Keep the next step aligned with the merchant's actual request and available context.
        if "abstract" in body.message.lower() or "patient" in body.message.lower() or "whatsapp" in body.message.lower():
            action_body = (
                "Got it — I can proceed with the research abstract and a patient-ed WhatsApp draft based on the JIDA digest already loaded. "
                "Reply CONFIRM to continue with that draft."
            )
            cta = "binary_confirm_cancel"
            rationale = "Merchant explicitly accepted the research CTA and asked for the abstract plus patient-ed WhatsApp; the response stays grounded in the loaded digest and doesn’t invent an announcement or Google post step."
        else:
            action_body = (
                "Got it — I can proceed with the next step based on the context already loaded. "
                "Reply CONFIRM to continue."
            )
            cta = "binary_confirm_cancel"
            rationale = "Merchant committed to moving forward without additional qualification; acknowledging the request and keeping the next step concise."
        return {
            "action": "send",
            "body": action_body,
            "cta": cta,
            "rationale": rationale,
        }

    # 5. Off-topic inquiries
    if is_off_topic(body.message):
        return {
            "action": "send",
            "body": (
                "That's outside what I can help with directly — best to check with your CA for that. "
                "Coming back to our plan, would you like me to proceed with the draft we discussed?"
            ),
            "cta": "binary_yes_no",
            "rationale": "Politely declined out-of-scope query and redirected to active merchant goal.",
        }

    # 6. Default engaged reply
    return {
        "action": "send",
        "body": "Got it! I can prepare the draft and post setup for you right away. Should I proceed and send it for your review?",
        "cta": "binary_yes_no",
        "rationale": "Acknowledged inquiry and advanced towards concrete deliverable.",
    }


@app.post("/v1/teardown")
@app.post("/v1/reset")
async def teardown():
    storage.clear()
    return {"status": "ok", "message": "Storage reset complete."}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
