"""
Purpose: Telecom domain tool definitions, business logic, and diagnostics for the MCP server.
Architecture/Context: Invoked by the FastMCP JSON-RPC server and CRM controllers to perform customer care
                      actions. Anchored on subscriber Mobile or Landline numbers as primary unique identity
                      tokens. Tools return RAW subscriber data (full names, email, SSN, street address,
                      full payment card number). PII masking is performed downstream by the Apigee proxy
                      (Model Armor + Sensitive Data Protection) before responses reach the AI agent; the
                      MCP server itself is reachable only by the Apigee and Prometheus service accounts
                      (Cloud Run IAM).
Dependencies/Side Effects: Queries database, records Prometheus metrics, streams Loki audit records with
                           FULL RAW request and response payloads (Loki is a restricted PII store).
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from prometheus_client import Counter, Histogram
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from src.core.audit import audit_client
from src.core.security import DEFAULT_AGENT_CALLER, normalize_phone_digits
from src.db.database import AsyncSessionLocal
from src.db.models import (
    Account, BillingRecord, CallInteraction, Customer, Device,
    NetworkOutage, Subscription, SupportTicket, UpsellOffer
)

# Prometheus metrics for observability
TOOL_CALL_COUNTER = Counter(
    "mcp_tool_invocations_total",
    "Total count of MCP tool executions",
    ["tool_name", "status"]
)

TOOL_LATENCY_HISTOGRAM = Histogram(
    "mcp_tool_duration_seconds",
    "Execution duration of MCP tools in seconds",
    ["tool_name"]
)


def _resolve_effective_caller(
    caller_id: str,
    agent_email: Optional[str] = None,
    security_context: Optional[Dict[str, Any]] = None
) -> str:
    """
    Summary:
        Determines the calling entity for audit trails.
        The direct caller is always the AI agent (ADK agent via Agent Gateway and Apigee).

    Parameters:
        caller_id (str): Default caller identifier.
        agent_email (Optional[str]): Explicit user email provided in tool call (if any).
        security_context (Optional[Dict[str, Any]]): Request-level security context.

    Return Value:
        str: Resolved AI Agent caller identity string (DEFAULT_AGENT_CALLER when unknown).

    Exceptions/Errors:
        None.
    """
    if security_context and security_context.get("caller"):
        return str(security_context["caller"]).strip()
    if caller_id and str(caller_id).strip():
        return str(caller_id).strip()
    return DEFAULT_AGENT_CALLER


async def _resolve_customer(
    session,
    phone_number: Optional[str] = None,
    customer_id: Optional[str] = None,
    query: Optional[str] = None,
    load_relations: bool = False
) -> Optional[Customer]:
    """
    Summary:
        Resolves a single customer record prioritizing Mobile or Landline phone number,
        falling back to customer UUID or general search query.

    Parameters:
        session: Active SQLAlchemy async database session.
        phone_number (Optional[str]): Subscriber mobile or landline phone number.
        customer_id (Optional[str]): Customer UUID.
        query (Optional[str]): Fallback query string.
        load_relations (bool): Whether to eagerly load accounts, subscriptions, and devices.

    Return Value:
        Optional[Customer]: Matched Customer model instance or None.
    """
    # 1. Match by Phone Number (Primary Telecom Identity)
    if phone_number and str(phone_number).strip():
        clean_digits = normalize_phone_digits(str(phone_number))
        if clean_digits:
            stmt = select(Customer)
            if load_relations:
                stmt = stmt.options(
                    selectinload(Customer.accounts).selectinload(Account.subscriptions).selectinload(Subscription.devices),
                    selectinload(Customer.accounts).selectinload(Account.billing_records),
                    selectinload(Customer.tickets),
                    selectinload(Customer.interactions)
                )
            stmt = stmt.where(Customer.phone_number.ilike(f"%{clean_digits}%"))
            res = await session.execute(stmt)
            cust = res.scalars().first()
            if cust:
                return cust

    # 2. Match by Customer UUID
    if customer_id and str(customer_id).strip():
        stmt = select(Customer)
        if load_relations:
            stmt = stmt.options(
                selectinload(Customer.accounts).selectinload(Account.subscriptions).selectinload(Subscription.devices),
                selectinload(Customer.accounts).selectinload(Account.billing_records),
                selectinload(Customer.tickets),
                selectinload(Customer.interactions)
            )
        stmt = stmt.where(Customer.id == str(customer_id).strip())
        res = await session.execute(stmt)
        cust = res.scalar_one_or_none()
        if cust:
            return cust

    # 3. Match by General Query (Phone digits, account number, email, or names)
    if query and str(query).strip():
        clean_q = str(query).strip()
        clean_digits = normalize_phone_digits(clean_q)
        stmt = select(Customer)
        if load_relations:
            stmt = stmt.options(
                selectinload(Customer.accounts).selectinload(Account.subscriptions).selectinload(Subscription.devices),
                selectinload(Customer.accounts).selectinload(Account.billing_records),
                selectinload(Customer.tickets),
                selectinload(Customer.interactions)
            )
        else:
            stmt = stmt.options(selectinload(Customer.accounts))

        conditions = [
            Customer.accounts.any(Account.account_number.ilike(f"%{clean_q}%")),
            Customer.email.ilike(f"%{clean_q}%"),
        ]
        if clean_digits and len(clean_digits) >= 4:
            conditions.append(Customer.phone_number.ilike(f"%{clean_digits}%"))
        else:
            conditions.extend([
                Customer.first_name.ilike(f"%{clean_q}%"),
                Customer.last_name.ilike(f"%{clean_q}%")
            ])
        stmt = stmt.where(or_(*conditions))
        res = await session.execute(stmt)
        cust = res.scalars().first()
        if cust:
            return cust

    return None


async def search_customer_tool(
    phone_number: Optional[str] = None,
    query: Optional[str] = None,
    caller_id: str = DEFAULT_AGENT_CALLER,
    agent_email: Optional[str] = None,
    security_context: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Summary:
        Finds subscriber records driven by Mobile or Landline phone number (or account query).
        Returns RAW subscriber data (full name, email) plus the titles of open support tickets.
        PII is masked downstream by Apigee + Model Armor, not here.

    Parameters:
        phone_number (Optional[str]): Subscriber mobile or landline phone number.
        query (Optional[str]): Search keyword, subscriber phone, or account ID.
        caller_id (str): Identity of the calling agent or system.
        agent_email (Optional[str]): Authenticated email of human contact center agent.
        security_context (Optional[Dict[str, Any]]): Security and network context for audit logging.

    Return Value:
        Dict[str, Any]: {'status': 'success', 'count': int, 'customers': [...]} or
        {'status': 'error', 'message': str} on failure.

    Exceptions/Errors:
        None raised; database errors are returned as an error payload and audited.
    """
    start_time = time.time()
    tool_name = "search_customer"
    effective_caller = _resolve_effective_caller(caller_id, agent_email, security_context)

    raw_search = (phone_number or query or "").strip()
    request_data = {
        "phone_number": phone_number,
        "query": query,
        "agent_email": agent_email
    }

    async with AsyncSessionLocal() as session:
        try:
            clean_digits = normalize_phone_digits(raw_search)
            stmt = select(Customer).options(selectinload(Customer.accounts), selectinload(Customer.tickets))

            if clean_digits and len(clean_digits) >= 4:
                stmt = stmt.where(
                    or_(
                        Customer.phone_number.ilike(f"%{clean_digits}%"),
                        Customer.accounts.any(Account.account_number.ilike(f"%{raw_search}%"))
                    )
                )
            else:
                clean_q = f"%{raw_search}%"
                stmt = stmt.where(
                    or_(
                        Customer.phone_number.ilike(clean_q),
                        Customer.accounts.any(Account.account_number.ilike(clean_q)),
                        Customer.first_name.ilike(clean_q),
                        Customer.last_name.ilike(clean_q),
                        Customer.email.ilike(clean_q)
                    )
                )

            result = await session.execute(stmt)
            customers = result.scalars().all()

            results_list = []
            for c in customers:
                account_nums = [acc.account_number for acc in c.accounts]
                # Ticket titles are free text entered by customers/agents; they are returned raw so
                # Model Armor in Apigee can screen them for indirect prompt injection.
                open_ticket_titles = [t.title for t in c.tickets if t.status != "RESOLVED"]
                results_list.append({
                    "customer_id": c.id,
                    "phone_number": c.phone_number,
                    "subscriber_name": f"{c.first_name} {c.last_name}",
                    "email": c.email,
                    "postal_code": c.postal_code,
                    "loyalty_tier": c.loyalty_tier,
                    "accounts": account_nums,
                    "open_ticket_titles": open_ticket_titles
                })

            response_data = {
                "status": "success",
                "count": len(results_list),
                "customers": results_list
            }

            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="SUCCESS").inc()
            duration = time.time() - start_time
            TOOL_LATENCY_HISTOGRAM.labels(tool_name=tool_name).observe(duration)

            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="SUCCESS",
                security_context=security_context,
                request_payload=request_data,
                response_payload=response_data,
                details={"search_target": raw_search, "matches_found": len(results_list)}
            )

            return response_data
        except Exception as exc:
            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="FAILED").inc()
            error_resp = {"status": "error", "message": f"Subscriber lookup failed: {str(exc)}"}
            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="FAILED",
                security_context=security_context,
                request_payload=request_data,
                response_payload=error_resp,
                details={"search_target": raw_search, "error": str(exc)}
            )
            return error_resp


