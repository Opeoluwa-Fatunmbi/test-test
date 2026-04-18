import asyncio
import enum
import uuid
import time
from dataclasses import dataclass, asdict

"""
main.py

Minimal, self-contained Python simulation showing the core ideas of OCPP (Open Charge Point Protocol).
This is NOT a full OCPP implementation. It is an algorithmic example to learn how messages and flows
typically work between a Charge Point (EVSE) and a Central System (CSMS).

Run with: python main.py
Requires: Python 3.7+
"""


# --- Basic OCPP concepts ----------------------------------------------------
# OCPP is message-based and usually runs over WebSockets. Messages are arrays:
# [MessageTypeId, UniqueId, Action, Payload]
#
# MessageTypeId:
# 2 = CALL       (request from one side)
# 3 = CALLRESULT (success response)
# 4 = CALLERROR  (error response)
#
# Actions are strings like "BootNotification", "Authorize", "StartTransaction", "MeterValues".
# The "Charge Point" (CP) typically sends CALL messages like BootNotification/Authorize/StartTransaction,
# and the Central System typically replies with CALLRESULT. The Central System can also CALL the CP
# (e.g., RemoteStartTransaction) in more advanced scenarios.
# ----------------------------------------------------------------------------

class MessageType(enum.IntEnum):
    CALL = 2
    CALLRESULT = 3
    CALLERROR = 4

def new_message_id() -> str:
    return uuid.uuid4().hex[:8]

def make_call(action: str, payload: dict) -> list:
    return [MessageType.CALL, new_message_id(), action, payload]

def make_callresult(request_id: str, payload: dict) -> list:
    return [MessageType.CALLRESULT, request_id, payload]

def make_callerror(request_id: str, error_code: str, error_description: str, details: dict=None) -> list:
    return [MessageType.CALLERROR, request_id, error_code, {"description": error_description, "details": details or {}}]

# --- Simple in-memory transport (simulates a WebSocket) ---------------------
# We use asyncio.Queues to simulate message passing in both directions.
# ----------------------------------------------------------------------------

@dataclass
class Transport:
    to_central: asyncio.Queue
    to_chargepoint: asyncio.Queue

    async def cp_send(self, msg):
        await self.to_central.put(msg)

    async def cs_send(self, msg):
        await self.to_chargepoint.put(msg)

# --- Central System (CSMS) -------------------------------------------------
# Handles incoming CALLs from Charge Point and replies. Implements basic
# logic for BootNotification, Authorize, Start/StopTransaction, and MeterValues.
# ----------------------------------------------------------------------------

