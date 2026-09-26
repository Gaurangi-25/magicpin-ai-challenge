"""In-memory state and context storage for Vera.

Thread-safe storage supporting:
- Context storage with atomic version updates and idempotency
- Conversation histories and per-conversation state
- Suppression key dedup and merchant opt-out tracking
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple


class Storage:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._contexts: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._conversations: Dict[str, List[Dict[str, Any]]] = {}
        self._conversation_meta: Dict[str, Dict[str, Any]] = {}
        self._merchant_auto_replies: Dict[str, int] = {}
        self._suppressed_keys: Set[str] = set()
        self._suppressed_merchants: Set[str] = set()
        self._start_time = time.time()

    def get_uptime_seconds(self) -> int:
        return int(time.time() - self._start_time)

    def get_counts(self) -> Dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        with self._lock:
            for (scope, _), _ in self._contexts.items():
                if scope in counts:
                    counts[scope] += 1
        return counts

    def store_context(
        self, scope: str, context_id: str, version: int, payload: Dict[str, Any], delivered_at: Optional[str] = None
    ) -> Tuple[bool, str, Optional[int]]:
        """Stores or updates a context.

        Returns:
            (accepted, ack_or_reason, current_version)
        """
        valid_scopes = {"category", "merchant", "customer", "trigger"}
        if scope not in valid_scopes:
            return False, "invalid_scope", None

        key = (scope, context_id)
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "") + "Z"

        with self._lock:
            cur = self._contexts.get(key)
            if cur is not None:
                cur_ver = cur["version"]
                if version <= cur_ver:
                    # Stale or duplicate version: 409 conflict
                    return False, "stale_version", cur_ver

            # Store / atomically replace
            self._contexts[key] = {
                "scope": scope,
                "context_id": context_id,
                "version": version,
                "payload": payload,
                "delivered_at": delivered_at or now_iso,
                "stored_at": now_iso,
            }
            ack_id = f"ack_{context_id}_v{version}"
            return True, ack_id, version

    def get_context(self, scope: str, context_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._contexts.get((scope, context_id))
            return entry["payload"] if entry else None

    def get_all_contexts(self, scope: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if scope:
                return [v["payload"] for (s, _), v in self._contexts.items() if s == scope]
            return [v["payload"] for v in self._contexts.values()]

    def is_suppressed(self, key: str) -> bool:
        if not key:
            return False
        with self._lock:
            return key in self._suppressed_keys

    def suppress(self, key: str) -> None:
        if key:
            with self._lock:
                self._suppressed_keys.add(key)

    def is_merchant_opted_out(self, merchant_id: str) -> bool:
        if not merchant_id:
            return False
        with self._lock:
            return merchant_id in self._suppressed_merchants

    def opt_out_merchant(self, merchant_id: str) -> None:
        if merchant_id:
            with self._lock:
                self._suppressed_merchants.add(merchant_id)

    def record_message(
        self,
        conversation_id: str,
        from_role: str,
        message: str,
        merchant_id: Optional[str] = None,
        customer_id: Optional[str] = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "") + "Z"
        with self._lock:
            history = self._conversations.setdefault(conversation_id, [])
            history.append({
                "from": from_role,
                "message": message,
                "ts": now_iso,
            })
            meta = self._conversation_meta.setdefault(conversation_id, {
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "auto_reply_count": 0,
                "turns": 0,
                "state": "active",
            })
            if merchant_id:
                meta["merchant_id"] = merchant_id
            if customer_id:
                meta["customer_id"] = customer_id
            meta["turns"] = len(history)

    def get_conversation_history(self, conversation_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._conversations.get(conversation_id, []))

    def get_conversation_meta(self, conversation_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._conversation_meta.get(conversation_id, {}))

    def update_conversation_meta(self, conversation_id: str, updates: Dict[str, Any]) -> None:
        with self._lock:
            meta = self._conversation_meta.setdefault(conversation_id, {
                "auto_reply_count": 0,
                "turns": 0,
                "state": "active",
            })
            meta.update(updates)

    def record_auto_reply(self, merchant_id: Optional[str]) -> int:
        if not merchant_id:
            return 1
        with self._lock:
            count = self._merchant_auto_replies.get(merchant_id, 0) + 1
            self._merchant_auto_replies[merchant_id] = count
            return count

    def clear(self) -> None:
        """Resets all storage (used for test setup / teardown)."""
        with self._lock:
            self._contexts.clear()
            self._conversations.clear()
            self._conversation_meta.clear()
            self._merchant_auto_replies.clear()
            self._suppressed_keys.clear()
            self._suppressed_merchants.clear()
            self._start_time = time.time()


# Global in-memory storage singleton
storage = Storage()
