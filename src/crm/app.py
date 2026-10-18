"""
Purpose: Customer Care CRM Frontline Agent Console (FastAPI + Jinja2 + Local Bootstrap 5).
Architecture/Context: Standalone visual interface for contact center executives providing subscriber 360 views, device diagnostics, and itemized billing.
                      Human agents see full customer records here (same UI behaviour as HybridAI); it is a
                      separate Cloud Run service (IAM-protected) and is NOT on the AI agent's data path.
Dependencies/Side Effects: Serves local static assets, queries database models, interacts with remote device actions and Loki audit logger.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from src.core.config import settings
from src.crm.display_masking import (
    mask_credit_card,
    mask_email,
    mask_phone,
    mask_ssn,
    mask_street_address,
    mask_subscriber_name,
)
from src.db.database import AsyncSessionLocal, init_db
from src.db.models import (
    Account, BillingRecord, CallInteraction, Customer, Device,
    NetworkOutage, Subscription, SupportTicket, UpsellOffer
)
from src.db.seed_data import seed_synthetic_telecom_data
from src.mcp.tools import run_remote_device_action_tool

logger = logging.getLogger("hybrid_ai.crm")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Summary:
        Lifespan manager ensuring database initialization and synthetic data seeding on startup.
    """
    logger.info("Initializing CRM database schema and synthetic dataset...")
    await init_db()
    await seed_synthetic_telecom_data()
    yield
    logger.info("Shutting down CRM application...")


app = FastAPI(
    title="Telecom Customer Care CRM Console",
    description="Agent console for customer 360, diagnostics, and call handling.",
    version="1.0.0",
    lifespan=lifespan
)

# Mount local static files (Bootstrap 5, Icons, fonts) - Zero external CDN dependency
app.mount("/static", StaticFiles(directory="src/crm/static"), name="static")

templates = Jinja2Templates(directory="src/crm/templates")

# Register enterprise PII masking functions globally for templates
templates.env.globals["mask_phone"] = mask_phone
templates.env.globals["mask_ssn"] = mask_ssn
templates.env.globals["mask_credit_card"] = mask_credit_card
templates.env.globals["mask_email"] = mask_email
templates.env.globals["mask_street_address"] = mask_street_address
templates.env.globals["mask_subscriber_name"] = mask_subscriber_name