async def get_customer_360_tool(
    phone_number: Optional[str] = None,
    customer_id: Optional[str] = None,
    caller_id: str = DEFAULT_AGENT_CALLER,
    agent_email: Optional[str] = None,
    security_context: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Summary:
        Retrieves a 360-degree consolidated profile of a telecom subscriber.
        Driven by the subscriber's Mobile or Landline number (or customer ID).
        Returns RAW data (full name, email, SSN, street address, full payment card number,
        ticket titles). PII is masked downstream by Apigee + Model Armor, not here.

    Parameters:
        phone_number (Optional[str]): Subscriber mobile or landline phone number.
        customer_id (Optional[str]): Unique customer UUID.
        caller_id (str): Identity of the calling agent.
        agent_email (Optional[str]): Authenticated email of human contact center agent.
        security_context (Optional[Dict[str, Any]]): Security and network context for audit logging.

    Return Value:
        Dict[str, Any]: {'status': 'success', 'customer_360': {...}} or an error payload
        ({'status': 'error', 'message': str}) when not found or on failure.

    Exceptions/Errors:
        None raised; errors are returned as payloads and audited.
    """
    start_time = time.time()
    tool_name = "get_customer_360"
    effective_caller = _resolve_effective_caller(caller_id, agent_email, security_context)
    request_data = {
        "phone_number": phone_number,
        "customer_id": customer_id,
        "agent_email": agent_email
    }

    async with AsyncSessionLocal() as session:
        try:
            c = await _resolve_customer(
                session=session,
                phone_number=phone_number,
                customer_id=customer_id,
                load_relations=True
            )

            if not c:
                TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="NOT_FOUND").inc()
                error_resp = {
                    "status": "error",
                    "message": f"Subscriber with phone or ID '{phone_number or customer_id}' not found."
                }
                await audit_client.log_event(
                    event_type="MCP_TOOL_EXECUTION",
                    caller_identity=effective_caller,
                    tool_name=tool_name,
                    status="NOT_FOUND",
                    security_context=security_context,
                    request_payload=request_data,
                    response_payload=error_resp,
                    details={"lookup_key": phone_number or customer_id}
                )
                return error_resp

            accounts_data = []
            for acc in c.accounts:
                subs_data = []
                for s in acc.subscriptions:
                    devs_data = []
                    for d in s.devices:
                        devs_data.append({
                            "device_id": d.id,
                            "device_type": d.device_type,
                            "serial_number": d.serial_number,
                            "health_status": d.health_status,
                            "live_telemetry": d.live_telemetry,
                            "last_reboot": d.last_reboot_time.isoformat() if d.last_reboot_time else None
                        })
                    subs_data.append({
                        "subscription_id": s.id,
                        "service_type": s.service_type,
                        "plan_name": s.plan_name,
                        "speed_tier_mbps": s.speed_tier_mbps,
                        "data_cap_gb": s.data_cap_gb,
                        "current_usage_gb": s.current_usage_gb,
                        "monthly_fee": s.monthly_fee,
                        "status": s.status,
                        "devices": devs_data
                    })

                bills_data = [
                    {
                        "invoice_month": b.invoice_month,
                        "total_amount": b.total_amount,
                        "roaming_charges": b.roaming_charges,
                        "payment_status": b.payment_status,
                        "dispute_status": b.dispute_status
                    }
                    for b in acc.billing_records
                ]

                accounts_data.append({
                    "account_number": acc.account_number,
                    "status": acc.status,
                    "balance_amount": acc.balance_amount,
                    "payment_card": acc.payment_method_card,
                    "subscriptions": subs_data,
                    "recent_bills": bills_data
                })

            tickets_data = [
                {
                    "ticket_id": t.id,
                    "title": t.title,
                    "category": t.category,
                    "priority": t.priority,
                    "status": t.status,
                    "created_at": t.created_at.isoformat() if t.created_at else None
                }
                for t in c.tickets
            ]

            payload = {
                "customer_id": c.id,
                "phone_number": c.phone_number,
                "subscriber_name": f"{c.first_name} {c.last_name}",
                "email": c.email,
                "ssn": c.ssn,
                "street_address": c.street_address,
                "city": c.city,
                "state": c.state,
                "postal_code": c.postal_code,
                "loyalty_tier": c.loyalty_tier,
                "accounts": accounts_data,
                "open_tickets": tickets_data
            }

            response_data = {"status": "success", "customer_360": payload}

            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="SUCCESS").inc()
            duration = time.time() - start_time
            TOOL_LATENCY_HISTOGRAM.labels(tool_name=tool_name).observe(duration)

            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                customer_id=c.id,
                status="SUCCESS",
                security_context=security_context,
                request_payload=request_data,
                response_payload=response_data,
                details={
                    "accessed_phone": c.phone_number,
                    "subscriber_name": f"{c.first_name} {c.last_name}"
                }
            )

            return response_data
        except Exception as exc:
            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="FAILED").inc()
            error_resp = {"status": "error", "message": f"Failed fetching customer 360: {str(exc)}"}
            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="FAILED",
                security_context=security_context,
                request_payload=request_data,
                response_payload=error_resp,
                details={"error": str(exc)}
            )
            return error_resp


async def get_service_diagnostics_tool(
    phone_number: Optional[str] = None,
    service_id: Optional[str] = None,
    caller_id: str = DEFAULT_AGENT_CALLER,
    agent_email: Optional[str] = None,
    security_context: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Summary:
        Performs remote line diagnostics for a Home Broadband ONT or Mobile 5G connection.
        Can be queried by subscriber phone number or subscription UUID.
        Returns optical dBm signal, packet loss, latency, and automated troubleshooting insights.

    Parameters:
        phone_number (Optional[str]): Subscriber mobile or landline phone number.
        service_id (Optional[str]): Subscription UUID.
        caller_id (str): Identity of the calling agent.
        agent_email (Optional[str]): Authenticated email of human contact center agent.
        security_context (Optional[Dict[str, Any]]): Security and network context for audit logging.

    Return Value:
        Dict[str, Any]: Detailed telemetry and root-cause diagnostic assessment.
    """
    start_time = time.time()
    tool_name = "get_service_diagnostics"
    effective_caller = _resolve_effective_caller(caller_id, agent_email, security_context)
    request_data = {
        "phone_number": phone_number,
        "service_id": service_id,
        "agent_email": agent_email
    }

    async with AsyncSessionLocal() as session:
        try:
            sub = None
            if phone_number and str(phone_number).strip():
                cust = await _resolve_customer(session, phone_number=phone_number, load_relations=True)
                if cust:
                    for acc in cust.accounts:
                        for s in acc.subscriptions:
                            if s.status in ("ACTIVE", "THROTTLED"):
                                sub = s
                                break
                        if sub:
                            break

            if not sub and service_id and str(service_id).strip():
                stmt = (
                    select(Subscription)
                    .options(selectinload(Subscription.devices))
                    .where(Subscription.id == str(service_id).strip())
                )
                result = await session.execute(stmt)
                sub = result.scalar_one_or_none()

            if not sub:
                error_resp = {
                    "status": "error",
                    "message": f"Active service subscription for '{phone_number or service_id}' not found."
                }
                await audit_client.log_event(
                    event_type="MCP_TOOL_EXECUTION",
                    caller_identity=effective_caller,
                    tool_name=tool_name,
                    status="NOT_FOUND",
                    security_context=security_context,
                    request_payload=request_data,
                    response_payload=error_resp,
                    details={"lookup_key": phone_number or service_id}
                )
                return error_resp

            diagnostics = []
            for dev in sub.devices:
                telemetry = dev.live_telemetry or {}
                root_cause = "NORMAL"
                recommended_action = "No intervention needed."

                if dev.device_type == "ONT_FIBER_ROUTER":
                    optical_rx = telemetry.get("optical_rx_power_dbm", -18.0)
                    packet_loss = telemetry.get("packet_loss_percent", 0.0)

                    if optical_rx < -27.0:
                        root_cause = "OPTICAL_SIGNAL_DEGRADATION"
                        recommended_action = "Optical signal degraded (-28.5 dBm). Check fiber patch cable, schedule clean/splice or remote optical calibration."
                    elif packet_loss > 5.0:
                        root_cause = "WIFI_PACKET_LOSS_HIGH"
                        recommended_action = "Perform remote router reboot to clear memory leak and switch Wi-Fi to less congested channel."

                elif dev.device_type in ("5G_SIM", "ESIM"):
                    current_usage = sub.current_usage_gb
                    data_cap = sub.data_cap_gb

                    if data_cap > 0 and current_usage >= data_cap:
                        root_cause = "DATA_CAP_EXCEEDED"
                        recommended_action = f"Subscriber has used {current_usage}GB / {data_cap}GB cap. Speed throttled to 128kbps. Pitch 5G Unlimited Priority Pass."

                diagnostics.append({
                    "device_id": dev.id,
                    "device_type": dev.device_type,
                    "serial_number": dev.serial_number,
                    "health_status": dev.health_status,
                    "telemetry_metrics": telemetry,
                    "root_cause_diagnosis": root_cause,
                    "recommended_action": recommended_action
                })

            response_data = {
                "status": "success",
                "service_id": sub.id,
                "service_type": sub.service_type,
                "plan_name": sub.plan_name,
                "diagnostics": diagnostics
            }

            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="SUCCESS").inc()
            duration = time.time() - start_time
            TOOL_LATENCY_HISTOGRAM.labels(tool_name=tool_name).observe(duration)

            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="SUCCESS",
                security_context=security_context,
                request_payload=request_data,
                response_payload=response_data,
                details={"service_id": sub.id, "service_type": sub.service_type}
            )

            return response_data
        except Exception as exc:
            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="FAILED").inc()
            error_resp = {"status": "error", "message": f"Diagnostics error: {str(exc)}"}
            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="FAILED",
                security_context=security_context,
                request_payload=request_data,
                response_payload=error_resp,
                details={"error": str(exc)}
            )
            return error_resp