class CentralSystem:
    def __init__(self, transport: Transport):
        self.transport = transport
        self.authorized_tokens = {"CARD123": True}  # simple token store
        self.active_transactions = {}  # transaction_id -> info

    async def run(self):
        while True:
            msg = await self.transport.to_central.get()
            await self.handle_message(msg)

    async def handle_message(self, msg):
        # Expect the OCPP array-like message
        mtype = MessageType(msg[0])
        if mtype == MessageType.CALL:
            req_id = msg[1]
            action = msg[2]
            payload = msg[3]
            handler = getattr(self, f"on_{action}", None)
            if handler:
                try:
                    result = await handler(payload)
                    # Successful CALLRESULT uses the same request id
                    response = make_callresult(req_id, result)
                    await self.transport.cs_send(response)
                except Exception as e:
                    response = make_callerror(req_id, "InternalError", str(e))
                    await self.transport.cs_send(response)
            else:
                response = make_callerror(req_id, "NotSupported", f"Action {action} not supported")
                await self.transport.cs_send(response)
        else:
            # For learning, just print received CALLRESULT/CALLERROR
            print("CentralSystem received non-CALL message:", msg)

    # Handlers for OCPP actions (very simplified):
    async def on_BootNotification(self, payload):
        # CP tells CSMS it's online and gives vendor/model info.
        print(f"CSMS: BootNotification from {payload.get('chargePointModel')} (serial {payload.get('chargePointSerialNumber')})")
        # Reply with accepted and heartbeat interval
        return {"status": "Accepted", "currentTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "heartbeatInterval": 10}

    async def on_Authorize(self, payload):
        id_tag = payload.get("idTag")
        allowed = self.authorized_tokens.get(id_tag, False)
        print(f"CSMS: Authorize request for idTag={id_tag} -> {'Accepted' if allowed else 'Rejected'}")
        return {"idTagInfo": {"status": "Accepted" if allowed else "Blocked"}}

    async def on_StartTransaction(self, payload):
        transaction_id = str(len(self.active_transactions) + 1)
        self.active_transactions[transaction_id] = payload
        print(f"CSMS: StartTransaction -> assigned transactionId={transaction_id}")
        return {"transactionId": int(transaction_id), "idTagInfo": {"status": "Accepted"}}

    async def on_MeterValues(self, payload):
        tx_id = payload.get("transactionId")
        meter = payload.get("meterValue")
        print(f"CSMS: MeterValues for tx={tx_id} -> {meter}")
        return {}  # empty payload is allowed

    async def on_StopTransaction(self, payload):
        tx_id = payload.get("transactionId")
        reason = payload.get("reason")
        self.active_transactions.pop(str(tx_id), None)
        print(f"CSMS: StopTransaction tx={tx_id} reason={reason}")
        return {"idTagInfo": {"status": "Accepted"}}

# --- Charge Point (CP) -----------------------------------------------------
# Simulates a charge station that sends BootNotification, Authorize, Start/StopTransaction,
# and periodic MeterValues. It waits for CALLRESULT replies from the Central System.
# ----------------------------------------------------------------------------

class ChargePoint:
    def __init__(self, transport: Transport, cp_id="CP_001"):
        self.transport = transport
        self.cp_id = cp_id
        self.heartbeat_interval = 10  # will be updated by BootNotification reply
        self.running = True
        self.transaction_id = None

    async def run(self):
        # Start a receiver task to process incoming CALLRESULT/CALLERRORs
        asyncio.create_task(self.receiver())
        # Boot sequence
        await self.boot_notification()
        # Periodic heartbeat loop and demo of a single transaction
        await asyncio.gather(self.heartbeat_loop(), self.demo_transaction_flow())

    async def receiver(self):
        while True:
            msg = await self.transport.to_chargepoint.get()
            mtype = MessageType(msg[0])
            if mtype == MessageType.CALLRESULT:
                req_id = msg[1]
                payload = msg[2]
                print(f"ChargePoint received CALLRESULT for id={req_id} -> {payload}")
                # Special handling for BootNotification result to capture heartbeatInterval
                # In this simulation we don't map req_id->pending futures; we infer by action in order.
                # A real implementation matches request IDs to awaiting coroutines.
                if "heartbeatInterval" in payload:
                    self.heartbeat_interval = payload["heartbeatInterval"]
            elif mtype == MessageType.CALLERROR:
                print("ChargePoint received CALLERROR:", msg)
            else:
                print("ChargePoint received unexpected:", msg)

    async def send_and_wait(self, message):
        # Simplified: send message and do not block for matching response.
        # In real OCPP you must correlate request IDs with incoming CALLRESULTs.
        await self.transport.cp_send(message)

    async def boot_notification(self):
        payload = {
            "chargePointModel": "SimCP",
            "chargePointSerialNumber": "SN123456",
            "chargeBoxSerialNumber": "BOX123",
        }
        msg = make_call("BootNotification", payload)
        print("ChargePoint: sending BootNotification")
        await self.send_and_wait(msg)
        # In this simulation, we rely on receiver to update heartbeatInterval.

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            payload = {}
            msg = make_call("Heartbeat", payload)
            print("ChargePoint: sending Heartbeat")
            await self.send_and_wait(msg)

    async def demo_transaction_flow(self):
        # Wait a bit for boot to complete
        await asyncio.sleep(1)
        # 1) Authorize an id tag (user presents RFID)
        id_tag = "CARD123"
        print(f"ChargePoint: sending Authorize for idTag={id_tag}")
        await self.send_and_wait(make_call("Authorize", {"idTag": id_tag}))
        await asyncio.sleep(0.5)

        # 2) StartTransaction when EV plugged in and meter starts at 0
        meter_start = 0
        payload_start = {"connectorId": 1, "idTag": id_tag, "meterStart": meter_start, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        print("ChargePoint: sending StartTransaction")
        await self.send_and_wait(make_call("StartTransaction", payload_start))

        # For simulation assume Central returned transactionId=1 (see CentralSystem)
        self.transaction_id = 1
        # 3) Periodically send MeterValues while charging
        for i in range(3):
            await asyncio.sleep(1)
            meter = meter_start + (i + 1) * 5  # kWh or Wh depending on implementation
            payload_meter = {"connectorId": 1, "transactionId": self.transaction_id, "meterValue": {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "value": meter}}
            print(f"ChargePoint: sending MeterValues -> {meter}")
            await self.send_and_wait(make_call("MeterValues", payload_meter))

        # 4) StopTransaction when EV unplugged
        payload_stop = {"transactionId": self.transaction_id, "meterStop": meter, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "reason": "Local"}
        print("ChargePoint: sending StopTransaction")
        await self.send_and_wait(make_call("StopTransaction", payload_stop))

        # End demo after a short wait
        await asyncio.sleep(1)
        print("ChargePoint: demo transaction completed")
        # stop program
        await asyncio.sleep(0.1)
        asyncio.get_event_loop().stop()

# --- Main: wire transport, start CP and CS ---------------------------------

async def main():
    q1 = asyncio.Queue()
    q2 = asyncio.Queue()
    transport = Transport(to_central=q1, to_chargepoint=q2)

    cs = CentralSystem(transport)
    cp = ChargePoint(transport)

    # Run both concurrently
    await asyncio.gather(cs.run(), cp.run())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped by user")