@app.get("/health")
async def health():
    """
    Summary:
        Health check endpoint for CRM service.
    """
    return {"status": "healthy", "service": "telecom-crm-console"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Summary:
        Renders the frontline agent dashboard with active customer queue, outages, and KPIs.
    """
    async with AsyncSessionLocal() as session:
        # Load customers
        stmt_cust = select(Customer).options(selectinload(Customer.accounts)).order_by(Customer.created_at.asc())
        cust_res = await session.execute(stmt_cust)
        customers_raw = cust_res.scalars().all()

        customers = [
            {
                "id": c.id,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "email": c.email,
                "phone_masked": mask_phone(c.phone_number),
                "postal_code": c.postal_code,
                "loyalty_tier": c.loyalty_tier
            }
            for c in customers_raw
        ]

        # Load active outages
        stmt_out = select(NetworkOutage).where(NetworkOutage.status.in_(["ACTIVE", "INVESTIGATING"]))
        out_res = await session.execute(stmt_out)
        outages = out_res.scalars().all()

        # Load recent interactions (both AI Assist and Human Frontline Agent records)
        stmt_inter = (
            select(CallInteraction)
            .options(selectinload(CallInteraction.customer))
            .order_by(CallInteraction.timestamp.desc())
            .limit(10)
        )
        inter_res = await session.execute(stmt_inter)
        recent_interactions = inter_res.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "customers": customers,
            "outages": outages,
            "recent_interactions": recent_interactions
        }
    )


@app.get("/search", response_class=HTMLResponse)
async def search_customers(request: Request, q: Optional[str] = None):
    """
    Summary:
        Searches customer records by query parameter and renders matching entries.
    """
    if not q:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    clean_q = f"%{q.strip()}%"
    async with AsyncSessionLocal() as session:
        stmt = (
            select(Customer)
            .options(selectinload(Customer.accounts))
            .where(
                or_(
                    Customer.first_name.ilike(clean_q),
                    Customer.last_name.ilike(clean_q),
                    Customer.phone_number.ilike(clean_q),
                    Customer.email.ilike(clean_q)
                )
            )
        )
        res = await session.execute(stmt)
        matched_cust = res.scalars().all()

        customers = [
            {
                "id": c.id,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "email": c.email,
                "phone_masked": mask_phone(c.phone_number),
                "postal_code": c.postal_code,
                "loyalty_tier": c.loyalty_tier
            }
            for c in matched_cust
        ]

        stmt_out = select(NetworkOutage).where(NetworkOutage.status.in_(["ACTIVE", "INVESTIGATING"]))
        out_res = await session.execute(stmt_out)
        outages = out_res.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "customers": customers,
            "outages": outages,
            "recent_interactions": [],
            "search_query": q
        }
    )


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
async def customer_360(request: Request, customer_id: str):
    """
    Summary:
        Renders the comprehensive Customer 360 console for a single subscriber.
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(Customer)
            .options(
                selectinload(Customer.accounts).selectinload(Account.subscriptions).selectinload(Subscription.devices),
                selectinload(Customer.accounts).selectinload(Account.billing_records),
                selectinload(Customer.tickets),
                selectinload(Customer.interactions)
            )
            .where(Customer.id == customer_id)
        )
        res = await session.execute(stmt)
        customer = res.scalar_one_or_none()

        if not customer:
            raise HTTPException(status_code=404, detail="Customer record not found.")

        # Sort billing records chronologically descending (newest first)
        for acc in customer.accounts:
            acc.billing_records.sort(key=lambda b: b.invoice_month, reverse=True)

        # Sort interactions chronologically descending (newest first)
        if customer.interactions:
            customer.interactions.sort(key=lambda i: i.timestamp or datetime.min, reverse=True)

        # Load upsell offers
        stmt_offers = select(UpsellOffer)
        offers_res = await session.execute(stmt_offers)
        upsell_offers = offers_res.scalars().all()

    phone_masked = mask_phone(customer.phone_number)
    ssn_masked = mask_ssn(customer.ssn)

    return templates.TemplateResponse(
        request=request,
        name="customer_detail.html",
        context={
            "customer": customer,
            "phone_masked": phone_masked,
            "ssn_masked": ssn_masked,
            "upsell_offers": upsell_offers
        }
    )


