"""
Purpose: Relational database schema models for the Telecom Customer Care platform.
Architecture/Context: Defines SQLAlchemy ORM entities representing customers, subscriptions, devices, telemetry, and billing.
Dependencies/Side Effects: Interacts with PostgreSQL or SQLite backing engines via SQLAlchemy.
"""

import uuid
from datetime import datetime
from typing import Any, Dict
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, JSON
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Customer(Base):
    """
    Summary:
        Represents a telecom subscriber / customer entity.
    """
    __tablename__ = "customers"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    phone_number = Column(String(50), unique=True, nullable=False, index=True)
    ssn = Column(String(50), nullable=False)
    street_address = Column(String(255), nullable=False)
    city = Column(String(100), nullable=False)
    state = Column(String(50), nullable=False)
    postal_code = Column(String(20), nullable=False, index=True)
    loyalty_tier = Column(String(50), default="Standard")  # Standard, Gold, Platinum
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    accounts = relationship("Account", back_populates="customer", cascade="all, delete-orphan")
    tickets = relationship("SupportTicket", back_populates="customer", cascade="all, delete-orphan")
    interactions = relationship("CallInteraction", back_populates="customer", cascade="all, delete-orphan")


class Account(Base):
    """
    Summary:
        Represents a billing account owned by a customer.
    """
    __tablename__ = "accounts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id = Column(String(36), ForeignKey("customers.id"), nullable=False, index=True)
    account_number = Column(String(50), unique=True, nullable=False, index=True)
    status = Column(String(50), default="ACTIVE")  # ACTIVE, DELINQUENT, CLOSED
    balance_amount = Column(Float, default=0.0)
    payment_method_card = Column(String(50), nullable=False)
    billing_cycle_day = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="accounts")
    subscriptions = relationship("Subscription", back_populates="account", cascade="all, delete-orphan")
    billing_records = relationship("BillingRecord", back_populates="account", cascade="all, delete-orphan")


class Subscription(Base):
    """
    Summary:
        Represents an active or provisioned telecom service line (Broadband or Mobile).
    """
    __tablename__ = "subscriptions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id = Column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    service_type = Column(String(50), nullable=False)  # HOME_BROADBAND, MOBILE_5G
    plan_name = Column(String(150), nullable=False)
    speed_tier_mbps = Column(Integer, default=100)
    data_cap_gb = Column(Float, default=0.0)  # 0.0 represents unlimited
    current_usage_gb = Column(Float, default=0.0)
    monthly_fee = Column(Float, nullable=False)
    status = Column(String(50), default="ACTIVE")  # ACTIVE, THROTTLED, SUSPENDED
    created_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("Account", back_populates="subscriptions")
    devices = relationship("Device", back_populates="subscription", cascade="all, delete-orphan")


class Device(Base):
    """
    Summary:
        Hardware endpoint tied to a subscription (e.g. ONT Fiber Gateway, Wi-Fi 6 Router, 5G SIM).
    """
    __tablename__ = "devices"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    subscription_id = Column(String(36), ForeignKey("subscriptions.id"), nullable=False, index=True)
    device_type = Column(String(50), nullable=False)  # ONT_FIBER_ROUTER, 5G_SIM, ESIM
    serial_number = Column(String(100), unique=True, nullable=False, index=True)
    mac_address = Column(String(50), nullable=True)
    imei = Column(String(50), nullable=True)
    firmware_version = Column(String(50), default="v3.14.2")
    health_status = Column(String(50), default="HEALTHY")  # HEALTHY, DEGRADED, DOWN
    live_telemetry = Column(JSON, default=dict)
    last_reboot_time = Column(DateTime, default=datetime.utcnow)

    subscription = relationship("Subscription", back_populates="devices")


class NetworkOutage(Base):
    """
    Summary:
        Regional infrastructure incidents affecting broadband loops or cell towers.
    """
    __tablename__ = "network_outages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    region = Column(String(100), nullable=False)
    postal_codes = Column(String(255), nullable=False, index=True)
    infrastructure_type = Column(String(50), nullable=False)  # FIBER_BACKBONE, CELL_TOWER_5G
    status = Column(String(50), default="ACTIVE")  # ACTIVE, INVESTIGATING, RESOLVED
    description = Column(Text, nullable=False)
    estimated_resolution = Column(String(100), default="Within 2 hours")
    created_at = Column(DateTime, default=datetime.utcnow)


class BillingRecord(Base):
    """
    Summary:
        Detailed monthly invoice and itemized charge breakdown.
    """
    __tablename__ = "billing_records"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id = Column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    invoice_month = Column(String(20), nullable=False)
    base_charges = Column(Float, nullable=False)
    roaming_charges = Column(Float, default=0.0)
    extra_charges = Column(Float, default=0.0)
    taxes = Column(Float, default=0.0)
    total_amount = Column(Float, nullable=False)
    payment_status = Column(String(50), default="PAID")
    dispute_status = Column(String(50), default="NONE")  # NONE, UNDER_REVIEW, CREDITED
    dispute_notes = Column(Text, nullable=True)

    account = relationship("Account", back_populates="billing_records")


class SupportTicket(Base):
    """
    Summary:
        Customer support ticket logged in the CRM.
    """
    __tablename__ = "support_tickets"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id = Column(String(36), ForeignKey("customers.id"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    category = Column(String(100), nullable=False)  # BROADBAND, MOBILE, BILLING, HARDWARE
    priority = Column(String(50), default="MEDIUM")  # LOW, MEDIUM, HIGH, URGENT
    status = Column(String(50), default="OPEN")  # OPEN, IN_PROGRESS, RESOLVED
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="tickets")


class CallInteraction(Base):
    """
    Summary:
        Contact center agent interaction audit log.
    """
    __tablename__ = "call_interactions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_id = Column(String(36), ForeignKey("customers.id"), nullable=False, index=True)
    agent_name = Column(String(100), default="Agent-Frontline")
    call_duration_sec = Column(Integer, default=0)
    issue_summary = Column(Text, nullable=False)
    resolution_summary = Column(Text, nullable=False)
    upsell_offered = Column(Boolean, default=False)
    upsell_accepted = Column(Boolean, default=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="interactions")


class UpsellOffer(Base):
    """
    Summary:
        Marketing and speed upgrade catalog offers.
    """
    __tablename__ = "upsell_offers"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    target_service_type = Column(String(50), nullable=False)  # HOME_BROADBAND, MOBILE_5G
    title = Column(String(150), nullable=False)
    description = Column(Text, nullable=False)
    upgrade_tier = Column(String(100), nullable=False)
    promotional_price = Column(Float, nullable=False)
    pitch_script = Column(Text, nullable=False)
