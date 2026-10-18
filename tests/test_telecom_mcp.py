"""
Purpose: Automated test suite for the HybridMCP-Apigee MCP server: RAW (unmasked) tool outputs,
         IAM-only auth model (no static token), idempotent/locked seeding, SECURITY-TEST persona,
         DB fallback flag behaviour, and Loki Google ID-token authentication.
Architecture/Context: Executed locally or in CI against an isolated SQLite database file. In the real
                      deployment PII masking happens in Apigee + Model Armor, so these tests assert
                      that the MCP server returns raw values.
Dependencies/Side Effects: Creates/deletes ./test_telecom.db and a temporary fallback SQLite file.
                           Loki shipping is disabled via LOKI_URL='' except in tests that inject an
                           httpx.MockTransport (no network access is required).
"""

import asyncio
from datetime import datetime, timezone
import json
import os
import tempfile
import unittest
from unittest import mock

# Environment must be configured BEFORE importing any src.* module (settings are read at import).
TEST_DB_FILE = "./test_telecom.db"
if os.path.exists(TEST_DB_FILE):
    os.remove(TEST_DB_FILE)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_FILE}"
os.environ["DB_ALLOW_SQLITE_FALLBACK"] = "true"
os.environ["LOKI_URL"] = ""
os.environ["LOKI_AUTH_MODE"] = "none"
os.environ["DEFAULT_END_USER_EMAIL"] = "fallback-user@hybridmcp.invalid"
os.environ.pop("MCP_AUTH_TOKEN", None)

import httpx  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from src.core import security  # noqa: E402
from src.core.audit import LokiAuditClient, sanitize_audit_headers  # noqa: E402
from src.core.config import Settings, settings  # noqa: E402
from src.core.security import (  # noqa: E402
    DEFAULT_AGENT_CALLER, extract_caller_identity, extract_security_context, normalize_phone_digits
)
from src.db import database, seed_data  # noqa: E402
from src.db.database import AsyncSessionLocal, init_db, resolve_database_url  # noqa: E402
from src.db.demo_baselines import DEMO_DEVICE_BASELINES, device_baseline_kwargs  # noqa: E402
from src.db.models import BillingRecord, CallInteraction, Customer, Device, SupportTicket  # noqa: E402
from src.db.seed_data import (  # noqa: E402
    SECURITY_TEST_ACCOUNT, SECURITY_TEST_INJECTION_PAYLOAD, seed_synthetic_telecom_data
)
from src.mcp.tools import (  # noqa: E402
    check_network_outages_tool, get_billing_breakdown_tool, get_customer_360_tool,
    get_service_diagnostics_tool, get_upsell_recommendations_tool, run_remote_device_action_tool,
    search_customer_tool
)

ELENA_PHONE = "+1 (555) 234-5678"
MALLORY_PHONE = "+1 (555) 000-1337"


def _fake_jwt(claims: dict) -> str:
    """
    Summary:
        Builds an unsigned JWT-shaped string for identity-extraction tests.

    Parameters:
        claims (dict): Payload claims.

    Return Value:
        str: 'header.payload.signature' token.

    Exceptions/Errors:
        None.
    """
    import base64

    def enc(obj: dict) -> str:
        """
        Summary:
            base64url-encodes a JSON object without padding.

        Parameters:
            obj (dict): Object to encode.

        Return Value:
            str: Encoded segment.

        Exceptions/Errors:
            None.
        """
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'RS256', 'typ': 'JWT'})}.{enc(claims)}.c2lnbmF0dXJl"


def setUpModule() -> None:
    """
    Summary:
        Creates the schema and seeds the test database once for the whole module.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        Any database error aborts the test run.
    """
    asyncio.run(_bootstrap())


async def _bootstrap() -> None:
    """
    Summary:
        Async helper for setUpModule: init schema, seed, and dispose pooled connections so the
        next event loop starts clean.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        Propagates database errors.
    """
    await init_db()
    await seed_synthetic_telecom_data()
    await database.engine.dispose()


def tearDownModule() -> None:
    """
    Summary:
        Removes the SQLite test database file.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        None.
    """
    asyncio.run(database.engine.dispose())
    if os.path.exists(TEST_DB_FILE):
        os.remove(TEST_DB_FILE)