async def run_remote_device_action_tool(
    phone_number: Optional[str] = None,
    device_id: Optional[str] = None,
    action: str = "reboot",
    caller_id: str = DEFAULT_AGENT_CALLER,
    agent_email: Optional[str] = None,
    security_context: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Summary:
        Executes a remote operational command on subscriber premise equipment (ONT reboot, Wi-Fi channel reset, Ping).
        Can be triggered directly by the subscriber's phone number or hardware UUID.

    Parameters:
        phone_number (Optional[str]): Subscriber mobile or landline phone number.
        device_id (Optional[str]): UUID of the target hardware device.
        action (str): Action command ('reboot', 'channel_optimization', 'ping_sweep').
        caller_id (str): Calling agent ID.
        agent_email (Optional[str]): Authenticated email of human contact center agent.
        security_context (Optional[Dict[str, Any]]): Security and network context for audit logging.

    Return Value:
        Dict[str, Any]: Execution status, updated telemetry, and completion timestamp.
    """
    start_time = time.time()
    tool_name = "run_remote_device_action"
    effective_caller = _resolve_effective_caller(caller_id, agent_email, security_context)
    request_data = {
        "phone_number": phone_number,
        "device_id": device_id,
        "action": action,
        "agent_email": agent_email
    }

    async with AsyncSessionLocal() as session:
        try:
            device = None
            if phone_number and str(phone_number).strip():
                cust = await _resolve_customer(session, phone_number=phone_number, load_relations=True)
                if cust:
                    for acc in cust.accounts:
                        for s in acc.subscriptions:
                            for d in s.devices:
                                if d.device_type == "ONT_FIBER_ROUTER" or not device:
                                    device = d
                                    break
                            if device:
                                break
                        if device:
                            break

            if not device and device_id and str(device_id).strip():
                stmt = select(Device).where(Device.id == str(device_id).strip())
                result = await session.execute(stmt)
                device = result.scalar_one_or_none()

            if not device:
                error_resp = {
                    "status": "error",
                    "message": f"Customer premise equipment for '{phone_number or device_id}' not found."
                }
                await audit_client.log_event(
                    event_type="PRIVILEGED_DEVICE_ACTION",
                    caller_identity=effective_caller,
                    tool_name=tool_name,
                    status="NOT_FOUND",
                    security_context=security_context,
                    request_payload=request_data,
                    response_payload=error_resp,
                    details={"lookup_key": phone_number or device_id}
                )
                return error_resp

            telemetry = dict(device.live_telemetry or {})

            if action.lower() == "reboot":
                device.last_reboot_time = datetime.utcnow()
                device.health_status = "HEALTHY"
                telemetry["packet_loss_percent"] = 0.05
                telemetry["latency_ms"] = 12.3
                telemetry["uptime_hours"] = 0.1
                telemetry["wifi_interference_score"] = "LOW"
                device.live_telemetry = telemetry
                message = f"Remote reboot initiated on {device.device_type} ({device.serial_number}). Optical link resynced and channel cleared."

            elif action.lower() == "channel_optimization":
                telemetry["wifi_interference_score"] = "OPTIMIZED"
                device.live_telemetry = telemetry
                message = "Wi-Fi channel steering algorithm executed. Migrated to 5GHz DFS low-noise channel."

            else:
                message = f"Executed diagnostic ping to {device.serial_number}: 0% packet loss, 12ms round-trip."

            await session.commit()

            response_data = {
                "status": "success",
                "action": action,
                "message": message,
                "device_id": device.id,
                "updated_health_status": device.health_status,
                "telemetry": telemetry
            }

            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="SUCCESS").inc()
            duration = time.time() - start_time
            TOOL_LATENCY_HISTOGRAM.labels(tool_name=tool_name).observe(duration)

            await audit_client.log_event(
                event_type="PRIVILEGED_DEVICE_ACTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="SUCCESS",
                security_context=security_context,
                request_payload=request_data,
                response_payload=response_data,
                details={"device_id": device.id, "action": action, "serial": device.serial_number}
            )

            return response_data
        except Exception as exc:
            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="FAILED").inc()
            error_resp = {"status": "error", "message": f"Action execution failed: {str(exc)}"}
            await audit_client.log_event(
                event_type="PRIVILEGED_DEVICE_ACTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="FAILED",
                security_context=security_context,
                request_payload=request_data,
                response_payload=error_resp,
                details={"error": str(exc)}
            )
            return error_resp


async def check_network_outages_tool(
    phone_number: Optional[str] = None,
    postal_code: Optional[str] = None,
    caller_id: str = DEFAULT_AGENT_CALLER,
    agent_email: Optional[str] = None,
    security_context: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Summary:
        Checks for active or investigating network infrastructure outages in a postal area.
        Can resolve postal code automatically from the subscriber's phone number.

    Parameters:
        phone_number (Optional[str]): Subscriber mobile or landline phone number.
        postal_code (Optional[str]): Customer ZIP/postal code.
        caller_id (str): Calling agent ID.
        agent_email (Optional[str]): Authenticated email of human contact center agent.
        security_context (Optional[Dict[str, Any]]): Security and network context for audit logging.

    Return Value:
        Dict[str, Any]: List of active infrastructure incidents or clean status.
    """
    start_time = time.time()
    tool_name = "check_network_outages"
    effective_caller = _resolve_effective_caller(caller_id, agent_email, security_context)
    request_data = {
        "phone_number": phone_number,
        "postal_code": postal_code,
        "agent_email": agent_email
    }

    async with AsyncSessionLocal() as session:
        try:
            target_postal = postal_code
            if not target_postal and phone_number and str(phone_number).strip():
                cust = await _resolve_customer(session, phone_number=phone_number)
                if cust:
                    target_postal = cust.postal_code

            if not target_postal:
                target_postal = "N/A"

            stmt = select(NetworkOutage).where(NetworkOutage.status.in_(["ACTIVE", "INVESTIGATING"]))
            result = await session.execute(stmt)
            all_outages = result.scalars().all()

            matching_outages = []
            for out in all_outages:
                if target_postal in out.postal_codes:
                    matching_outages.append({
                        "outage_id": out.id,
                        "region": out.region,
                        "infrastructure_type": out.infrastructure_type,
                        "status": out.status,
                        "description": out.description,
                        "estimated_resolution": out.estimated_resolution
                    })

            response_data = {
                "status": "success",
                "postal_code": target_postal,
                "has_active_outage": len(matching_outages) > 0,
                "outages": matching_outages
            }

            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="SUCCESS").inc()
            duration = time.time() - start_time
            TOOL_LATENCY_HISTOGRAM.labels(tool_name=tool_name).observe(duration)

            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="SUCCESS",
                security_context=security_context,
                request_payload=request_data,
                response_payload=response_data,
                details={"postal_code": target_postal, "outages_count": len(matching_outages)}
            )

            return response_data
        except Exception as exc:
            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="FAILED").inc()
            error_resp = {"status": "error", "message": f"Failed checking outages: {str(exc)}"}
            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="FAILED",
                security_context=security_context,
                request_payload=request_data,
                response_payload=error_resp,
                details={"error": str(exc)}
            )
            return error_resp