@app.get("/api/invoices/{billing_id}")
async def get_invoice_details(billing_id: str):
    """
    Summary:
        Returns sanitized, itemized billing invoice information with masked PII
        (phone, address, card, subscriber name).
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(BillingRecord)
            .options(
                selectinload(BillingRecord.account).selectinload(Account.customer),
                selectinload(BillingRecord.account).selectinload(Account.subscriptions)
            )
            .where(BillingRecord.id == billing_id)
        )
        res = await session.execute(stmt)
        bill = res.scalar_one_or_none()
        if not bill:
            raise HTTPException(status_code=404, detail="Invoice record not found.")

        acc = bill.account
        cust = acc.customer
        sub = acc.subscriptions[0] if acc.subscriptions else None

        return {
            "invoice_id": f"INV-{bill.invoice_month}-{acc.account_number[-5:]}",
            "billing_record_id": bill.id,
            "invoice_month": bill.invoice_month,
            "billing_period": f"{bill.invoice_month}-01 to {bill.invoice_month}-28",
            "customer": {
                "subscriber_name": mask_subscriber_name(f"{cust.first_name} {cust.last_name}"),
                "phone_number": mask_phone(cust.phone_number),
                "street_address": f"{mask_street_address(cust.street_address)}, {cust.city}, {cust.state} ({cust.postal_code[:2]}***)",
                "email": mask_email(cust.email)
            },
            "account": {
                "account_number": f"{acc.account_number[:8]}***",
                "payment_card": mask_credit_card(acc.payment_method_card),
                "plan_name": sub.plan_name if sub else "Standard Telecom Plan"
            },
            "charges": {
                "base_charges": bill.base_charges,
                "roaming_charges": bill.roaming_charges,
                "extra_charges": bill.extra_charges,
                "taxes": bill.taxes,
                "total_amount": bill.total_amount
            },
            "payment_status": bill.payment_status,
            "dispute_status": bill.dispute_status,
            "dispute_notes": bill.dispute_notes
        }


@app.post("/devices/{device_id}/action")
async def trigger_device_action(
    device_id: str,
    action: str = Form(...),
    customer_id: str = Form(...)
):
    """
    Summary:
        Triggers an operational command (e.g., remote reboot) on customer equipment.
    """
    await run_remote_device_action_tool(
        device_id=device_id,
        action=action,
        caller_id="crm-agent-console"
    )
    return RedirectResponse(
        url=f"/customers/{customer_id}",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/customers/{customer_id}/log_interaction")
async def log_interaction(
    customer_id: str,
    agent_name: str = Form("Frontline-Agent-4082"),
    interaction_channel: str = Form("Inbound Phone Call"),
    issue_summary: str = Form(...),
    resolution_summary: str = Form(...),
    call_duration_sec: int = Form(120),
    upsell_offered: bool = Form(False),
    upsell_accepted: bool = Form(False)
):
    """
    Summary:
        Saves call notes, resolution, and upsell metrics to the database and streams audit record to Loki.
    """
    full_issue = f"[{interaction_channel}] {issue_summary.strip()}" if interaction_channel else issue_summary.strip()
    async with AsyncSessionLocal() as session:
        inter = CallInteraction(
            customer_id=customer_id,
            agent_name=agent_name,
            issue_summary=full_issue,
            resolution_summary=resolution_summary.strip(),
            call_duration_sec=call_duration_sec,
            upsell_offered=upsell_offered,
            upsell_accepted=upsell_accepted,
            timestamp=datetime.utcnow()
        )
        session.add(inter)
        await session.commit()

    logger.info("CRM call interaction recorded for customer %s by %s", customer_id, agent_name)

    return RedirectResponse(
        url=f"/customers/{customer_id}",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/interactions/quick_log")
async def quick_log_interaction(
    customer_id: str = Form(...),
    agent_name: str = Form("Frontline-Agent-4082"),
    interaction_channel: str = Form("Inbound Phone Call"),
    issue_summary: str = Form(...),
    resolution_summary: str = Form(...),
    call_duration_sec: int = Form(180),
    upsell_offered: bool = Form(False),
    upsell_accepted: bool = Form(False)
):
    """
    Summary:
        Allows contact center agents to quickly record customer calls or escalations
        directly from the dashboard view.
    """
    full_issue = f"[{interaction_channel}] {issue_summary.strip()}" if interaction_channel else issue_summary.strip()
    async with AsyncSessionLocal() as session:
        inter = CallInteraction(
            customer_id=customer_id,
            agent_name=agent_name,
            issue_summary=full_issue,
            resolution_summary=resolution_summary.strip(),
            call_duration_sec=call_duration_sec,
            upsell_offered=upsell_offered,
            upsell_accepted=upsell_accepted,
            timestamp=datetime.utcnow()
        )
        session.add(inter)
        await session.commit()

    logger.info("Quick call interaction logged for customer %s by %s", customer_id, agent_name)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


def run():
    """
    Summary:
        CLI runner for launching the CRM console standalone.
        Dynamically respects the PORT environment variable assigned by Cloud Run or container orchestrators.
        Suppresses repetitive 10-second /health probe lines from uvicorn.access.
    """
    import os
    import uvicorn

    class _ProbeAccessLogFilter(logging.Filter):
        """Filters out repetitive /health 200 OK probe lines from uvicorn.access."""

        def filter(self, record: logging.LogRecord) -> bool:
            return "GET /health " not in record.getMessage()

    logging.getLogger("uvicorn.access").addFilter(_ProbeAccessLogFilter())
    port = int(os.environ.get("PORT", settings.PORT or settings.CRM_PORT))
    uvicorn.run("src.crm.app:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    run()