class TestConfig(unittest.TestCase):
    """
    Summary:
        Validates the configuration contract (no static MCP token, validated LOKI_AUTH_MODE).
    """

    def test_static_token_setting_removed(self):
        """Summary: MCP_AUTH_TOKEN no longer exists; Cloud Run IAM is the only gate."""
        self.assertFalse(hasattr(settings, "MCP_AUTH_TOKEN"))

    def test_new_settings_defaults(self):
        """Summary: New keys exist with the documented defaults / test overrides."""
        self.assertTrue(settings.DB_ALLOW_SQLITE_FALLBACK)
        self.assertEqual(settings.LOKI_AUTH_MODE, "none")
        self.assertEqual(settings.SERVICE_NAME, "telecom-mcp-server")
        self.assertEqual(settings.DEFAULT_END_USER_EMAIL, "fallback-user@hybridmcp.invalid")

    def test_loki_auth_mode_validation(self):
        """Summary: Unsupported LOKI_AUTH_MODE values are rejected; valid ones are normalized."""
        with self.assertRaises(ValidationError):
            Settings(LOKI_AUTH_MODE="basic")
        self.assertEqual(Settings(LOKI_AUTH_MODE="GOOGLE_ID_TOKEN").LOKI_AUTH_MODE, "google_id_token")


class TestSecurityContext(unittest.TestCase):
    """
    Summary:
        Tests audit identity extraction and confirms no masking/token logic remains in security.py.
    """

    def test_no_masking_or_token_functions(self):
        """Summary: All mask_*/sanitize/validate helpers were removed from the MCP security module."""
        for name in ("mask_subscriber_name", "mask_email", "mask_phone", "mask_ssn", "mask_credit_card",
                     "mask_street_address", "sanitize_customer_record", "validate_token",
                     "validate_bearer_token", "PII_REDACTIONS_COUNTER", "AUTH_FAILURES_COUNTER"):
            self.assertFalse(hasattr(security, name), name)

    def test_phone_normalization(self):
        """Summary: Formatted and E.164 numbers normalize to the same 10 digits."""
        self.assertEqual(normalize_phone_digits(ELENA_PHONE), "5552345678")
        self.assertEqual(normalize_phone_digits("+15550001337"), "5550001337")
        self.assertEqual(normalize_phone_digits(None), "")

    def test_extract_caller_identity(self):
        """Summary: Identity precedence: user headers, agent headers, JWT, payload, fallback."""
        self.assertEqual(
            extract_caller_identity(user_email_header="accounts.google.com:sarah.jenkins@telecom.example"),
            "sarah.jenkins@telecom.example"
        )
        self.assertEqual(extract_caller_identity(custom_agent_header="executive-mike@telecom.example"),
                         "executive-mike@telecom.example")
        self.assertEqual(extract_caller_identity(payload_user="ops@telecom.example"), "ops@telecom.example")
        self.assertEqual(
            extract_caller_identity(token="Bearer " + _fake_jwt({"email": "apigee@p.iam.gserviceaccount.com"})),
            "service-account:apigee@p.iam.gserviceaccount.com"
        )
        self.assertEqual(extract_caller_identity(), DEFAULT_AGENT_CALLER)

    def test_security_context_defaults_to_configured_email(self):
        """Summary: Without identity headers the configured DEFAULT_END_USER_EMAIL is used."""
        ctx = extract_security_context(headers={
            "x-forwarded-for": "173.194.96.179, 10.0.0.1",
            "x-cloud-trace-context": "1d78db52281b503501f0dcc110a0d99b/12345;o=1",
        })
        self.assertEqual(ctx["caller"], DEFAULT_AGENT_CALLER)
        self.assertEqual(ctx["caller_type"], "AI_AGENT")
        self.assertEqual(ctx["end_user_email"], "fallback-user@hybridmcp.invalid")
        self.assertEqual(ctx["client_ip"], "173.194.96.179")
        self.assertEqual(ctx["trace_id"], "1d78db52281b503501f0dcc110a0d99b")
        self.assertEqual(ctx["auth_method"], "IAM_EDGE_PERIMETER")
        self.assertEqual(ctx["apigee_developer_app"], "N/A")

    def test_security_context_captures_apigee_and_end_user_headers(self):
        """Summary: Apigee consumer headers and agent-forwarded end-user email are captured; SA JWT email is ignored."""
        sa_token = _fake_jwt({"email": "hmcp-apigee@proj.iam.gserviceaccount.com", "sub": "1234"})
        ctx = extract_security_context(headers={
            "authorization": f"Bearer {sa_token}",
            "x-apigee-developer-app": "telecom-agent-app",
            "x-apigee-client-id": "client-abc",
            "x-apigee-proxy": "telecom-mcp",
            "x-user-email": "second@telecom.example",
            "x-end-user-email": "operator@telecom.example",
        })
        self.assertEqual(ctx["apigee_developer_app"], "telecom-agent-app")
        self.assertEqual(ctx["apigee_client_id"], "client-abc")
        self.assertEqual(ctx["apigee_proxy"], "telecom-mcp")
        self.assertEqual(ctx["caller"], "apigee-app:telecom-agent-app")
        self.assertEqual(ctx["end_user_email"], "operator@telecom.example")
        self.assertEqual(ctx["auth_method"], "OIDC_ID_TOKEN")

    def test_goog_authenticated_header_used_and_prefix_stripped(self):
        """Summary: x-goog-authenticated-user-email is honoured with the IAP prefix removed."""
        ctx = extract_security_context(headers={
            "x-goog-authenticated-user-email": "accounts.google.com:iap-user@telecom.example"
        })
        self.assertEqual(ctx["end_user_email"], "iap-user@telecom.example")