async def get_billing_breakdown_tool(
    phone_number: Optional[str] = None,
    account_id: Optional[str] = None,
    caller_id: str = DEFAULT_AGENT_CALLER,
    agent_email: Optional[str] = None,
    security_context: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Summary:
        Retrieves line-item breakdown of recent bills, roaming fees, and dispute notes.
        Can be queried by subscriber phone number or account ID. Returns the RAW payment card
        number and dispute notes; masking happens downstream in Apigee + Model Armor.

    Parameters:
        phone_number (Optional[str]): Subscriber mobile or landline phone number.
        account_id (Optional[str]): Billing account ID or account number.
        caller_id (str): Calling agent ID.
        agent_email (Optional[str]): Authenticated email of human contact center agent.
        security_context (Optional[Dict[str, Any]]): Security and network context for audit logging.

    Return Value:
        Dict[str, Any]: Itemized invoice breakdown, or an error payload when not found/failed.

    Exceptions/Errors:
        None raised; errors are returned as payloads and audited.
    """
    start_time = time.time()
    tool_name = "get_billing_breakdown"
    effective_caller = _resolve_effective_caller(caller_id, agent_email, security_context)
    request_data = {
        "phone_number": phone_number,
        "account_id": account_id,
        "agent_email": agent_email
    }

    async with AsyncSessionLocal() as session:
        try:
            acc = None
            if phone_number and str(phone_number).strip():
                cust = await _resolve_customer(session, phone_number=phone_number, load_relations=True)
                if cust and cust.accounts:
                    acc = cust.accounts[0]

            if not acc and account_id and str(account_id).strip():
                clean_acc = str(account_id).strip()
                stmt = (
                    select(Account)
                    .options(selectinload(Account.billing_records))
                    .where(or_(Account.id == clean_acc, Account.account_number == clean_acc))
                )
                result = await session.execute(stmt)
                acc = result.scalar_one_or_none()

            if not acc:
                error_resp = {
                    "status": "error",
                    "message": f"Billing account for '{phone_number or account_id}' not found."
                }
                await audit_client.log_event(
                    event_type="MCP_TOOL_EXECUTION",
                    caller_identity=effective_caller,
                    tool_name=tool_name,
                    status="NOT_FOUND",
                    security_context=security_context,
                    request_payload=request_data,
                    response_payload=error_resp,
                    details={"lookup_key": phone_number or account_id}
                )
                return error_resp

            records = [
                {
                    "invoice_month": b.invoice_month,
                    "base_charges": b.base_charges,
                    "roaming_charges": b.roaming_charges,
                    "extra_charges": b.extra_charges,
                    "taxes": b.taxes,
                    "total_amount": b.total_amount,
                    "payment_status": b.payment_status,
                    "dispute_status": b.dispute_status,
                    "dispute_notes": b.dispute_notes
                }
                for b in acc.billing_records
            ]

            response_data = {
                "status": "success",
                "account_number": acc.account_number,
                "current_balance": acc.balance_amount,
                "payment_card": acc.payment_method_card,
                "invoices": records
            }

            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="SUCCESS").inc()
            duration = time.time() - start_time
            TOOL_LATENCY_HISTOGRAM.labels(tool_name=tool_name).observe(duration)

            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="SUCCESS",
                security_context=security_context,
                request_payload=request_data,
                response_payload=response_data,
                details={"account_number": acc.account_number}
            )

            return response_data
        except Exception as exc:
            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="FAILED").inc()
            error_resp = {"status": "error", "message": f"Billing lookup error: {str(exc)}"}
            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="FAILED",
                security_context=security_context,
                request_payload=request_data,
                response_payload=error_resp,
                details={"error": str(exc)}
            )
            return error_resp


async def get_upsell_recommendations_tool(
    phone_number: Optional[str] = None,
    customer_id: Optional[str] = None,
    caller_id: str = DEFAULT_AGENT_CALLER,
    agent_email: Optional[str] = None,
    security_context: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Summary:
        Analyzes subscriber usage metrics to compute personalized upsell offers and pitch scripts.
        Identified by subscriber phone number or UUID. Returns the RAW subscriber name and phone number.

    Parameters:
        phone_number (Optional[str]): Subscriber mobile or landline phone number.
        customer_id (Optional[str]): Customer UUID.
        caller_id (str): Calling agent ID.
        agent_email (Optional[str]): Authenticated email of human contact center agent.
        security_context (Optional[Dict[str, Any]]): Security and network context for audit logging.

    Return Value:
        Dict[str, Any]: Targeted promotional upgrades with pitch scripts.
    """
    start_time = time.time()
    tool_name = "get_upsell_recommendations"
    effective_caller = _resolve_effective_caller(caller_id, agent_email, security_context)
    request_data = {
        "phone_number": phone_number,
        "customer_id": customer_id,
        "agent_email": agent_email
    }

    async with AsyncSessionLocal() as session:
        try:
            cust = await _resolve_customer(session, phone_number=phone_number, customer_id=customer_id, load_relations=True)

            if not cust:
                error_resp = {
                    "status": "error",
                    "message": f"Subscriber '{phone_number or customer_id}' not found."
                }
                await audit_client.log_event(
                    event_type="MCP_TOOL_EXECUTION",
                    caller_identity=effective_caller,
                    tool_name=tool_name,
                    status="NOT_FOUND",
                    security_context=security_context,
                    request_payload=request_data,
                    response_payload=error_resp,
                    details={"lookup_key": phone_number or customer_id}
                )
                return error_resp

            offers_stmt = select(UpsellOffer)
            offers_res = await session.execute(offers_stmt)
            catalog_offers = offers_res.scalars().all()

            recommendations = []
            for acc in cust.accounts:
                for sub in acc.subscriptions:
                    if sub.service_type == "HOME_BROADBAND" and sub.speed_tier_mbps < 1000:
                        for off in catalog_offers:
                            if off.title.startswith("Gigabit"):
                                recommendations.append({
                                    "title": off.title,
                                    "description": off.description,
                                    "promotional_price": off.promotional_price,
                                    "agent_pitch_script": off.pitch_script,
                                    "reason": f"Subscriber is on {sub.speed_tier_mbps}Mbps plan with high network utilization."
                                })

                    if sub.service_type == "MOBILE_5G":
                        for off in catalog_offers:
                            if "Unlimited 5G Priority" in off.title and sub.status == "THROTTLED":
                                recommendations.append({
                                    "title": off.title,
                                    "description": off.description,
                                    "promotional_price": off.promotional_price,
                                    "agent_pitch_script": off.pitch_script,
                                    "reason": "Subscriber exceeded data cap and is actively throttled."
                                })

            response_data = {
                "status": "success",
                "phone_number": cust.phone_number,
                "subscriber_name": f"{cust.first_name} {cust.last_name}",
                "recommendations": recommendations
            }

            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="SUCCESS").inc()
            duration = time.time() - start_time
            TOOL_LATENCY_HISTOGRAM.labels(tool_name=tool_name).observe(duration)

            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                customer_id=cust.id,
                status="SUCCESS",
                security_context=security_context,
                request_payload=request_data,
                response_payload=response_data,
                details={"recommendations_count": len(recommendations)}
            )

            return response_data
        except Exception as exc:
            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="FAILED").inc()
            error_resp = {"status": "error", "message": f"Upsell engine error: {str(exc)}"}
            await audit_client.log_event(
                event_type="MCP_TOOL_EXECUTION",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="FAILED",
                security_context=security_context,
                request_payload=request_data,
                response_payload=error_resp,
                details={"error": str(exc)}
            )
            return error_resp


