"""Durable Edge execution metadata in the existing PurchaseIntent database.

The queue is a transport journal, never a second order database. A dispatched
command is never delivered twice. Only a fresh, explicitly requested resume of
the original intent may continue after a pause; the extension must reconcile
its own before-dispatch action journal before any subsequent mutation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from autograb.edge.protocol import PROVIDERS, ProtocolError, make_message, now, timestamp, validate
from autograb.storage.intents import IntentStore

_PAUSED = {"WAITING_FOR_HUMAN", "DISCONNECTED", "DISARMED", "FAILED", "SITE_CHANGED"}
_FINISHED = {"CHECKOUT_READY", "CANCELLED", "SOLD_OUT"}
_ORDER_EVENTS = {"ORDER_CREATED", "INVOICE_FOUND", "PAYMENT_READY"}


class EdgeBroker:
    def __init__(self, store):
        self.store = store
        self.connection = store.connection
        self.intents = IntentStore(store)
        with store._transaction():
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(purchase_intents)")}
            for name, declaration in {
                "edge_state": "TEXT", "edge_checkpoint": "TEXT", "edge_command_id": "TEXT",
                "edge_error_code": "TEXT",
                "edge_tab_id": "INTEGER", "edge_mutation_uncertain": "INTEGER NOT NULL DEFAULT 0",
                "edge_pause_generation": "INTEGER NOT NULL DEFAULT 0",
                "edge_normal_returned": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    self.connection.execute(f"ALTER TABLE purchase_intents ADD COLUMN {name} {declaration}")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS edge_transport_state (
                id INTEGER PRIMARY KEY CHECK(id=1), connected INTEGER NOT NULL DEFAULT 0,
                connection_id TEXT, version TEXT, last_heartbeat TEXT,
                current_tab INTEGER, current_intent TEXT, challenge_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                execution_state TEXT NOT NULL DEFAULT 'DISCONNECTED')""")
            self.connection.execute("INSERT OR IGNORE INTO edge_transport_state(id) VALUES(1)")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS edge_commands (
                command_id TEXT PRIMARY KEY, type TEXT NOT NULL, intent_id TEXT,
                message_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                dispatched_at TEXT, connection_id TEXT)""")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS edge_messages (
                message_id TEXT PRIMARY KEY, command_id TEXT NOT NULL, type TEXT NOT NULL,
                received_at TEXT NOT NULL)""")
            command_columns = {r[1] for r in self.connection.execute("PRAGMA table_info(edge_commands)")}
            if "order_result_json" not in command_columns:
                self.connection.execute("ALTER TABLE edge_commands ADD COLUMN order_result_json TEXT")

    def enqueue_order(self, kind, intent_id, payload):
        """Use the existing queue; submit requires an already committed nonce."""
        from .order_protocol import ORDER_COMMANDS, permit_fresh
        if kind not in ORDER_COMMANDS:
            raise ProtocolError("ORDER_COMMAND_INVALID")
        intent = self.intents._required(intent_id)
        message = make_message(kind, intent_id=intent_id, product_id=intent["product_id"],
                               provider=intent["provider"], payload=payload)
        with self.store._transaction():
            intent = self.intents._required(intent_id)
            if not self.status()["connected"]:
                raise ProtocolError("EDGE_DISCONNECTED")
            active = self._state()["current_intent"]
            if active and active != intent_id:
                previous = self.intents.get(active)
                if previous and previous["edge_state"] not in _FINISHED | _PAUSED:
                    raise ProtocolError("EXECUTOR_BUSY")
            if self.connection.execute("SELECT 1 FROM edge_commands WHERE intent_id=? AND status IN ('QUEUED','DISPATCHED')", (intent_id,)).fetchone():
                raise ProtocolError("INTENT_COMMAND_PENDING")
            if intent["edge_tab_id"] is not None and intent["edge_tab_id"] != payload["tab_id"]:
                raise ProtocolError("TAB_IDENTITY_MISMATCH")
            saved = self.store.get_event(intent["event_id"])["product"]
            product = payload["product"]
            if (product["name"] != saved["name"] or product["url"] != saved["product_url"]
                    or not any(all(p.get(k) == product[k] for k in ("cents", "currency", "period")) and p.get("available") is True for p in saved["prices"])):
                raise ProtocolError("INTENT_PRODUCT_CHANGED")
            if kind == "ORDER_PRECHECK" and (intent["state"] not in {"CHECKOUT_READY", "ORDER_PRECHECK"} or intent["submit_started_at"]):
                raise ProtocolError("CHECKOUT_REQUIRED")
            if kind == "SUBMIT_ORDER":
                permit, check = payload["permit"], intent["order_precheck"]
                if (intent["state"] != "ORDER_SUBMITTING" or intent["submission_nonce"] != permit["nonce"]
                        or not check or check["precheck_id"] != permit["precheck_id"] or check["tab_id"] != payload["tab_id"]
                        or not permit_fresh(permit) or self.connection.execute("SELECT 1 FROM edge_commands WHERE intent_id=? AND type='SUBMIT_ORDER'", (intent_id,)).fetchone()):
                    raise ProtocolError("ORDER_SUBMISSION_REJECTED")
            if kind == "RECONCILE_ORDER" and (payload["submission_nonce"] != intent["submission_nonce"]
                    or payload["submitted_at"] != (intent["submit_started_at"] if intent["submission_nonce"] else None)
                    or payload["order_id"] != intent["order_id"] or payload["invoice_id"] != intent["invoice_id"]):
                raise ProtocolError("ORDER_SUBMISSION_IDENTITY_MISMATCH")
            self.connection.execute("UPDATE purchase_intents SET edge_state='QUEUED',edge_command_id=?,updated_at=? WHERE intent_id=?", (message["command_id"], now(), intent_id))
            self.connection.execute("UPDATE edge_transport_state SET current_intent=?,execution_state='QUEUED' WHERE id=1", (intent_id,))
            self.connection.execute("INSERT INTO edge_commands(command_id,type,intent_id,message_json,status,created_at) VALUES(?,?,?,?,'QUEUED',?)",
                                    (message["command_id"], kind, intent_id, json.dumps(message), now()))
        return message

    def order_result(self, command_id):
        row = self.connection.execute("SELECT status,order_result_json FROM edge_commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            raise ProtocolError("COMMAND_UNKNOWN")
        return {"status": row["status"], "observation": json.loads(row["order_result_json"]) if row["order_result_json"] else None}

    def _order_observation(self, message):
        payload = message["payload"]
        with self.store._transaction():
            if not self.status()["connected"]:
                raise ProtocolError("EDGE_DISCONNECTED")
            if self.connection.execute("SELECT 1 FROM edge_messages WHERE message_id=?", (message["message_id"],)).fetchone():
                raise ProtocolError("MESSAGE_REPLAYED")
            command = self.connection.execute("SELECT * FROM edge_commands WHERE command_id=?", (message["command_id"],)).fetchone()
            if command is None or command["status"] != "DISPATCHED" or command["connection_id"] != self._state()["connection_id"]:
                raise ProtocolError("COMMAND_CORRELATION_REJECTED")
            original = json.loads(command["message_json"])
            if original["type"] not in {"ORDER_PRECHECK", "SUBMIT_ORDER", "RECONCILE_ORDER"}:
                raise ProtocolError("ORDER_COMMAND_INVALID")
            if any(original[k] != message[k] for k in ("provider", "product_id", "intent_id")) or payload["tab_id"] != original["payload"]["tab_id"]:
                raise ProtocolError("COMMAND_IDENTITY_MISMATCH")
            outcome = payload["outcome"]
            if (original["type"] == "ORDER_PRECHECK" and outcome not in {"PRECHECK_READY", "UNKNOWN", "LOGIN_REQUIRED", "HUMAN_ACTION_REQUIRED"}
                    or original["type"] != "ORDER_PRECHECK" and outcome == "PRECHECK_READY"):
                raise ProtocolError("ORDER_RESULT_STAGE_INVALID")
            if "observed_at" in payload and not 0 <= (datetime.now(timezone.utc)-timestamp(payload["observed_at"])).total_seconds() <= 120:
                raise ProtocolError("ORDER_EVIDENCE_STALE")
            if outcome not in {"UNKNOWN", "LOGIN_REQUIRED", "HUMAN_ACTION_REQUIRED"}:
                if any(payload[k] != original["payload"]["product"][v] for k, v in (("amount_cents", "cents"), ("currency", "currency"), ("billing", "period"))):
                    raise ProtocolError("ORDER_QUOTE_MISMATCH")
            intent = self.intents._required(message["intent_id"])
            if "submission_nonce" in payload and payload["submission_nonce"] != intent["submission_nonce"]:
                raise ProtocolError("ORDER_SUBMISSION_IDENTITY_MISMATCH")
            if outcome in {"ORDER_FOUND", "INVOICE_FOUND", "PAYMENT_READY"}:
                for field in ("order_id", "invoice_id"):
                    if intent[field] is not None and payload.get(field, intent[field]) != intent[field]:
                        raise ProtocolError("ORDER_RESULT_IDENTITY_MISMATCH")
                if intent["order_id"] is None:
                    if (not intent["submit_started_at"] or "created_at" not in payload
                            or (timestamp(payload["created_at"])-timestamp(intent["submit_started_at"])).total_seconds() < -5
                            or timestamp(payload["created_at"]) > timestamp(payload["observed_at"])):
                        raise ProtocolError("ORDER_TIME_SCOPE_UNVERIFIED")
            self.connection.execute("INSERT INTO edge_messages(message_id,command_id,type,received_at) VALUES(?,?,?,?)", (message["message_id"], message["command_id"], message["type"], now()))
            self.connection.execute("UPDATE edge_commands SET status='DONE',order_result_json=? WHERE command_id=?", (json.dumps(payload), message["command_id"]))
            self.connection.execute("UPDATE purchase_intents SET edge_tab_id=?,edge_state='CHECKOUT_READY',updated_at=? WHERE intent_id=?", (payload["tab_id"], now(), message["intent_id"]))
            self.connection.execute("UPDATE edge_transport_state SET current_intent=NULL,execution_state='READY' WHERE id=1")
        return {"accepted": True, "type": message["type"], "human_notification": None}

    def _state(self):
        return dict(self.connection.execute("SELECT * FROM edge_transport_state WHERE id=1").fetchone())

    def status(self):
        state = self._state()
        age = ((datetime.now(timezone.utc) - timestamp(state["last_heartbeat"])).total_seconds()
               if state["last_heartbeat"] else float("inf"))
        intent = self.intents.get(state["current_intent"]) if state["current_intent"] else None
        return {
            "installed": "OBSERVED" if state["version"] else "UNKNOWN",
            "connected": bool(state["connected"] and 0 <= age <= 45),
            "version": state["version"], "last_heartbeat": state["last_heartbeat"],
            "current_tab": state["current_tab"], "current_intent": state["current_intent"],
            "challenge_status": state["challenge_status"], "execution_state": state["execution_state"],
            "intent_state": intent["state"] if intent else None,
            "checkpoint": intent["edge_checkpoint"] if intent else None,
            "error_code": intent["edge_error_code"] if intent else None,
            "resume_available": bool(intent and intent["edge_state"] in _PAUSED and intent["edge_normal_returned"]),
            "mutation_uncertain": bool(intent and intent["edge_mutation_uncertain"]),
            "live": "OFF", "disarmed": True,
        }

    def _required_intent(self, intent_id, product_id, provider=None):
        intent = self.intents.get(intent_id)
        if (intent is None or intent["provider"] not in PROVIDERS or intent["product_id"] != product_id
                or provider is not None and provider != intent["provider"]
                or intent["origin"] != "REAL" or intent["submit_started_at"] is not None
                or intent["order_id"] is not None or intent["invoice_id"] is not None
                or intent["payment_url"] is not None or intent["payment_page_verified"]
                or intent["verification"] is not None
                or intent["state"] not in {"INTENT_CREATED", "CART_READY", "CHECKOUT_READY"}):
            raise ProtocolError("INTENT_IDENTITY_REJECTED")
        return intent

    def can_start_provider(self, provider):
        """A completed DRY checkout may release the executor, never its cart lock."""
        current_id = self._state()["current_intent"]
        if current_id is None:
            return True
        current = self.intents.get(current_id)
        if (not current or current["provider"] == provider
                or current["state"] != "CHECKOUT_READY" or current["edge_state"] != "CHECKOUT_READY"
                or current["edge_mutation_uncertain"]):
            return False
        self._required_intent(current_id, current["product_id"])
        row = self.connection.execute("SELECT status FROM edge_commands WHERE command_id=?", (current["edge_command_id"],)).fetchone()
        return bool(row and row[0] == "DONE")

    def _release_cancelled_diagnostic(self, intent_id):
        """Release only an acknowledged, pure read-only history in this transaction."""
        intent = self.intents.get(intent_id)
        if (not intent or intent["state"] != "INTENT_CREATED" or intent["edge_state"] != "CANCELLED"
                or intent["edge_checkpoint"] not in {None, "PAGE_OPENED"}
                or intent["edge_mutation_uncertain"] or intent["cart_identifier"] is not None
                or any(intent[key] is not None for key in ("order_id", "invoice_id", "payment_url",
                    "submit_started_at", "submit_finished_at", "verification", "terminal_evidence"))
                or intent["payment_page_verified"]):
            return False
        history = self.connection.execute("SELECT * FROM edge_commands WHERE intent_id=?", (intent_id,)).fetchall()
        opened, cancelled = False, False
        for command in history:
            if command["type"] not in {"OPEN_PRODUCT", "CANCEL_INTENT"} or command["status"] in {"QUEUED", "DISPATCHED"}:
                return False
            try:
                message = validate(json.loads(command["message_json"]), direction="command", fresh=False)
                if (message["type"] != command["type"] or message["command_id"] != command["command_id"]
                        or message["intent_id"] != intent_id or message["provider"] != intent["provider"]
                        or message["product_id"] != intent["product_id"]):
                    return False
                if command["type"] == "OPEN_PRODUCT":
                    query = parse_qs(urlsplit(message["payload"]["product"]["url"]).query)
                    if query.get("a") == ["add"] or query.get("action") == ["add"]:
                        return False  # A navigation can itself be a cart mutation.
                    opened = True
                elif command["command_id"] == intent["edge_command_id"] and command["status"] == "DONE":
                    cancelled = True
            except (ValueError, TypeError, KeyError):
                return False
        if not opened or not cancelled:
            return False
        self.intents.abort_before_submit(intent_id)
        self.connection.execute("""UPDATE edge_transport_state SET current_intent=NULL,
            execution_state='READY',challenge_status='UNKNOWN' WHERE id=1 AND current_intent=?""", (intent_id,))
        return True

    def enqueue(self, kind, intent_id=None, product_id=None, payload=None):
        if kind == "START_CHECKOUT":
            raise ProtocolError("LIVE_NOT_ENABLED")
        with self.store._transaction():
            state = self._state()
            intent = self._required_intent(intent_id, product_id) if intent_id else None
            message = make_message(kind, intent_id=intent_id, product_id=product_id, payload=payload,
                                   provider=intent["provider"] if intent else "bandwagon")
            validate(message, direction="command")
            if intent_id:
                if state["current_intent"] not in {None, intent_id}:
                    if kind not in {"OPEN_PRODUCT", "START_DRY_RUN"} or not self.can_start_provider(intent["provider"]):
                        raise ProtocolError("ANOTHER_EDGE_INTENT_ACTIVE")
                if kind != "CANCEL_INTENT":
                    saved = self.store.get_event(intent["event_id"])
                    if (saved is None or saved["provider"] != intent["provider"]
                            or saved["product_id"] != product_id
                            or saved["product"]["product_url"] != message["payload"]["product"]["url"]
                            or saved["product"]["name"] != message["payload"]["product"]["name"]):
                        raise ProtocolError("INTENT_PRODUCT_CHANGED")
                if kind in {"START_DRY_RUN", "RESUME_INTENT"} and not self.status()["connected"]:
                    raise ProtocolError("EDGE_DISCONNECTED")
                if kind in {"START_DRY_RUN", "OPEN_PRODUCT"}:
                    allowed = {None, "OPENED"} if kind == "START_DRY_RUN" else {None}
                    if intent["edge_state"] not in allowed or intent["state"] != "INTENT_CREATED":
                        raise ProtocolError("INTENT_ALREADY_STARTED")
                    if self.connection.execute("SELECT 1 FROM edge_commands WHERE intent_id=? AND status='QUEUED'", (intent_id,)).fetchone():
                        raise ProtocolError("INTENT_COMMAND_PENDING")
                if kind == "RESUME_INTENT":
                    if intent["edge_state"] not in _PAUSED or not intent["edge_normal_returned"]:
                        raise ProtocolError("NORMAL_PAGE_NOT_VERIFIED")
                    previous = self.connection.execute("SELECT message_json FROM edge_commands WHERE command_id=?", (intent["edge_command_id"],)).fetchone()
                    if previous is None or json.loads(previous[0])["payload"] != message["payload"]:
                        raise ProtocolError("RESUME_PRODUCT_CHANGED")
                if kind == "CANCEL_INTENT":
                    # Cancel local execution only. It does not remove a server cart.
                    self.connection.execute("UPDATE edge_commands SET status='HALTED' WHERE intent_id=? AND status IN ('QUEUED','DISPATCHED')", (intent_id,))
                    next_state = "CANCELLED"
                else:
                    next_state = "QUEUED"
                self.connection.execute("""UPDATE purchase_intents SET edge_state=?,edge_command_id=?,
                    edge_normal_returned=0,edge_error_code=NULL,updated_at=? WHERE intent_id=?""",
                    (next_state, message["command_id"], now(), intent_id))
                self.connection.execute("UPDATE edge_transport_state SET current_intent=?,execution_state=? WHERE id=1", (intent_id, next_state))
            if kind == "DISARM":
                self._halt("DISARMED")
            self.connection.execute("""INSERT INTO edge_commands(command_id,type,intent_id,message_json,status,created_at)
                VALUES(?,?,?,?,'QUEUED',?)""", (message["command_id"], kind, intent_id, json.dumps(message), now()))
        return message

    def next_command(self):
        with self.store._transaction():
            state = self._state()
            if not self.status()["connected"]:
                return None
            row = self.connection.execute("""SELECT * FROM edge_commands WHERE status='QUEUED'
                ORDER BY CASE type WHEN 'DISARM' THEN 0 WHEN 'CANCEL_INTENT' THEN 1 ELSE 2 END,rowid LIMIT 1""").fetchone()
            if row is None:
                return None
            message = json.loads(row["message_json"])
            if message["type"] == "SUBMIT_ORDER":
                from .order_protocol import permit_fresh
                from autograb.core.live import signal_present
                from pathlib import Path
                from autograb.core.lock import lock_is_held, submission_lease_path
                data = Path(self.store.path).parent
                intent = self.intents._required(message['intent_id'])
                permit = message['payload']['permit']
                check = intent['order_precheck'] or {}
                if (intent['state'] != 'ORDER_SUBMITTING' or intent['submission_nonce'] != permit['nonce']
                        or intent['submit_started_at'] != permit['issued_at']
                        or intent['order_id'] is not None or intent['invoice_id'] is not None
                        or check.get('precheck_id') != permit['precheck_id']
                        or check.get('tab_id') != message['payload']['tab_id']
                        or intent['edge_command_id'] != row['command_id']
                        or not lock_is_held(submission_lease_path(data, permit['nonce']))
                        or not permit_fresh(permit) or signal_present(data, "disarm")
                        or signal_present(data, "stop_monitoring")):
                    self._halt("DISARMED")
                    return None
            message["timestamp"] = now()
            validate(message, direction="command")
            self.connection.execute("""UPDATE edge_commands SET status='DISPATCHED',dispatched_at=?,
                connection_id=?,message_json=? WHERE command_id=? AND status='QUEUED'""",
                (now(), state["connection_id"], json.dumps(message), row["command_id"]))
            if row["intent_id"] and row["type"] != "CANCEL_INTENT":
                self.connection.execute("UPDATE purchase_intents SET edge_state='RUNNING',updated_at=? WHERE intent_id=?", (now(), row["intent_id"]))
                self.connection.execute("UPDATE edge_transport_state SET execution_state='RUNNING' WHERE id=1")
            return message

    def _halt(self, reason):
        state = self._state()
        self.connection.execute("UPDATE edge_commands SET status='HALTED' WHERE status IN ('QUEUED','DISPATCHED') AND intent_id IS NOT NULL")
        self.connection.execute("UPDATE purchase_intents SET state='ORDER_UNCERTAIN',updated_at=? WHERE state='ORDER_SUBMITTING' AND submission_nonce IS NOT NULL", (now(),))
        if state["current_intent"]:
            intent = self.intents.get(state["current_intent"])
            if intent and intent["edge_state"] not in _FINISHED:
                self.connection.execute("""UPDATE purchase_intents SET edge_state=?,edge_normal_returned=0,
                    updated_at=? WHERE intent_id=?""", (reason, now(), state["current_intent"]))
        self.connection.execute("UPDATE edge_transport_state SET execution_state=? WHERE id=1", (reason,))

    def disconnect(self):
        with self.store._transaction():
            self._halt("DISCONNECTED")
            self.connection.execute("UPDATE edge_transport_state SET connected=0,challenge_status='UNKNOWN' WHERE id=1")

    def handle_event(self, message):
        validate(message, direction="event")
        kind, payload = message["type"], message["payload"]
        if kind == "ORDER_OBSERVATION":
            return self._order_observation(message)
        if kind in _ORDER_EVENTS:
            raise ProtocolError("LIVE_NOT_ENABLED")
        result = {"accepted": True, "type": kind, "human_notification": None}
        with self.store._transaction():
            if self.connection.execute("SELECT 1 FROM edge_messages WHERE message_id=?", (message["message_id"],)).fetchone():
                raise ProtocolError("MESSAGE_REPLAYED")
            state = self._state()
            command = self.connection.execute("SELECT * FROM edge_commands WHERE command_id=?", (message["command_id"],)).fetchone()
            if kind == "EDGE_READY":
                if command:
                    if command["type"] not in {"PING", "GET_STATUS", "DISARM"} or command["status"] != "DISPATCHED" or command["connection_id"] != state["connection_id"]:
                        raise ProtocolError("COMMAND_CORRELATION_REJECTED")
                    self.connection.execute("UPDATE edge_commands SET status='DONE' WHERE command_id=?", (message["command_id"],))
                    connection_id = state["connection_id"]
                else:
                    if state["connected"] and message["command_id"] != state["connection_id"]:
                        raise ProtocolError("CONNECTION_CONFLICT")
                    connection_id = message["command_id"]
                self.connection.execute("""UPDATE edge_transport_state SET connected=1,connection_id=?,version=?,
                    last_heartbeat=?,current_tab=COALESCE(?,current_tab),execution_state=
                    CASE WHEN current_intent IS NULL THEN 'READY' ELSE execution_state END WHERE id=1""",
                    (connection_id, payload["version"], now(), payload.get("tab_id")))
            else:
                if not self.status()["connected"]:
                    raise ProtocolError("EDGE_DISCONNECTED")
                if command is None:
                    raise ProtocolError("COMMAND_UNKNOWN")
                intent = self._required_intent(message["intent_id"], message["product_id"], message["provider"])
                if json.loads(command["message_json"])["provider"] != message["provider"]:
                    raise ProtocolError("COMMAND_IDENTITY_MISMATCH")
                if (state["current_intent"] != message["intent_id"] or command["intent_id"] != message["intent_id"]
                        or intent["edge_command_id"] != message["command_id"]):
                    raise ProtocolError("COMMAND_IDENTITY_MISMATCH")
                # Read-only PAGE_OPENED may prove a page is normal after reconnect.
                # It cannot advance checkout and does not reactivate an old command.
                recovery_read = kind == "PAGE_OPENED" and intent["edge_state"] in _PAUSED
                opened_read = (kind == "PAGE_OPENED" and intent["edge_state"] == "OPENED"
                               and command["type"] == "OPEN_PRODUCT" and command["status"] == "DONE")
                if command["status"] not in {"DISPATCHED", "PAUSED"} and not recovery_read and not opened_read:
                    raise ProtocolError("COMMAND_NOT_ACTIVE")
                if command["connection_id"] != state["connection_id"] and not recovery_read:
                    raise ProtocolError("CONNECTION_MISMATCH")
                if intent["edge_tab_id"] is not None and payload.get("tab_id", intent["edge_tab_id"]) != intent["edge_tab_id"]:
                    raise ProtocolError("TAB_IDENTITY_MISMATCH")
                checkpoint = intent["edge_checkpoint"]
                edge_state = intent["edge_state"]
                uncertain = bool(intent["edge_mutation_uncertain"] or payload.get("mutation_uncertain"))
                normal_returned = intent["edge_normal_returned"]
                primary_state = intent["state"]
                cart_identifier = intent["cart_identifier"]
                challenge = payload.get("challenge", state["challenge_status"])
                if kind == "PAGE_OPENED":
                    if not payload.get("url"):
                        raise ProtocolError("PAGE_EVIDENCE_REQUIRED")
                    if payload.get("code") == "CART_REBUILDING":
                        # The worker confirms EMPTY on two fresh documents and
                        # caps rebuilds. Core retains this intent and cart identity.
                        if (command["type"] != "RESUME_INTENT" or edge_state != "RUNNING"
                                or checkpoint != "CART_READY" or primary_state != "CART_READY"
                                or cart_identifier != "configuration_0" or payload.get("challenge") != "NONE"
                                or payload.get("login") == "REQUIRED"):
                            raise ProtocolError("CART_REBUILD_NOT_ALLOWED")
                        checkpoint = "PAGE_OPENED"
                    elif edge_state in _PAUSED:
                        normal_returned = int(payload.get("challenge") == "NONE" and payload.get("login") != "REQUIRED")
                    elif edge_state == "RUNNING" or opened_read:
                        checkpoint = checkpoint or "PAGE_OPENED"
                        if command["type"] == "OPEN_PRODUCT":
                            edge_state = "OPENED"
                            self.connection.execute("UPDATE edge_commands SET status='DONE' WHERE command_id=?", (message["command_id"],))
                    else:
                        raise ProtocolError("EVENT_SEQUENCE_INVALID")
                elif kind == "PRODUCT_VERIFIED":
                    if edge_state != "RUNNING" or checkpoint not in {"PAGE_OPENED", "PRODUCT_VERIFIED"} or payload.get("challenge") != "NONE":
                        raise ProtocolError("EVENT_SEQUENCE_INVALID")
                    checkpoint = "PRODUCT_VERIFIED"
                elif kind == "CART_READY":
                    if edge_state != "RUNNING" or checkpoint not in {"PRODUCT_VERIFIED", "CART_READY"} or payload.get("challenge") != "NONE" or not payload.get("cart_id"):
                        raise ProtocolError("CART_EVIDENCE_REQUIRED")
                    if cart_identifier not in {None, payload["cart_id"]}:
                        raise ProtocolError("CART_IDENTITY_MISMATCH")
                    primary_state, checkpoint, cart_identifier = "CART_READY", "CART_READY", payload["cart_id"]
                    uncertain = False
                elif kind == "CHECKOUT_READY":
                    if (edge_state != "RUNNING" or checkpoint != "CART_READY" or primary_state != "CART_READY"
                            or payload.get("login") != "VALID" or payload.get("challenge") != "NONE"):
                        raise ProtocolError("CHECKOUT_EVIDENCE_REQUIRED")
                    primary_state, checkpoint, edge_state, uncertain = "CHECKOUT_READY", "CHECKOUT_READY", "CHECKOUT_READY", False
                    self.connection.execute("UPDATE edge_commands SET status='DONE' WHERE command_id=?", (message["command_id"],))
                elif kind in {"HUMAN_CHALLENGE_REQUIRED", "LOGIN_REQUIRED"}:
                    if edge_state in _FINISHED:
                        raise ProtocolError("EVENT_SEQUENCE_INVALID")
                    new_pause = edge_state != "WAITING_FOR_HUMAN"
                    edge_state, normal_returned = "WAITING_FOR_HUMAN", 0
                    if kind == "HUMAN_CHALLENGE_REQUIRED":
                        challenge = "REQUIRED"
                    generation = intent["edge_pause_generation"] + int(new_pause)
                    self.connection.execute("UPDATE purchase_intents SET edge_pause_generation=? WHERE intent_id=?", (generation, intent["intent_id"]))
                    self.connection.execute("UPDATE edge_commands SET status='PAUSED' WHERE command_id=?", (message["command_id"],))
                    notice_id = f"edge:{intent['intent_id']}:{generation}"
                    # Claim before SMTP. A crash or uncertain SMTP acceptance never
                    # triggers a second send on reconnect or repeated challenge.
                    if not self.connection.execute("SELECT 1 FROM notifications WHERE event_id=? AND detail=?", (intent["event_id"], notice_id)).fetchone():
                        notification_id = str(uuid4())
                        self.connection.execute("INSERT INTO notifications(id,event_id,status,detail,created_at) VALUES(?,?,'SENDING',?,?)", (notification_id, intent["event_id"], notice_id, now()))
                        result["human_notification"] = {"notification_id": notification_id, "notice_id": notice_id, "status": kind}
                elif kind in {"FAILED", "SITE_CHANGED", "SOLD_OUT"}:
                    cancelled = command["type"] == "CANCEL_INTENT" and kind == "FAILED" and payload.get("code") == "CANCELLED"
                    edge_state, normal_returned = "CANCELLED" if cancelled else kind, 0
                    if cancelled and "cart_id" in payload:
                        uncertain = True  # Preserve unexpected cart evidence across later cancels.
                    self.connection.execute("UPDATE edge_commands SET status=? WHERE command_id=?", ("DONE" if cancelled else "PAUSED", message["command_id"]))
                    if kind in {"FAILED", "SITE_CHANGED"}:
                        from autograb.core.rate_budget import ProviderRateBudget
                        if intent["provider"] == "vmiss" and payload.get("code") == "RATE_LIMITED":
                            ProviderRateBudget.record_browser_block(self.store, "vmiss", "global", "catalog", "RATE_LIMITED")
                        elif intent["provider"] == "apple" and payload.get("code") == "APPLE_BAG_BLOCKED":
                            ProviderRateBudget.record_browser_block(self.store, "apple", intent["product_id"].split(":", 1)[0], "fulfillment", "APPLE_BAG_BLOCKED")
                else:
                    raise ProtocolError("EVENT_SEQUENCE_INVALID")
                self.connection.execute("""UPDATE purchase_intents SET state=?,cart_identifier=?,edge_state=?,
                    edge_checkpoint=?,edge_tab_id=COALESCE(?,edge_tab_id),edge_mutation_uncertain=?,
                    edge_normal_returned=?,edge_error_code=?,updated_at=? WHERE intent_id=?""", (primary_state, cart_identifier,
                    edge_state, checkpoint, payload.get("tab_id"), int(uncertain), normal_returned,
                    payload.get("code", intent["edge_error_code"]) if edge_state in _PAUSED | {"CANCELLED", "SOLD_OUT"} else None,
                    now(), intent["intent_id"]))
                self.connection.execute("""UPDATE edge_transport_state SET last_heartbeat=?,current_tab=COALESCE(?,current_tab),
                    challenge_status=?,execution_state=? WHERE id=1""", (now(), payload.get("tab_id"), challenge, edge_state))
                if (command["type"] == "CANCEL_INTENT" and kind == "FAILED" and payload.get("code") == "CANCELLED"
                        and "cart_id" not in payload):
                    result["read_only_occupancy_released"] = self._release_cancelled_diagnostic(intent["intent_id"])
            self.connection.execute("INSERT INTO edge_messages VALUES(?,?,?,?)", (message["message_id"], message["command_id"], kind, now()))
        return result

    def finish_notification(self, notification_id, result):
        status = result.status if result.status in {"SMTP_ACCEPTED", "NOT_CONFIGURED", "NOTIFICATION_FAILED"} else "NOTIFICATION_FAILED"
        with self.store._transaction():
            self.connection.execute("UPDATE notifications SET status=? WHERE id=? AND status='SENDING'", (status, notification_id))