class TestDatabaseFallbackFlag(unittest.IsolatedAsyncioTestCase):
    """
    Summary:
        Verifies DB_ALLOW_SQLITE_FALLBACK semantics and Cloud SQL socket URL handling.
    """

    def test_populated_cloudsql_socket_url_is_kept(self):
        """Summary: A real Cloud SQL unix-socket URL is never diverted to SQLite."""
        url = "postgresql+asyncpg://u:p@/telecom_db?host=/cloudsql/my-proj:europe-west1:hmcp-sql"
        self.assertEqual(resolve_database_url(url, allow_sqlite_fallback=True), url)
        self.assertEqual(resolve_database_url(url, allow_sqlite_fallback=False), url)

    def test_empty_cloudsql_socket_behaviour(self):
        """Summary: Unpopulated '/cloudsql/' host -> SQLite when allowed, error when disallowed."""
        url = "postgresql+asyncpg://u:p@/telecom_db?host=/cloudsql/"
        self.assertEqual(resolve_database_url(url, True), database.SQLITE_FALLBACK_URL)
        with self.assertRaises(database.DatabaseConfigurationError):
            resolve_database_url(url, False)
        with self.assertRaises(database.DatabaseConfigurationError):
            resolve_database_url("", False)

    async def test_postgres_failure_raises_when_fallback_disabled(self):
        """Summary: With fallback disabled an unreachable Postgres raises instead of switching engines."""
        original_engine = database.engine
        unreachable = database._build_engine("postgresql+asyncpg://u:p@127.0.0.1:1/telecom_db")
        database.engine = unreachable
        try:
            with mock.patch.object(database.settings, "DB_ALLOW_SQLITE_FALLBACK", False), \
                    mock.patch.object(database, "DB_CONNECT_ATTEMPTS", 1):
                with self.assertRaises(database.DatabaseConfigurationError):
                    await init_db()
            self.assertIs(database.engine, unreachable)
        finally:
            await unreachable.dispose()
            database.engine = original_engine
            database._configure_sessions(original_engine)

    async def test_postgres_failure_falls_back_when_enabled(self):
        """Summary: With fallback enabled an unreachable Postgres switches to SQLite."""
        original_engine = database.engine
        unreachable = database._build_engine("postgresql+asyncpg://u:p@127.0.0.1:1/telecom_db")
        database.engine = unreachable
        tmp_dir = tempfile.mkdtemp()
        fallback_url = f"sqlite+aiosqlite:///{os.path.join(tmp_dir, 'fallback.db')}"
        try:
            with mock.patch.object(database.settings, "DB_ALLOW_SQLITE_FALLBACK", True), \
                    mock.patch.object(database, "DB_CONNECT_ATTEMPTS", 1), \
                    mock.patch.object(database, "SQLITE_FALLBACK_URL", fallback_url):
                await init_db()
            self.assertEqual(database.current_dialect_name(), "sqlite")
            self.assertIn("fallback.db", str(database.engine.url))
        finally:
            await database.engine.dispose()
            database.engine = original_engine
            database._configure_sessions(original_engine)