async def log_agent_interaction_tool(
    phone_number: Optional[str] = None,
    customer_id: Optional[str] = None,
    agent_name: str = "Contact Center Agent",
    issue_summary: str = "",
    resolution_summary: str = "",
    call_duration_sec: int = 180,
    upsell_offered: bool = False,
    upsell_accepted: bool = False,
    caller_id: str = DEFAULT_AGENT_CALLER,
    agent_email: Optional[str] = None,
    security_context: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Summary:
        Records contact center agent call notes and upsell outcomes directly into the CRM database.
        Identified by subscriber phone number or customer UUID. Commits structured audit event to Loki.

    Parameters:
        phone_number (Optional[str]): Subscriber mobile or landline phone number.
        customer_id (Optional[str]): Customer UUID.
        agent_name (str): Name or ID of the human agent.
        issue_summary (str): Brief summary of reason for call.
        resolution_summary (str): Action taken and resolution.
        call_duration_sec (int): Total duration of call in seconds.
        upsell_offered (bool): Whether an upsell was proposed.
        upsell_accepted (bool): Whether customer agreed to upgrade.
        caller_id (str): Calling agent ID.
        agent_email (Optional[str]): Authenticated email of human contact center agent.
        security_context (Optional[Dict[str, Any]]): Security and network context for audit logging.

    Return Value:
        Dict[str, Any]: Recorded interaction ID and status message.
    """
    start_time = time.time()
    tool_name = "log_agent_interaction"
    effective_caller = _resolve_effective_caller(caller_id, agent_email or agent_name, security_context)
    request_data = {
        "phone_number": phone_number,
        "customer_id": customer_id,
        "agent_name": agent_name,
        "issue_summary": issue_summary,
        "resolution_summary": resolution_summary,
        "call_duration_sec": call_duration_sec,
        "upsell_offered": upsell_offered,
        "upsell_accepted": upsell_accepted,
        "agent_email": agent_email
    }

    async with AsyncSessionLocal() as session:
        try:
            target_cust = None
            if phone_number or customer_id:
                target_cust = await _resolve_customer(
                    session,
                    phone_number=phone_number,
                    customer_id=customer_id,
                    query=customer_id if (customer_id and not phone_number) else None
                )

            if not target_cust:
                error_resp = {
                    "status": "error",
                    "message": f"Cannot log interaction: subscriber '{phone_number or customer_id}' not found."
                }
                await audit_client.log_event(
                    event_type="CALL_INTERACTION_RECORDED",
                    caller_identity=effective_caller,
                    tool_name=tool_name,
                    status="NOT_FOUND",
                    security_context=security_context,
                    request_payload=request_data,
                    response_payload=error_resp,
                    details={"lookup_key": phone_number or customer_id}
                )
                return error_resp

            target_cust_id = target_cust.id

            # Defensive guard: if agent_name was set to the customer's name, fall back to agent_email or default
            clean_agent_name = agent_name
            cust_full_name = f"{target_cust.first_name} {target_cust.last_name}".strip()
            if not clean_agent_name or clean_agent_name in (cust_full_name, target_cust.first_name, target_cust.last_name):
                clean_agent_name = agent_email or (effective_caller if effective_caller != DEFAULT_AGENT_CALLER else "Contact Center Agent")

            interaction = CallInteraction(
                customer_id=target_cust_id,
                agent_name=clean_agent_name,
                call_duration_sec=call_duration_sec,
                issue_summary=issue_summary,
                resolution_summary=resolution_summary,
                upsell_offered=upsell_offered,
                upsell_accepted=upsell_accepted
            )
            session.add(interaction)
            await session.commit()

            response_data = {
                "status": "success",
                "interaction_id": interaction.id,
                "message": "Call interaction logged successfully in CRM database."
            }

            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="SUCCESS").inc()
            duration = time.time() - start_time
            TOOL_LATENCY_HISTOGRAM.labels(tool_name=tool_name).observe(duration)

            await audit_client.log_event(
                event_type="CALL_INTERACTION_RECORDED",
                caller_identity=effective_caller,
                tool_name=tool_name,
                customer_id=target_cust_id,
                status="SUCCESS",
                security_context=security_context,
                request_payload=request_data,
                response_payload=response_data,
                details={
                    "interaction_id": interaction.id,
                    "customer_id": target_cust_id,
                    "agent": clean_agent_name,
                    "duration_sec": call_duration_sec,
                    "upsell_accepted": upsell_accepted
                }
            )

            return response_data
        except Exception as exc:
            TOOL_CALL_COUNTER.labels(tool_name=tool_name, status="FAILED").inc()
            error_resp = {"status": "error", "message": f"Failed logging interaction: {str(exc)}"}
            await audit_client.log_event(
                event_type="CALL_INTERACTION_RECORDED",
                caller_identity=effective_caller,
                tool_name=tool_name,
                status="FAILED",
                security_context=security_context,
                request_payload=request_data,
                response_payload=error_resp,
                details={"error": str(exc)}
            )
            return error_resp