class TestSeeding(unittest.IsolatedAsyncioTestCase):
    """
    Summary:
        Tests seeding idempotency, the advisory lock on PostgreSQL, and the SECURITY-TEST persona.
    """

    async def asyncTearDown(self):
        """Summary: Dispose pooled connections bound to this test's event loop."""
        await database.engine.dispose()

    async def test_seeding_is_idempotent(self):
        """Summary: Re-running the seeder does not duplicate personas, bills, or interactions."""
        await seed_synthetic_telecom_data()
        await seed_synthetic_telecom_data()
        async with AsyncSessionLocal() as session:
            customers = (await session.execute(select(func.count(Customer.id)))).scalar()
            bills = (await session.execute(select(func.count(BillingRecord.id)))).scalar()
            interactions = (await session.execute(select(func.count(CallInteraction.id)))).scalar()
            names = set((await session.execute(select(Customer.first_name))).scalars().all())
        self.assertEqual(customers, 5)
        self.assertEqual(bills, 30)  # 5 accounts x 6 months
        self.assertGreaterEqual(interactions, 4)
        self.assertEqual(names, {"Elena", "Marcus", "Amina", "David", "Mallory"})

    async def test_security_persona_present_with_payload(self):
        """Summary: Mallory Tester exists once with the injection payload in an open ticket."""
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(Customer).where(Customer.phone_number == "+15550001337"))).scalars().all()
            self.assertEqual(len(rows), 1)
            mallory = rows[0]
            self.assertEqual((mallory.first_name, mallory.last_name, mallory.postal_code), ("Mallory", "Tester", "94043"))
            titles = (await session.execute(select(SupportTicket.title).where(SupportTicket.customer_id == mallory.id))).scalars().all()
        self.assertIn(SECURITY_TEST_INJECTION_PAYLOAD, titles)
        self.assertLessEqual(len(SECURITY_TEST_INJECTION_PAYLOAD), 255)

    async def test_advisory_lock_only_on_postgres(self):
        """Summary: pg_advisory_xact_lock is issued on PostgreSQL and skipped on SQLite."""
        fake_session = mock.MagicMock()
        fake_session.execute = mock.AsyncMock()
        with mock.patch.object(seed_data, "current_dialect_name", return_value="postgresql"):
            await seed_data._acquire_seed_lock(fake_session)
        statement = str(fake_session.execute.await_args.args[0])
        self.assertIn("pg_advisory_xact_lock", statement)
        self.assertEqual(fake_session.execute.await_args.args[1], {"key": seed_data.SEED_ADVISORY_LOCK_KEY})

        fake_session.execute.reset_mock()
        with mock.patch.object(seed_data, "current_dialect_name", return_value="sqlite"):
            await seed_data._acquire_seed_lock(fake_session)
        fake_session.execute.assert_not_awaited()

    async def test_every_demo_baseline_device_is_seeded(self):
        """Summary: reset_demo_data.py can only restore devices the seeder creates, so the serials must match exactly."""
        async with AsyncSessionLocal() as session:
            serials = set((await session.execute(select(Device.serial_number))).scalars().all())
        self.assertEqual(serials, set(DEMO_DEVICE_BASELINES))

    def test_baseline_kwargs_are_isolated_copies(self):
        """Summary: Mutating returned telemetry (as a reboot does) must never alter the shared baseline table."""
        kwargs = device_baseline_kwargs("ONT-HW-99281-FBR")
        self.assertEqual(kwargs["health_status"], "DEGRADED")
        kwargs["live_telemetry"]["packet_loss_percent"] = 0.05
        self.assertEqual(DEMO_DEVICE_BASELINES["ONT-HW-99281-FBR"]["live_telemetry"]["packet_loss_percent"], 14.2)
        reboot_age = datetime.now(timezone.utc).replace(tzinfo=None) - kwargs["last_reboot_time"]
        self.assertEqual(round(reboot_age.total_seconds() / 86400), 59)
        self.assertNotIn("last_reboot_time", device_baseline_kwargs("ONT-HW-77312-FBR"))


class TestMCPToolsRawOutput(unittest.IsolatedAsyncioTestCase):
    """
    Summary:
        Tool outputs must contain RAW PII (Apigee + Model Armor mask it downstream).
    """

    async def asyncTearDown(self):
        """Summary: Dispose pooled connections bound to this test's event loop."""
        await database.engine.dispose()

    async def test_search_customer_returns_raw_values(self):
        """Summary: Search by phone, digits, and account number returns the full name and email."""
        res = await search_customer_tool(phone_number=ELENA_PHONE, caller_id="test-agent")
        self.assertEqual(res["status"], "success")
        match = res["customers"][0]
        self.assertIn("5552345678", match["phone_number"])
        self.assertEqual(match["subscriber_name"], "Elena Rostova")
        self.assertEqual(match["email"], "elena.rostova@example.com")

        by_digits = await search_customer_tool(query="5552345678")
        self.assertEqual(by_digits["count"], 1)
        by_account = await search_customer_tool(query="TEL-ACC-88129")
        self.assertEqual(by_account["customers"][0]["subscriber_name"], "Elena Rostova")

    async def test_customer_360_returns_raw_values(self):
        """Summary: Customer 360 exposes raw SSN, address, email and full card under 'payment_card'."""
        c360 = await get_customer_360_tool(phone_number=ELENA_PHONE)
        self.assertEqual(c360["status"], "success")
        profile = c360["customer_360"]
        self.assertEqual(profile["loyalty_tier"], "Gold")
        self.assertEqual(profile["subscriber_name"], "Elena Rostova")
        self.assertEqual(profile["email"], "elena.rostova@example.com")
        self.assertEqual(profile["ssn"], "987-65-4321")
        self.assertEqual(profile["street_address"], "742 Evergreen Terrace, Apt 4B")
        account = profile["accounts"][0]
        self.assertEqual(account["payment_card"], "4111222233331111")
        self.assertNotIn("payment_card_masked", account)
        self.assertNotIn("*", json.dumps(profile))

    async def test_billing_and_upsell_raw_values(self):
        """Summary: Billing exposes the raw card; upsell exposes the raw subscriber name."""
        bill = await get_billing_breakdown_tool(phone_number="+1 (555) 678-4321")
        self.assertEqual(bill["status"], "success")
        self.assertEqual(bill["payment_card"], "378282246310005")
        self.assertNotIn("payment_card_masked", bill)

        upsell = await get_upsell_recommendations_tool(phone_number="+1 (555) 987-1234")
        self.assertEqual(upsell["status"], "success")
        self.assertEqual(upsell["subscriber_name"], "Marcus Vance")
        self.assertIn("Unlimited 5G", upsell["recommendations"][0]["title"])

    async def test_diagnostics_reboot_and_outages(self):
        """Summary: Business logic for diagnostics, remote actions and outages is unchanged."""
        diag = await get_service_diagnostics_tool(phone_number=ELENA_PHONE)
        self.assertEqual(diag["diagnostics"][0]["root_cause_diagnosis"], "OPTICAL_SIGNAL_DEGRADATION")
        action = await run_remote_device_action_tool(phone_number=ELENA_PHONE, action="reboot")
        self.assertEqual(action["updated_health_status"], "HEALTHY")
        outage = await check_network_outages_tool(postal_code="98101")
        self.assertTrue(outage["has_active_outage"])

    async def test_security_persona_payload_returned_raw(self):
        """Summary: Mallory's injection payload is returned verbatim by 360, search and billing."""
        c360 = await get_customer_360_tool(phone_number=MALLORY_PHONE)
        self.assertEqual(c360["status"], "success")
        self.assertEqual(c360["customer_360"]["subscriber_name"], "Mallory Tester")
        self.assertEqual(c360["customer_360"]["postal_code"], "94043")
        self.assertIn(SECURITY_TEST_INJECTION_PAYLOAD, [t["title"] for t in c360["customer_360"]["open_tickets"]])

        search = await search_customer_tool(phone_number=MALLORY_PHONE)
        self.assertIn(SECURITY_TEST_INJECTION_PAYLOAD, search["customers"][0]["open_ticket_titles"])

        bill = await get_billing_breakdown_tool(account_id=SECURITY_TEST_ACCOUNT)
        self.assertIn(SECURITY_TEST_INJECTION_PAYLOAD, [i["dispute_notes"] for i in bill["invoices"]])


class TestMCPEndpoints(unittest.TestCase):
    """
    Summary:
        FastAPI HTTP / JSON-RPC endpoints: no application token, raw tool results, gateway wording.
    """

    def setUp(self):
        """Summary: Creates a TestClient without auth headers (Cloud Run IAM is the only gate)."""
        from fastapi.testclient import TestClient
        from src.mcp.server import app
        self.client = TestClient(app)

    def tearDown(self):
        """Summary: Dispose pooled DB connections bound to the TestClient's event loop."""
        asyncio.run(database.engine.dispose())

    def test_initialize_without_any_token(self):
        """Summary: No Authorization header is required by the application."""
        res = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                             "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["result"]["serverInfo"]["name"], "telecom-mcp-server")

    def test_arbitrary_bearer_is_not_rejected(self):
        """Summary: The app never validates tokens itself (Cloud Run already did)."""
        res = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
                               headers={"Authorization": "Bearer not-a-real-token"})
        self.assertEqual(res.status_code, 200)

    def test_tool_descriptions_reference_gateway_masking(self):
        """Summary: Descriptions must not claim the server masks PII; they credit Apigee + Model Armor."""
        res = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        tools = res.json()["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(len(names), 8)
        for tool in tools:
            self.assertNotIn("strictly masked", tool["description"])
        c360 = next(t for t in tools if t["name"] == "get_customer_360")
        self.assertIn("Apigee + Model Armor", c360["description"])

    def test_tools_call_returns_raw_pii(self):
        """Summary: JSON-RPC tools/call returns raw names/SSN inside result.content[0].text."""
        payload = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                   "params": {"name": "get_customer_360", "arguments": {"phone_number": ELENA_PHONE}}}
        res = self.client.post("/mcp", json=payload, headers={"x-end-user-email": "operator@telecom.example",
                                                              "x-apigee-developer-app": "telecom-agent-app"})
        self.assertEqual(res.status_code, 200)
        inner = json.loads(res.json()["result"]["content"][0]["text"])
        self.assertEqual(inner["customer_360"]["subscriber_name"], "Elena Rostova")
        self.assertEqual(inner["customer_360"]["ssn"], "987-65-4321")

    def test_execute_endpoint_raw(self):
        """Summary: /execute returns raw values and ignores client-supplied caller_id."""
        res = self.client.post("/execute", json={"name": "search_customer",
                                                 "arguments": {"phone_number": ELENA_PHONE, "caller_id": "spoofed"}})
        self.assertEqual(res.status_code, 200)
        cust = res.json()["result"]["customers"][0]
        self.assertEqual(cust["subscriber_name"], "Elena Rostova")

    def test_get_discovery_and_health(self):
        """Summary: GET /mcp discovery JSON and /health work without credentials."""
        self.assertEqual(self.client.get("/mcp").json()["status"], "healthy")
        self.assertEqual(self.client.get("/health").json()["status"], "healthy")

    def test_invalid_json_rejected(self):
        """Summary: Malformed JSON yields HTTP 400."""
        res = self.client.post("/mcp", content=b"{not json", headers={"content-type": "application/json"})
        self.assertEqual(res.status_code, 400)


class TestLokiAuditIdToken(unittest.IsolatedAsyncioTestCase):
    """
    Summary:
        Loki pushes carry a metadata-server ID token when LOKI_AUTH_MODE=google_id_token.
    """

    LOKI_URL = "https://hmcp-loki.example.test/"

    def _make_client(self, mode: str, loki_status_for_token: dict, metadata_ok: bool = True):
        """
        Summary:
            Builds a LokiAuditClient wired to an httpx.MockTransport simulating metadata + Loki.

        Parameters:
            mode (str): LOKI_AUTH_MODE value.
            loki_status_for_token (dict): Maps Authorization header value -> HTTP status from Loki.
            metadata_ok (bool): Whether the metadata server returns tokens.

        Return Value:
            tuple: (client, calls) where calls records {'metadata': [...], 'loki': [...]}.

        Exceptions/Errors:
            None.
        """
        calls = {"metadata": [], "loki": []}
        tokens = iter(["id-token-1", "id-token-2", "id-token-3"])

        def handler(request: httpx.Request) -> httpx.Response:
            """
            Summary:
                Mock transport routing metadata and Loki requests.

            Parameters:
                request (httpx.Request): Outgoing request.

            Return Value:
                httpx.Response: Simulated response.

            Exceptions/Errors:
                None.
            """
            if "computeMetadata" in request.url.path:
                calls["metadata"].append(request)
                if not metadata_ok:
                    return httpx.Response(500, text="boom")
                return httpx.Response(200, text=next(tokens))
            calls["loki"].append(request)
            auth = request.headers.get("authorization", "")
            return httpx.Response(loki_status_for_token.get(auth, 204))

        client = LokiAuditClient(loki_url=self.LOKI_URL, auth_mode=mode)
        mock_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client._client = lambda: mock_http
        return client, calls

    async def test_id_token_attached_with_audience_and_cached(self):
        """Summary: Token fetched once with audience = Loki URL (no trailing slash) and reused."""
        client, calls = self._make_client("google_id_token", {})
        self.assertTrue(await client.push_to_loki({"app": "t"}, "line-1"))
        self.assertTrue(await client.push_to_loki({"app": "t"}, "line-2"))

        self.assertEqual(len(calls["metadata"]), 1)
        meta_req = calls["metadata"][0]
        self.assertEqual(meta_req.headers["metadata-flavor"], "Google")
        self.assertEqual(meta_req.url.params["audience"], "https://hmcp-loki.example.test")
        self.assertEqual([r.headers["authorization"] for r in calls["loki"]],
                         ["Bearer id-token-1", "Bearer id-token-1"])
        self.assertEqual(str(calls["loki"][0].url), "https://hmcp-loki.example.test/loki/api/v1/push")

    async def test_token_refreshed_on_401(self):
        """Summary: A 401 from Loki triggers a fresh token and one retry."""
        client, calls = self._make_client("google_id_token", {"Bearer id-token-1": 401})
        self.assertTrue(await client.push_to_loki({"app": "t"}, "line"))
        self.assertEqual(len(calls["metadata"]), 2)
        self.assertEqual([r.headers["authorization"] for r in calls["loki"]],
                         ["Bearer id-token-1", "Bearer id-token-2"])

    async def test_token_refreshed_on_expiry(self):
        """Summary: An expired cache entry forces a new metadata fetch."""
        client, calls = self._make_client("google_id_token", {})
        await client.push_to_loki({"app": "t"}, "line")
        client._token_expiry = 0.0
        await client.push_to_loki({"app": "t"}, "line")
        self.assertEqual(len(calls["metadata"]), 2)
        self.assertEqual(calls["loki"][-1].headers["authorization"], "Bearer id-token-2")

    async def test_no_header_in_none_mode(self):
        """Summary: LOKI_AUTH_MODE=none never contacts the metadata server."""
        client, calls = self._make_client("none", {})
        self.assertTrue(await client.push_to_loki({"app": "t"}, "line"))
        self.assertEqual(calls["metadata"], [])
        self.assertNotIn("authorization", calls["loki"][0].headers)

    async def test_metadata_failure_never_raises(self):
        """Summary: Metadata errors degrade gracefully (no exception, push reported as failed)."""
        client, calls = self._make_client("google_id_token", {"": 403}, metadata_ok=False)
        with self.assertLogs("hybrid_mcp.audit", level="WARNING"):
            self.assertFalse(await client.push_to_loki({"app": "t"}, "line"))
        self.assertGreater(client._token_backoff_until, 0.0)

    async def test_log_event_ships_raw_payload_in_background(self):
        """Summary: log_event returns immediately and the background push carries the raw response."""
        client, calls = self._make_client("google_id_token", {})
        await client.log_event(
            event_type="MCP_TOOL_EXECUTION", caller_identity="agent", tool_name="get_customer_360",
            response_payload={"subscriber_name": "Elena Rostova", "ssn": "987-65-4321"},
            security_context={"end_user_email": "operator@telecom.example", "apigee_developer_app": "app-1"},
        )
        await client.flush()
        body = json.loads(calls["loki"][0].content)
        stream = body["streams"][0]
        self.assertEqual(stream["stream"]["app"], settings.SERVICE_NAME)
        record = json.loads(stream["values"][0][1])
        self.assertEqual(record["response"]["ssn"], "987-65-4321")
        self.assertEqual(record["apigee_developer_app"], "app-1")

    def test_header_sanitization(self):
        """Summary: Credentials are redacted from audited headers; other headers are kept."""
        token = _fake_jwt({"email": "sa@p.iam.gserviceaccount.com"})
        cleaned = sanitize_audit_headers({
            "authorization": f"Bearer {token}", "x-apikey": "secret-key", "x-end-user-email": "a@b.example"
        })
        self.assertTrue(cleaned["authorization"].endswith(".[REDACTED_SIGNATURE]"))
        self.assertEqual(cleaned["x-apikey"], "[REDACTED]")
        self.assertEqual(cleaned["x-end-user-email"], "a@b.example")


if __name__ == "__main__":
    unittest.main()
