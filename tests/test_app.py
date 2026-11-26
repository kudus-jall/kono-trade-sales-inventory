import os
import json
import tempfile
import secrets
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import app as sales_app
import backup_db
from kono_trade import create_app
from kono_trade.repositories.database import connect_db
from kono_trade.routes import auth as auth_routes, reports as reports_routes
from seed_demo import DEMO_PASSWORD, seed_demo_database
from cryptography.fernet import Fernet
from PIL import Image


class SalesSystemTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        sales_app.DATABASE = Path(self.temp_dir.name) / "test.db"
        sales_app.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        sales_app.init_db()
        self.client = sales_app.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_encrypted_backup_restores_and_rejects_tampering(self):
        db = sales_app.connect_db()
        db.execute(
            "INSERT INTO businesses (name, business_type, owner_name, phone, created_at) VALUES (?, ?, ?, ?, ?)",
            ("Backup Original", "General trading", "Test Owner", "076000000", "2026-01-01T00:00:00"),
        )
        db.commit()
        db.close()
        backup_db.DATABASE = sales_app.DATABASE
        backup_db.BACKUP_DIR = Path(self.temp_dir.name) / "backups"
        backup_path = Path(self.temp_dir.name) / "placeholder.kbackup"
        key = Fernet.generate_key().decode("ascii")
        with patch.dict(os.environ, {"BACKUP_ENCRYPTION_KEY": key}):
            backup_path = backup_db.backup_database()
            self.assertNotIn(b"Backup Original", backup_path.read_bytes())
            db = sales_app.connect_db()
            db.execute("UPDATE businesses SET name = 'Changed after backup'")
            db.commit()
            db.close()
            safety_backup = backup_db.restore_database(backup_path)
            self.assertIsNotNone(safety_backup)
            db = sales_app.connect_db()
            self.assertEqual(db.execute("SELECT name FROM businesses").fetchone()[0], "Backup Original")
            db.close()

            corrupted = backup_db.BACKUP_DIR / "tampered.kbackup"
            corrupted.write_bytes(backup_path.read_bytes()[:-1] + b"X")
            with self.assertRaises(ValueError):
                backup_db.restore_database(corrupted)
            db = sales_app.connect_db()
            self.assertEqual(db.execute("SELECT name FROM businesses").fetchone()[0], "Backup Original")
            db.close()

    def post(self, url, data):
        with self.client.session_transaction() as browser_session:
            has_csrf = "_csrf" in browser_session
        if not has_csrf:
            self.client.get("/login")
        with self.client.session_transaction() as browser_session:
            data["_csrf"] = browser_session["_csrf"]
        if url in {
            "/sales", "/purchases", "/supplier-payments", "/supplier-returns",
            "/sales-returns", "/payments",
        }:
            data.setdefault("request_key", secrets.token_urlsafe(24))
        return self.client.post(url, data=data, follow_redirects=True)

    def register_owner(self, username="owner"):
        response = self.post(
            "/register",
            {
                "business_name": "Test Trading",
                "business_type": "General trading",
                "owner_name": "Test Owner",
                "phone": "076000000",
                "email": f"{username}@example.test",
                "username": username,
                "password": "secure-test-password",
                "currency": "SLE",
                "address": "Freetown",
            },
        )
        self.assertEqual(response.status_code, 200)
        return response

    def add_product_and_stock(self):
        self.post(
            "/products",
            {
                "name": "Rice",
                "sku": "RICE-001",
                "category": "Food",
                "unit": "bag",
                "price": "30.50",
                "cost": "0",
                "stock": "0",
                "reorder_level": "2",
            },
        )
        self.post(
            "/suppliers",
            {
                "name": "Rice Supplier",
                "phone": "076111111",
                "contact_person": "Supplier Contact",
                "address": "Freetown",
            },
        )
        self.post(
            "/purchases",
            {
                "product_id": "1",
                "supplier_id": "1",
                "quantity": "10",
                "unit_cost": "20.25",
                "paid": "100",
                "method": "Cash",
            },
        )

    def test_owner_can_register_purchase_sell_and_receive_credit_payment(self):
        self.register_owner()
        self.add_product_and_stock()
        db = sales_app.connect_db()
        product = db.execute("SELECT stock, cost_minor FROM products").fetchone()
        self.assertEqual(product["stock"], 10)
        self.assertEqual(product["cost_minor"], 2025)
        db.close()

        self.post(
            "/customers",
            {"name": "Aminata Customer", "phone": "076222222", "address": "Freetown"},
        )
        response = self.post(
            "/sales",
            {
                "product_id": "1",
                "customer_id": "1",
                "quantity": "2",
                "discount": "1.00",
                "paid": "20",
                "method": "Orange Money",
            },
        )
        self.assertIn(b"Sale recorded", response.data)
        self.assertIn(b"Receipt", response.data)

        db = sales_app.connect_db()
        sale = db.execute("SELECT total_minor, cost_minor FROM sales").fetchone()
        product = db.execute("SELECT stock FROM products").fetchone()
        self.assertEqual(sale["total_minor"], 6000)
        self.assertEqual(sale["cost_minor"], 4050)
        self.assertEqual(product["stock"], 8)
        self.assertEqual(
            db.execute("SELECT amount_minor FROM payments WHERE sale_id = 1").fetchone()[0],
            2000,
        )
        db.close()

        response = self.post(
            "/payments",
            {"customer_id": "1", "amount": "40", "method": "Cash"},
        )
        self.assertIn(b"Payment recorded", response.data)
        db = sales_app.connect_db()
        outstanding = db.execute(
            """SELECT COALESCE(SUM(s.total_minor), 0) -
                      COALESCE((SELECT SUM(p.amount_minor) FROM payments p
                                WHERE p.customer_id = 1 AND p.direction = 'received'), 0)
               FROM sales s WHERE s.customer_id = 1"""
        ).fetchone()[0]
        self.assertEqual(outstanding, 0)
        db.close()

    def test_attendant_can_record_credit_payment_once_without_balance_disclosure(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post("/customers", {"name": "Attendant Payment Customer"})
        self.post(
            "/sales",
            {
                "product_id": "1",
                "customer_id": "1",
                "quantity": "1",
                "discount": "0",
                "paid": "0",
                "method": "Cash",
            },
        )
        self.post(
            "/team",
            {
                "full_name": "Payment Attendant",
                "username": "payment-attendant",
                "password": "secure-staff-password",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.post("/login", {"username": "payment-attendant", "password": "secure-staff-password"})

        page = self.client.get("/payments")
        self.assertIn(b"Attendant Payment Customer", page.data)
        self.assertNotIn(b"owes SLE", page.data)
        self.assertIn(b"Restricted", self.client.get("/customers").data)
        payment = {
            "customer_id": "1",
            "amount": "10",
            "method": "Cash",
            "request_key": "attendant-payment-once",
        }
        self.assertIn(b"Payment recorded", self.post("/payments", payment).data)
        self.assertIn(b"already recorded", self.post("/payments", payment).data)
        db = sales_app.connect_db()
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM payments WHERE request_key = ?", (payment["request_key"],)).fetchone()[0],
            1,
        )
        self.assertEqual(
            db.execute(
                """SELECT SUM(s.total_minor) -
                          (SELECT SUM(amount_minor) FROM payments
                           WHERE customer_id = 1 AND direction = 'received')
                   FROM sales s WHERE s.customer_id = 1"""
            ).fetchone()[0],
            2050,
        )
        db.close()

    def test_sale_form_retry_does_not_duplicate_a_sale(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post(
            "/customers",
            {"name": "Repeat Test Customer", "phone": "076222222"},
        )
        sale_form = {
            "product_id": "1",
            "customer_id": "1",
            "quantity": "1",
            "discount": "0",
            "paid": "30.50",
            "method": "Cash",
            "request_key": "same-form-submission",
        }
        self.post("/sales", sale_form)
        response = self.post("/sales", sale_form)
        self.assertIn(b"already have been submitted", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sales").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT stock FROM products").fetchone()[0], 9)
        db.close()

    def test_split_sale_payments_are_stored_and_shown_on_receipt(self):
        self.register_owner()
        self.add_product_and_stock()
        response = self.post(
            "/sales",
            {
                "product_id": "1",
                "quantity": "1",
                "discount": "0",
                "payment_amount": ["10.00", "20.50", "0"],
                "payment_method": ["Cash", "Orange Money", "Bank Transfer"],
            },
        )
        self.assertIn(b"Cash", response.data)
        self.assertIn(b"Orange Money", response.data)
        db = sales_app.connect_db()
        payments = db.execute(
            "SELECT amount_minor, method FROM payments WHERE sale_id = 1 ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["amount_minor"], row["method"]) for row in payments],
            [(1000, "Cash"), (2050, "Orange Money")],
        )
        db.close()
        pdf = self.client.get("/receipts/1.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        self.assertEqual(pdf.mimetype, "application/pdf")

    def test_purchase_sale_and_return_convert_units_to_base_stock(self):
        self.register_owner()
        self.post(
            "/products",
            {
                "name": "Rice",
                "sku": "RICE-KG",
                "category": "Food",
                "unit": "kg",
                "price": "5",
                "cost": "0",
                "stock": "0",
                "reorder_level": "5",
            },
        )
        self.post(
            "/products/1/units",
            {"unit_name": "bag", "to_base_factor": "50"},
        )
        self.post(
            "/suppliers",
            {"name": "Bulk Rice Supplier", "phone": "076111111"},
        )
        db = sales_app.connect_db()
        bag_unit_id = db.execute(
            "SELECT id FROM product_units WHERE product_id = 1 AND unit_name = 'bag'"
        ).fetchone()[0]
        base_unit_id = db.execute(
            "SELECT id FROM product_units WHERE product_id = 1 AND unit_name = 'kg'"
        ).fetchone()[0]
        db.close()
        self.post(
            "/purchases",
            {
                "purchase_unit_id": str(bag_unit_id),
                "supplier_id": "1",
                "quantity": "2",
                "unit_cost": "100",
                "paid": "200",
                "method": "Cash",
            },
        )
        self.post(
            "/customers",
            {"name": "Rice Buyer"},
        )
        response = self.post(
            "/sales",
            {
                "sale_unit_id": str(bag_unit_id),
                "customer_id": "1",
                "quantity": "1",
                "discount": "0",
                "payment_amount": ["250", "0", "0"],
                "payment_method": ["Cash", "Orange Money", "Bank Transfer"],
            },
        )
        self.assertIn(b"Receipt", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT stock FROM products").fetchone()[0], 50)
        purchase = db.execute(
            "SELECT quantity, base_quantity, unit_name FROM purchases"
        ).fetchone()
        self.assertEqual(tuple(purchase), (2, 100, "bag"))
        sale = db.execute(
            "SELECT quantity, base_quantity, unit_name, total_minor, cost_minor FROM sales"
        ).fetchone()
        self.assertEqual(tuple(sale), (1, 50, "bag", 25000, 10000))
        db.close()
        self.post(
            "/sales-returns",
            {
                "sale_id": "1",
                "quantity": "0.5",
                "condition": "resellable",
                "adjustment_type": "refund",
                "method": "Cash",
                "reason": "Half bag returned",
            },
        )
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT stock FROM products").fetchone()[0], 75)
        returned = db.execute(
            "SELECT quantity, base_quantity FROM sales_returns"
        ).fetchone()
        self.assertEqual(tuple(returned), (0.5, 25))
        db.close()

    def test_owner_voids_sale_with_stock_and_payment_reversals(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post("/customers", {"name": "Void Buyer"})
        self.post(
            "/sales",
            {
                "product_id": "1",
                "customer_id": "1",
                "quantity": "1",
                "discount": "0",
                "paid": "30.50",
                "method": "Cash",
            },
        )
        response = self.post(
            "/sales/1/void",
            {"reason": "Duplicate sale entered by mistake"},
        )
        self.assertIn(b"Sale voided", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT stock FROM products").fetchone()[0], 10)
        sale = db.execute("SELECT status FROM sales WHERE id = 1").fetchone()
        self.assertEqual(sale["status"], "void")
        self.assertEqual(
            db.execute(
                "SELECT SUM(amount_minor) FROM payments WHERE sale_id=1 AND direction='received'"
            ).fetchone()[0],
            3050,
        )
        self.assertEqual(
            db.execute(
                "SELECT SUM(amount_minor) FROM payments WHERE sale_id=1 AND direction='paid'"
            ).fetchone()[0],
            3050,
        )
        self.assertEqual(
            db.execute("SELECT reason FROM sale_voids WHERE sale_id = 1").fetchone()[0],
            "Duplicate sale entered by mistake",
        )
        db.close()
        voided_export = self.client.get("/exports/sales.csv").get_data(as_text=True)
        self.assertIn(",0,0,void", voided_export)
        customer_page = self.client.get("/customers")
        self.assertIn(b"SLE 0.00", customer_page.data)
        report = self.client.get("/reports")
        self.assertIn(b"SLE 0.00", report.data)
        self.assertIn(b"VOIDED", self.client.get("/receipts/1").data)
        self.assertTrue(self.client.get("/receipts/1.pdf").data.startswith(b"%PDF"))

    def test_owner_can_change_currency_before_transactions_and_upload_logo(self):
        self.register_owner()
        response = self.post(
            "/settings",
            {
                "name": "Freetown Traders",
                "business_type": "Building materials",
                "phone": "076123456",
                "address": "Freetown",
                "currency": "USD",
            },
        )
        self.assertIn(b"Business settings saved", response.data)
        db = sales_app.connect_db()
        self.assertEqual(
            tuple(db.execute("SELECT name, currency FROM businesses").fetchone()),
            ("Freetown Traders", "USD"),
        )
        db.close()
        image_bytes = BytesIO()
        Image.new("RGB", (20, 20), color="green").save(image_bytes, format="PNG")
        image_bytes.seek(0)
        logo_response = self.post(
            "/settings",
            {
                "name": "Freetown Traders",
                "business_type": "Building materials",
                "phone": "076123456",
                "address": "Freetown",
                "currency": "USD",
                "logo": (image_bytes, "logo.png"),
            },
        )
        self.assertIn(b"Current logo", logo_response.data)
        logo_response = self.client.get("/business-logo")
        self.assertEqual(logo_response.mimetype, "image/png")
        logo_response.close()

    def test_ai_stays_disabled_without_provider_configuration_and_local_insights_work(self):
        self.register_owner()
        missing_configuration = {
            "AI_PROVIDER": "",
            "AI_API_KEY": "",
            "AI_BASE_URL": "",
            "AI_MODEL": "",
        }
        with patch.dict(os.environ, missing_configuration):
            enable_response = self.post(
                "/settings",
                {
                    "settings_section": "ai",
                    "ai_insights_enabled": "1",
                    "ai_data_consent": sales_app.AI_CONSENT_VERSION,
                },
            )
            self.assertIn(b"AI_PROVIDER", enable_response.data)
            db = sales_app.connect_db()
            self.assertEqual(
                db.execute("SELECT ai_insights_enabled FROM businesses").fetchone()[0],
                0,
            )
            db.close()
            with patch.object(sales_app, "call_ai_provider") as provider:
                response = self.post(
                    "/insights",
                    {
                        "mode": "ai",
                        "question": "How are sales?",
                        "start": sales_app.date.today().isoformat(),
                        "end": sales_app.date.today().isoformat(),
                    },
                )
                provider.assert_not_called()
            self.assertIn(b"AI is disabled", response.data)
            self.assertIn(b"recorded net sales are SLE 0.00", response.data)
        with patch.dict(
            os.environ,
            {
                "AI_PROVIDER": "openai-compatible",
                "AI_API_KEY": "test-key",
                "AI_BASE_URL": "https://provider.example/v1",
                "AI_MODEL": "test-model",
            },
        ):
            with patch.object(sales_app, "call_ai_provider") as provider:
                configured_but_disabled = self.post(
                    "/insights",
                    {
                        "mode": "ai",
                        "question": "How are sales?",
                        "start": sales_app.date.today().isoformat(),
                        "end": sales_app.date.today().isoformat(),
                    },
                )
                provider.assert_not_called()
        self.assertIn(b"AI is disabled", configured_but_disabled.data)

    def test_ai_owner_consent_sends_only_aggregates_and_numbers_stay_app_computed(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post(
            "/sales",
            {
                "product_id": "1",
                "quantity": "1",
                "discount": "0",
                "paid": "30.50",
                "method": "Cash",
            },
        )
        provider_environment = {
            "AI_PROVIDER": "openai-compatible",
            "AI_API_KEY": "test-key-not-a-real-secret",
            "AI_BASE_URL": "https://provider.example/v1",
            "AI_MODEL": "test-model",
        }
        with patch.dict(os.environ, provider_environment):
            consent_response = self.post(
                "/settings",
                {
                    "settings_section": "ai",
                    "ai_insights_enabled": "1",
                    "ai_data_consent": sales_app.AI_CONSENT_VERSION,
                },
            )
            self.assertIn(b"AI insights enabled", consent_response.data)
            with patch.object(
                sales_app,
                "call_ai_provider",
                return_value="The recorded sales trend is improving.",
            ) as provider:
                response = self.post(
                    "/insights",
                    {
                        "mode": "ai",
                        "question": "How are sales for Mr Example Customer?",
                        "start": sales_app.date.today().isoformat(),
                        "end": sales_app.date.today().isoformat(),
                    },
                )
            provider.assert_called_once()
            topic, payload = provider.call_args.args
            self.assertEqual(topic, "sales_performance")
            self.assertEqual(payload["metrics"]["net_sales_minor_units"], 3050)
            self.assertNotIn("operating_expenses_minor_units", payload["metrics"])
            self.assertNotIn("aggregate_customer_amount_owed_minor_units", payload["metrics"])
            self.assertEqual(payload["currency"], "SLE")
            self.assertNotIn("question", payload)
            self.assertNotIn("business_name", payload)
            self.assertNotIn("customer_name", str(payload))
            self.assertNotIn("product_name", str(payload))
            self.assertNotIn("Mr Example Customer", str(payload))
            self.assertIn(b"The recorded sales trend is improving.", response.data)
            self.assertIn(b"SLE 30.50", response.data)
            self.assertIn(b"AI explanation", response.data)
            self.assertIn(b"not accounting advice", response.data)
            self.post("/settings", {"settings_section": "ai"})
            db = sales_app.connect_db()
            self.assertEqual(
                db.execute("SELECT ai_insights_enabled FROM businesses").fetchone()[0],
                0,
            )
            db.close()
            with patch.object(sales_app, "call_ai_provider") as provider:
                disabled_response = self.post(
                    "/insights",
                    {
                        "mode": "ai",
                        "question": "How are sales?",
                        "start": sales_app.date.today().isoformat(),
                        "end": sales_app.date.today().isoformat(),
                    },
                )
                provider.assert_not_called()
            self.assertIn(b"AI is disabled", disabled_response.data)

    def test_ai_numeric_claims_and_provider_failures_fall_back_to_local_insights(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post(
            "/sales",
            {
                "product_id": "1",
                "quantity": "1",
                "discount": "0",
                "paid": "30.50",
                "method": "Cash",
            },
        )
        with patch.dict(
            os.environ,
            {
                "AI_PROVIDER": "openai-compatible",
                "AI_API_KEY": "test-key",
                "AI_BASE_URL": "https://provider.example/v1",
                "AI_MODEL": "test-model",
            },
        ):
            self.post(
                "/settings",
                {
                    "settings_section": "ai",
                    "ai_insights_enabled": "1",
                    "ai_data_consent": sales_app.AI_CONSENT_VERSION,
                },
            )
            request_data = {
                "mode": "ai",
                "question": "How are sales?",
                "start": sales_app.date.today().isoformat(),
                "end": sales_app.date.today().isoformat(),
            }
            with patch.object(
                sales_app,
                "call_ai_provider",
                return_value="Revenue was SLE 999.00.",
            ):
                numeric_response = self.post("/insights", dict(request_data))
            self.assertIn(b"AI response contained unsupported figures", numeric_response.data)
            self.assertNotIn(b"SLE 999.00", numeric_response.data)
            self.assertIn(b"recorded net sales are SLE 30.50", numeric_response.data)
            with patch.object(
                sales_app,
                "call_ai_provider",
                side_effect=sales_app.AIProviderError(
                    "The AI provider could not be reached; local insights remain available."
                ),
            ) as provider:
                unavailable_response = self.post("/insights", dict(request_data))
        provider.assert_called_once()
        self.assertIn(b"local insights remain available", unavailable_response.data)
        self.assertIn(b"recorded net sales are SLE 30.50", unavailable_response.data)

    def test_openai_compatible_adapter_uses_environment_and_aggregate_payload_only(self):
        provider_environment = {
            "AI_PROVIDER": "openai-compatible",
            "AI_API_KEY": "test-key-not-a-real-secret",
            "AI_BASE_URL": "https://provider.example/v1",
            "AI_MODEL": "test-model",
        }
        with patch.dict(os.environ, provider_environment):
            with patch.object(sales_app.urllib.request, "build_opener") as build_opener:
                build_opener.return_value.open.return_value = BytesIO(
                    b'{"choices":[{"message":{"content":"Sales activity is available for review."}}]}'
                )
                narrative = sales_app.call_ai_provider(
                    "sales_performance",
                    {"data_period": {"start": "2026-01-01", "end": "2026-01-31"},
                     "metrics": {"net_sales_minor_units": 3050}},
                )
        self.assertEqual(narrative, "Sales activity is available for review.")
        provider_request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(provider_request.full_url, "https://provider.example/v1/chat/completions")
        self.assertEqual(
            provider_request.get_header("Authorization"),
            "Bearer test-key-not-a-real-secret",
        )
        request_json = json.loads(provider_request.data)
        provider_content = request_json["messages"][1]["content"]
        self.assertIn("net_sales_minor_units", provider_content)
        self.assertNotIn("question", provider_content)
        self.assertNotIn("owner_name", provider_content)

    def test_attendant_ai_permissions_and_payload_exclude_owner_finances(self):
        self.register_owner()
        self.add_product_and_stock()
        with patch.dict(
            os.environ,
            {
                "AI_PROVIDER": "openai-compatible",
                "AI_API_KEY": "test-key",
                "AI_BASE_URL": "https://provider.example/v1",
                "AI_MODEL": "test-model",
            },
        ):
            self.post(
                "/settings",
                {
                    "settings_section": "ai",
                    "ai_insights_enabled": "1",
                    "ai_data_consent": sales_app.AI_CONSENT_VERSION,
                },
            )
            self.post(
                "/team",
                {
                    "full_name": "AI Attendant",
                    "username": "ai-attendant",
                    "password": "secure-staff-password",
                },
            )
            with self.client.session_transaction() as browser_session:
                browser_session.clear()
            self.post("/login", {"username": "ai-attendant", "password": "secure-staff-password"})
            today = sales_app.date.today().isoformat()
            with patch.object(sales_app, "call_ai_provider") as provider:
                restricted = self.post(
                    "/insights",
                    {
                        "mode": "ai",
                        "question": "What is the business profit?",
                        "start": today,
                        "end": today,
                    },
                )
                provider.assert_not_called()
            self.assertIn(b"restricted to the Owner/Admin", restricted.data)
            self.post(
                "/sales",
                {
                    "product_id": "1",
                    "quantity": "1",
                    "discount": "0",
                    "paid": "30.50",
                    "method": "Cash",
                },
            )
            with patch.object(
                sales_app,
                "call_ai_provider",
                return_value="Your recorded sales activity is available for review.",
            ) as provider:
                self.post(
                    "/insights",
                    {
                        "mode": "ai",
                        "question": "How are my sales?",
                        "start": today,
                        "end": today,
                    },
                )
            payload = provider.call_args.args[1]
            self.assertEqual(payload["metrics"]["net_sales_minor_units"], 3050)
            self.assertNotIn("estimated_net_profit_minor_units", payload["metrics"])
            self.assertNotIn("operating_expenses_minor_units", payload["metrics"])
            self.assertNotIn("aggregate_customer_amount_owed_minor_units", payload["metrics"])

    def test_business_currency_change_is_locked_after_financial_transaction(self):
        self.register_owner()
        self.add_product_and_stock()
        settings = self.client.get("/settings")
        self.assertIn(b"Locked because financial transactions exist", settings.data)
        response = self.post(
            "/settings",
            {
                "name": "Renamed Trading",
                "business_type": "General trading",
                "phone": "076000000",
                "address": "Freetown",
                "currency": "USD",
            },
        )
        self.assertIn(b"Currency is locked", response.data)
        db = sales_app.connect_db()
        business = db.execute("SELECT name, currency FROM businesses").fetchone()
        self.assertEqual(tuple(business), ("Test Trading", "SLE"))
        db.close()

    def test_supplier_debt_adjustment_return_reduces_balance_and_stock(self):
        self.register_owner()
        self.add_product_and_stock()
        response = self.post(
            "/supplier-returns",
            {
                "purchase_id": "1",
                "quantity": "1",
                "adjustment_type": "debt_adjustment",
                "reason": "Damaged packaging accepted by supplier",
            },
        )
        self.assertIn(b"Supplier return recorded", response.data)
        self.assertIn(b"Download PDF", self.client.get("/supplier-return-receipts/1").data)
        self.assertTrue(
            self.client.get("/supplier-return-receipts/1.pdf").data.startswith(b"%PDF")
        )
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT stock FROM products").fetchone()[0], 9)
        returned = db.execute(
            "SELECT quantity, base_quantity, amount_minor FROM supplier_returns"
        ).fetchone()
        self.assertEqual(tuple(returned), (1, 1, 2025))
        db.close()
        supplier_page = self.client.get("/suppliers")
        self.assertIn(b"SLE 82.25", supplier_page.data)

    def test_supplier_refund_and_replacement_are_recorded_without_double_reducing_debt(self):
        self.register_owner()
        self.add_product_and_stock()
        response = self.post(
            "/supplier-returns",
            {
                "purchase_id": "1",
                "quantity": "1",
                "adjustment_type": "refund",
                "method": "Cash",
                "reason": "Supplier refunded one bag",
            },
        )
        self.assertIn(b"Supplier return recorded", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT stock FROM products").fetchone()[0], 9)
        self.assertEqual(
            db.execute(
                "SELECT SUM(amount_minor) FROM payments WHERE supplier_id=1 AND direction='received'"
            ).fetchone()[0],
            2025,
        )
        db.close()
        self.post(
            "/supplier-returns",
            {
                "purchase_id": "1",
                "quantity": "1",
                "adjustment_type": "replacement",
                "reason": "One bag replaced by supplier",
            },
        )
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT stock FROM products").fetchone()[0], 9)
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM supplier_returns").fetchone()[0],
            2,
        )
        db.close()

    def test_split_payment_total_cannot_exceed_sale_total(self):
        self.register_owner()
        self.add_product_and_stock()
        response = self.post(
            "/sales",
            {
                "product_id": "1",
                "quantity": "1",
                "discount": "0",
                "payment_amount": ["30.50", "0.01"],
                "payment_method": ["Cash", "Orange Money"],
            },
        )
        self.assertIn(b"Amount paid cannot exceed", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sales").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT stock FROM products").fetchone()[0], 10)
        db.close()

    def test_owner_pages_render(self):
        self.register_owner()
        for path in (
            "/",
            "/products",
            "/customers",
            "/suppliers",
            "/supplier-payments",
            "/sales-returns",
            "/purchases",
            "/sales",
            "/payments",
            "/expenses",
            "/reports",
            "/settings",
            "/team",
            "/audit",
            "/insights",
            "/exports/sales.csv",
            "/exports/inventory.csv",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_owner_can_load_presentation_sample_data_once_for_empty_business(self):
        self.register_owner()
        confirmation = self.client.get("/presentation-samples")
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn(b"Confirm before adding records", confirmation.data)
        self.assertIn(b"fictional", confirmation.data.lower())

        missing_confirmation = self.post("/presentation-samples", {})
        self.assertIn(b"Confirm the presentation sample data load", missing_confirmation.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)
        db.close()

        response = self.post("/presentation-samples", {"confirm_load": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"presentation sample data was loaded", response.data)

        db = sales_app.connect_db()
        business = db.execute("SELECT id FROM businesses").fetchone()
        business_id = business["id"]
        expected_counts = {
            "products": 3,
            "product_units": 3,
            "customers": 2,
            "suppliers": 1,
            "purchases": 3,
            "sales": 3,
            "payments": 6,
            "expenses": 2,
        }
        for table, expected in expected_counts.items():
            if table == "product_units":
                actual = db.execute(
                    """SELECT COUNT(*) FROM product_units
                       JOIN products ON products.id = product_units.product_id
                       WHERE products.business_id = ?""",
                    (business_id,),
                ).fetchone()[0]
            else:
                actual = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE business_id = ?",
                    (business_id,),
                ).fetchone()[0]
            self.assertEqual(actual, expected, table)
        self.assertEqual(
            db.execute(
                "SELECT COUNT(*) FROM products WHERE business_id = ? AND name LIKE 'DEMO - %'",
                (business_id,),
            ).fetchone()[0],
            3,
        )
        self.assertEqual(
            db.execute(
                """SELECT COUNT(*) FROM purchases
                   WHERE business_id = ? AND reference LIKE 'DEMO-PUR-%'""",
                (business_id,),
            ).fetchone()[0],
            3,
        )
        self.assertEqual(
            db.execute(
                """SELECT COUNT(*) FROM sales
                   WHERE business_id = ? AND reference LIKE 'DEMO-SAL-%'""",
                (business_id,),
            ).fetchone()[0],
            3,
        )
        self.assertEqual(
            db.execute(
                """SELECT COUNT(*) FROM audit_logs
                   WHERE business_id = ? AND entity = 'presentation_sample_data'
                     AND action = 'loaded'""",
                (business_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            db.execute(
                "SELECT SUM(total_minor) FROM sales WHERE business_id = ?",
                (business_id,),
            ).fetchone()[0],
            62500,
        )
        self.assertEqual(
            db.execute(
                "SELECT SUM(amount_minor) FROM expenses WHERE business_id = ?",
                (business_id,),
            ).fetchone()[0],
            13000,
        )
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

        second_load = self.post("/presentation-samples", {"confirm_load": "yes"})
        self.assertIn(b"already been loaded", second_load.data)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM products").fetchone()[0], 3)
        db.close()

    def test_presentation_sample_load_is_blocked_when_business_has_records(self):
        self.register_owner()
        self.post(
            "/products",
            {
                "name": "Existing product",
                "sku": "EXISTING-1",
                "unit": "each",
                "price": "2",
                "cost": "1",
                "stock": "0",
                "reorder_level": "0",
            },
        )
        response = self.post("/presentation-samples", {"confirm_load": "yes"})
        self.assertIn(b"no operational records", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)
        for table in ("customers", "suppliers", "purchases", "sales", "payments", "expenses"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        db.close()

    def test_attendant_cannot_load_presentation_sample_data(self):
        self.register_owner()
        self.post(
            "/team",
            {
                "full_name": "Presentation Attendant",
                "username": "presentation-attendant",
                "password": "secure-staff-password",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.post(
            "/login",
            {"username": "presentation-attendant", "password": "secure-staff-password"},
        )
        self.assertEqual(self.client.get("/presentation-samples").status_code, 403)
        denied = self.post("/presentation-samples", {"confirm_load": "yes"})
        self.assertEqual(denied.status_code, 403)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)
        db.close()

    def test_presentation_sample_records_are_scoped_to_the_requesting_business(self):
        self.register_owner("first-sample-owner")
        db = sales_app.connect_db()
        first_business_id = db.execute(
            "SELECT id FROM businesses WHERE name = 'Test Trading' ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        db.close()

        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.register_owner("second-sample-owner")
        db = sales_app.connect_db()
        second_business_id = db.execute(
            "SELECT id FROM businesses ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        db.close()

        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.post(
            "/login",
            {"username": "first-sample-owner", "password": "secure-test-password"},
        )
        self.post("/presentation-samples", {"confirm_load": "yes"})

        db = sales_app.connect_db()
        for table in ("products", "customers", "suppliers", "purchases", "sales", "payments", "expenses"):
            self.assertGreater(
                db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE business_id = ?",
                    (first_business_id,),
                ).fetchone()[0],
                0,
                table,
            )
            self.assertEqual(
                db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE business_id = ?",
                    (second_business_id,),
                ).fetchone()[0],
                0,
                table,
            )
        db.close()


    def test_sidebar_navigation_is_grouped_active_and_role_scoped(self):
        self.register_owner()
        owner_dashboard = self.client.get("/")
        self.assertIn(b"class=\"workspace-header\"", owner_dashboard.data)
        self.assertIn(b"Business workspace", owner_dashboard.data)
        self.assertIn(b"aria-label=\"Main navigation\"", owner_dashboard.data)
        self.assertIn(b">SELLING</span>", owner_dashboard.data)
        self.assertIn(b">STOCK</span>", owner_dashboard.data)
        self.assertIn(b"href=\"/settings\"", owner_dashboard.data)
        self.assertIn(b"aria-current=\"page\"", owner_dashboard.data)
        self.post(
            "/team",
            {
                "full_name": "Sidebar Attendant",
                "username": "sidebar-attendant",
                "password": "secure-staff-password",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.post("/login", {"username": "sidebar-attendant", "password": "secure-staff-password"})
        attendant_dashboard = self.client.get("/")
        self.assertIn(b"class=\"workspace-header\"", attendant_dashboard.data)
        self.assertIn(b"href=\"/sales\"", attendant_dashboard.data)
        self.assertIn(b"href=\"/insights\"", attendant_dashboard.data)
        self.assertNotIn(b"href=\"/settings\"", attendant_dashboard.data)
        self.assertNotIn(b"href=\"/reports\"", attendant_dashboard.data)
        sign_in = sales_app.app.test_client().get("/login")
        self.assertIn(b"class=\"login-layout\"", sign_in.data)
        self.assertNotIn(b"class=\"app-shell\"", sign_in.data)
        stylesheet = self.client.get("/static/style.css")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn(b"@media (max-width: 700px)", stylesheet.data)
        self.assertIn(b"overflow-x: auto", stylesheet.data)
        self.assertIn(b"background: #123d2e; color: #fbfaf4", stylesheet.data)
        self.assertIn(b".sidebar-link.active { background: #dcefe3; color: #174b35", stylesheet.data)
        self.assertIn(b".workspace-header", stylesheet.data)
        self.assertIn(b".stat-card:nth-child(4n + 3)", stylesheet.data)
        self.assertIn(b".sidebar, .workspace-header", stylesheet.data)
        self.assertNotIn(b"#111d3d", stylesheet.data)
        self.assertNotIn(b"#f8faff", stylesheet.data)
        self.assertNotIn(b"#f4f6fb", stylesheet.data)
        self.assertNotIn(b"rgb(4 15 45", stylesheet.data)
        stylesheet.close()

    def test_login_page_has_responsive_split_layout_and_auth_controls(self):
        response = sales_app.app.test_client().get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"class=\"login-layout\"", response.data)
        self.assertIn(b"name=\"_csrf\"", response.data)
        self.assertIn(b"name=\"username\"", response.data)
        self.assertIn(b"name=\"password\"", response.data)
        self.assertIn(b"Forgot your password?", response.data)
        self.assertIn(b"Create your business account", response.data)
        self.assertIn(b"Customers and credit", response.data)
        self.assertIn(b"Business records", response.data)
        self.assertNotIn(b"class=\"app-shell\"", response.data)

        stylesheet = self.client.get("/static/style.css")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn(b"linear-gradient(135deg, #0f5038 0%, #157451 54%, #0c6047 100%)", stylesheet.data)
        self.assertIn(b"@media (max-width: 820px)", stylesheet.data)
        self.assertIn(b"@media (max-width: 540px)", stylesheet.data)
        stylesheet.close()

    def test_supplier_debt_payment_updates_balance_and_has_receipt(self):
        self.register_owner()
        self.add_product_and_stock()
        db = sales_app.connect_db()
        balance = db.execute(
            """SELECT COALESCE(SUM(total_minor), 0) -
                      COALESCE((SELECT SUM(amount_minor) FROM payments
                                WHERE supplier_id = 1 AND direction = 'paid'), 0)
               FROM purchases WHERE supplier_id = 1"""
        ).fetchone()[0]
        self.assertEqual(balance, 10250)
        db.close()
        response = self.post(
            "/supplier-payments",
            {"supplier_id": "1", "amount": "50", "method": "Orange Money"},
        )
        self.assertIn(b"Supplier payment recorded", response.data)
        self.assertIn(b"Payment receipt", response.data)
        self.assertTrue(
            self.client.get("/supplier-payment-receipts/1.pdf").data.startswith(b"%PDF")
        )
        db = sales_app.connect_db()
        payment = db.execute(
            "SELECT amount_minor, method, direction FROM payments WHERE supplier_id = 1 ORDER BY id DESC"
        ).fetchone()
        self.assertEqual((payment["amount_minor"], payment["method"], payment["direction"]),
                         (5000, "Orange Money", "paid"))
        balance = db.execute(
            """SELECT COALESCE(SUM(total_minor), 0) -
                      COALESCE((SELECT SUM(amount_minor) FROM payments
                                WHERE supplier_id = 1 AND direction = 'paid'), 0)
               FROM purchases WHERE supplier_id = 1"""
        ).fetchone()[0]
        self.assertEqual(balance, 5250)
        db.close()

    def test_customer_payment_receipt_pdf_is_scoped_to_the_recording_user(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post("/customers", {"name": "Payment Receipt Buyer", "phone": "076222222"})
        self.post(
            "/sales",
            {
                "product_id": "1",
                "customer_id": "1",
                "quantity": "1",
                "discount": "0",
                "paid": "0",
                "method": "Cash",
            },
        )
        self.post(
            "/payments",
            {"customer_id": "1", "amount": "10", "method": "Cash"},
        )
        db = sales_app.connect_db()
        payment_id = db.execute(
            """SELECT id FROM payments
               WHERE customer_id = 1 AND direction = 'received'"""
        ).fetchone()[0]
        db.close()
        html_receipt = self.client.get(f"/payment-receipts/{payment_id}")
        self.assertEqual(html_receipt.status_code, 200)
        self.assertTrue(
            self.client.get(f"/payment-receipts/{payment_id}.pdf").data.startswith(b"%PDF")
        )
        self.post(
            "/team",
            {
                "full_name": "Receipt Attendant",
                "username": "receipt-attendant",
                "password": "secure-staff-password",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.post("/login", {"username": "receipt-attendant", "password": "secure-staff-password"})
        self.assertEqual(self.client.get(f"/payment-receipts/{payment_id}").status_code, 404)
        self.assertEqual(
            self.client.get(f"/payment-receipts/{payment_id}.pdf").status_code,
            404,
        )

    def test_supplier_payment_cannot_exceed_balance_or_repeat(self):
        self.register_owner()
        self.add_product_and_stock()
        payment_form = {
            "supplier_id": "1",
            "amount": "200",
            "method": "Cash",
            "request_key": "supplier-payment-too-large",
        }
        response = self.post("/supplier-payments", payment_form)
        self.assertIn(b"exceeds this supplier", response.data)
        payment_form.update(amount="10", request_key="one-supplier-payment")
        self.post("/supplier-payments", payment_form)
        response = self.post("/supplier-payments", payment_form)
        self.assertIn(b"already have been submitted", response.data)
        db = sales_app.connect_db()
        self.assertEqual(
            db.execute("SELECT COUNT(*) FROM payments WHERE supplier_id = 1 AND purchase_id IS NULL").fetchone()[0],
            1,
        )
        db.close()

    def test_resellable_return_refunds_and_restocks_atomically(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post(
            "/sales",
            {
                "product_id": "1", "quantity": "1", "discount": "0",
                "paid": "30.50", "method": "Cash",
            },
        )
        response = self.post(
            "/sales-returns",
            {
                "sale_id": "1", "quantity": "1", "condition": "resellable",
                "adjustment_type": "refund", "method": "Orange Money",
                "reason": "Customer returned unopened item",
            },
        )
        self.assertIn(b"Return recorded", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT stock FROM products WHERE id = 1").fetchone()[0], 10)
        sale_return = db.execute(
            "SELECT amount_minor, condition, adjustment_type FROM sales_returns"
        ).fetchone()
        self.assertEqual((sale_return["amount_minor"], sale_return["condition"],
                          sale_return["adjustment_type"]), (3050, "resellable", "refund"))
        refund = db.execute(
            "SELECT amount_minor, method, direction FROM payments WHERE sales_return_id = 1"
        ).fetchone()
        self.assertEqual((refund["amount_minor"], refund["method"], refund["direction"]),
                         (3050, "Orange Money", "paid"))
        db.close()

    def test_damaged_debt_adjustment_reduces_balance_without_restocks(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post("/customers", {"name": "Credit Customer", "phone": "076222222"})
        self.post(
            "/sales",
            {
                "product_id": "1", "customer_id": "1", "quantity": "2",
                "discount": "0", "paid": "0",
            },
        )
        response = self.post(
            "/sales-returns",
            {
                "sale_id": "1", "quantity": "1", "condition": "damaged",
                "adjustment_type": "debt_adjustment", "reason": "Damaged on delivery",
            },
        )
        self.assertIn(b"Return recorded", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT stock FROM products WHERE id = 1").fetchone()[0], 8)
        self.assertEqual(db.execute("SELECT total_minor - (SELECT SUM(amount_minor) FROM sales_returns) FROM sales WHERE id = 1").fetchone()[0], 3050)
        db.close()
        response = self.post(
            "/payments",
            {"customer_id": "1", "amount": "30.50", "method": "Cash"},
        )
        self.assertIn(b"Payment recorded", response.data)
        db = sales_app.connect_db()
        self.assertEqual(
            db.execute(
                """SELECT total_minor -
                          (SELECT SUM(amount_minor) FROM sales_returns) -
                          (SELECT SUM(amount_minor) FROM payments
                           WHERE customer_id = 1 AND direction = 'received')
                   FROM sales WHERE id = 1"""
            ).fetchone()[0],
            0,
        )
        db.close()

    def test_damaged_replacement_consumes_stock_and_records_replacement_cost(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post(
            "/sales",
            {
                "product_id": "1", "quantity": "1", "discount": "0",
                "paid": "30.50", "method": "Cash",
            },
        )
        response = self.post(
            "/sales-returns",
            {
                "sale_id": "1", "quantity": "1", "condition": "damaged",
                "adjustment_type": "replacement", "reason": "Damaged product replaced",
            },
        )
        self.assertIn(b"Return recorded", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT stock FROM products WHERE id = 1").fetchone()[0], 8)
        self.assertEqual(
            db.execute("SELECT replacement_cost_minor FROM sales_returns").fetchone()[0],
            2025,
        )
        db.close()

    def test_return_quantity_cannot_exceed_original_sale(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post(
            "/sales",
            {
                "product_id": "1", "quantity": "1", "discount": "0",
                "paid": "30.50", "method": "Cash",
            },
        )
        form = {
            "sale_id": "1", "quantity": "1", "condition": "resellable",
            "adjustment_type": "refund", "reason": "Return",
        }
        self.post("/sales-returns", form)
        response = self.post("/sales-returns", form)
        self.assertIn(b"Only 0 can still be returned", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sales_returns").fetchone()[0], 1)
        db.close()

    @patch.dict(
        os.environ,
        {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM": "noreply@example.test",
            "PUBLIC_BASE_URL": "https://trade.example.test",
        },
    )
    @patch.object(sales_app, "send_password_reset_email")
    def test_password_reset_link_is_one_time_and_invalidates_sessions(self, send_email):
        self.register_owner()
        response = self.post("/forgot-password", {"email": "owner@example.test"})
        self.assertIn(b"reset instructions will be sent", response.data)
        reset_url = send_email.call_args.args[1]
        reset_path = urlsplit(reset_url).path
        response = self.post(
            reset_path,
            {
                "password": "replacement-secure-password",
                "confirm_password": "replacement-secure-password",
            },
        )
        self.assertIn(b"Password updated", response.data)
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("user_id", browser_session)
        response = self.post(
            reset_path,
            {
                "password": "another-secure-password",
                "confirm_password": "another-secure-password",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.post("/login", {"username": "owner", "password": "secure-test-password"})
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("user_id", browser_session)
        self.post(
            "/login",
            {"username": "owner", "password": "replacement-secure-password"},
        )
        with self.client.session_transaction() as browser_session:
            self.assertIn("user_id", browser_session)

    def test_password_recovery_does_not_send_without_mail_configuration(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in ("SMTP_HOST", "SMTP_FROM", "PUBLIC_BASE_URL"):
                os.environ.pop(key, None)
            self.register_owner()
            response = self.client.get("/forgot-password")
            self.assertIn(b"not configured", response.data)
            response = self.post("/forgot-password", {"email": "owner@example.test"})
            self.assertIn(b"reset instructions will be sent", response.data)
        db = sales_app.connect_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM password_reset_tokens").fetchone()[0], 0)
        db.close()

    @patch.dict(
        os.environ,
        {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_FROM": "noreply@example.test",
            "PUBLIC_BASE_URL": "https://trade.example.test",
        },
    )
    @patch.object(sales_app, "send_password_reset_email")
    def test_password_recovery_is_rate_limited_per_email(self, send_email):
        self.register_owner()
        for _ in range(4):
            self.post("/forgot-password", {"email": "owner@example.test"})
        self.assertEqual(send_email.call_count, 3)
        db = sales_app.connect_db()
        self.assertEqual(
            db.execute("SELECT request_count FROM password_reset_requests").fetchone()[0],
            3,
        )
        db.close()

    def test_csv_export_is_business_scoped_and_neutralizes_formulas(self):
        self.register_owner()
        self.post(
            "/products",
            {
                "name": "=HYPERLINK(\"https://example.test\")",
                "sku": "FORMULA-1",
                "unit": "piece",
                "price": "5",
                "cost": "1",
                "stock": "1",
                "reorder_level": "0",
            },
        )
        response = self.client.get("/exports/inventory.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        self.assertIn(b"'=HYPERLINK", response.data)
        self.assertIn(b"FORMULA-1", response.data)

    def test_attendant_cannot_download_owner_exports(self):
        self.register_owner()
        self.post(
            "/team",
            {
                "full_name": "Export Test Attendant",
                "username": "export-attendant",
                "password": "secure-staff-password",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.post("/login", {"username": "export-attendant", "password": "secure-staff-password"})
        self.assertEqual(self.client.get("/exports/customers.csv").status_code, 403)

    def test_sales_exports_apply_date_range_and_reject_partial_ranges(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post(
            "/sales",
            {
                "product_id": "1",
                "quantity": "1",
                "discount": "0",
                "paid": "30.50",
                "method": "Cash",
            },
        )
        db = sales_app.connect_db()
        db.execute("UPDATE sales SET created_at = '2026-01-04T10:00:00'")
        db.commit()
        db.close()
        outside = self.client.get(
            "/exports/sales.csv?start=2026-01-01&end=2026-01-03"
        )
        inside = self.client.get(
            "/exports/sales.csv?start=2026-01-04&end=2026-01-05"
        )
        self.assertNotIn(b"SAL-", outside.data)
        self.assertIn(b"SAL-", inside.data)
        self.assertEqual(self.client.get("/exports/sales.csv?start=2026-01-01").status_code, 400)
        purchases = self.client.get(
            "/exports/purchases.csv?start=2026-01-01&end=2026-01-03"
        )
        self.assertNotIn(b"PUR-", purchases.data)
        self.assertIn(b"PUR-", self.client.get("/exports/purchases.csv").data)

    def test_notifications_are_persistent_and_scoped_by_user_role(self):
        self.register_owner()
        self.add_product_and_stock()
        db = sales_app.connect_db()
        db.execute("UPDATE products SET reorder_level = 9 WHERE id = 1")
        db.commit()
        db.close()
        self.post(
            "/team",
            {
                "full_name": "Notification Attendant",
                "username": "notification-attendant",
                "password": "secure-staff-password",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.post(
            "/login",
            {"username": "notification-attendant", "password": "secure-staff-password"},
        )
        self.post(
            "/sales",
            {
                "product_id": "1",
                "quantity": "2",
                "discount": "0",
                "paid": "61",
                "method": "Cash",
            },
        )
        attendant_notifications = self.client.get("/notifications")
        self.assertIn(b"Sale recorded", attendant_notifications.data)
        self.assertIn(b"Low stock", attendant_notifications.data)
        self.assertNotIn(b"outstanding supplier balance", attendant_notifications.data)
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.post("/login", {"username": "owner", "password": "secure-test-password"})
        owner_notifications = self.client.get("/notifications")
        self.assertIn(b"Low stock", owner_notifications.data)
        self.assertNotIn(b"Sale recorded", owner_notifications.data)
        db = sales_app.connect_db()
        self.assertEqual(
            db.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE type = 'sale_confirmation' AND user_id = 2"""
            ).fetchone()[0],
            1,
        )
        owner_low_stock = db.execute(
            """SELECT id FROM notifications
               WHERE type = 'low_stock' AND user_id = 1 AND read_at IS NULL"""
        ).fetchone()[0]
        db.close()
        self.post(f"/notifications/{owner_low_stock}/read", {})
        db = sales_app.connect_db()
        self.assertIsNotNone(
            db.execute(
                "SELECT read_at FROM notifications WHERE id = ?",
                (owner_low_stock,),
            ).fetchone()["read_at"]
        )
        db.close()

    def test_due_date_notifications_are_owner_only_and_show_account_context(self):
        self.register_owner()
        self.add_product_and_stock()
        due_date = (sales_app.date.today() - sales_app.timedelta(days=1)).isoformat()
        self.post("/customers", {"name": "Overdue Customer"})
        self.post(
            "/sales",
            {
                "product_id": "1",
                "customer_id": "1",
                "quantity": "1",
                "discount": "0",
                "paid": "0",
                "method": "Cash",
                "due_date": due_date,
            },
        )
        self.post(
            "/purchases",
            {
                "product_id": "1",
                "supplier_id": "1",
                "quantity": "1",
                "unit_cost": "20",
                "paid": "0",
                "method": "Cash",
                "due_date": due_date,
            },
        )
        owner_notifications = self.client.get("/notifications")
        self.assertIn(b"Customer payment overdue", owner_notifications.data)
        self.assertIn(b"Supplier payment overdue", owner_notifications.data)
        self.post(
            "/team",
            {
                "full_name": "Due Date Attendant",
                "username": "due-date-attendant",
                "password": "secure-staff-password",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.post("/login", {"username": "due-date-attendant", "password": "secure-staff-password"})
        self.assertNotIn(b"Customer payment overdue", self.client.get("/notifications").data)
        self.assertNotIn(b"Supplier payment overdue", self.client.get("/notifications").data)

    def test_owner_insights_use_recorded_sales_returns_and_expenses(self):
        self.register_owner()
        self.add_product_and_stock()
        self.post("/customers", {"name": "Insight Customer", "phone": "076222222"})
        self.post(
            "/sales",
            {
                "product_id": "1", "customer_id": "1", "quantity": "1",
                "discount": "0", "paid": "30.50", "method": "Cash",
            },
        )
        self.post(
            "/sales-returns",
            {
                "sale_id": "1", "quantity": "1", "condition": "resellable",
                "adjustment_type": "refund", "method": "Cash", "reason": "Refund test",
            },
        )
        response = self.post(
            "/insights",
            {
                "question": "How much profit did we make?",
                "start": "2020-01-01",
                "end": "2035-01-01",
            },
        )
        self.assertIn(b"estimated net profit before tax is SLE 0.00", response.data)
        self.assertIn(b"Run-out estimates assume", response.data)
        self.assertIn(b"Customer balances", response.data)
        db = sales_app.connect_db()
        returned_cost = db.execute(
            "SELECT cost_recovered_minor FROM sales_returns"
        ).fetchone()[0]
        self.assertEqual(returned_cost, 2025)
        db.close()
        customer_page = self.client.get("/customers")
        self.assertIn(b"SLE 0.00", customer_page.data)
        dashboard = self.client.get("/")
        self.assertIn(b"Sales today", dashboard.data)
        report = self.client.get("/reports")
        self.assertEqual(report.status_code, 200)

    def test_attendant_insights_and_dashboard_hide_owner_financial_data(self):
        self.register_owner()
        self.post(
            "/team",
            {
                "full_name": "Insight Attendant",
                "username": "insight-attendant",
                "password": "secure-staff-password",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.post(
            "/login",
            {"username": "insight-attendant", "password": "secure-staff-password"},
        )
        response = self.post(
            "/insights",
            {"question": "What profit did the business make?", "start": "2026-01-01", "end": "2026-12-31"},
        )
        self.assertIn(b"restricted to the Owner/Admin", response.data)
        response = self.post(
            "/insights",
            {"question": "Which customers owe us money?", "start": "2026-01-01", "end": "2026-12-31"},
        )
        self.assertIn(b"Customer and supplier balances are restricted", response.data)
        dashboard = self.client.get("/")
        self.assertNotIn(b"Gross profit today", dashboard.data)
        self.assertNotIn(b"Customer balances", dashboard.data)

    def test_attendant_cannot_access_owner_reports_or_expenses(self):
        self.register_owner()
        response = self.post(
            "/team",
            {
                "full_name": "Sales Attendant",
                "username": "attendant",
                "password": "secure-staff-password",
            },
        )
        self.assertIn(b"account created", response.data.lower())

        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        response = self.post(
            "/login",
            {"username": "attendant", "password": "secure-staff-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/reports").status_code, 403)
        self.assertEqual(self.client.get("/expenses").status_code, 403)
        self.assertEqual(self.client.get("/settings").status_code, 403)

    def test_business_data_isolation(self):
        self.register_owner("first-owner")
        self.post(
            "/products",
            {
                "name": "First business item",
                "sku": "ONE",
                "unit": "piece",
                "price": "5",
                "cost": "1",
                "stock": "1",
                "reorder_level": "0",
            },
        )
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        self.register_owner("second-owner")
        self.post(
            "/products",
            {
                "name": "Second business item",
                "sku": "TWO",
                "unit": "piece",
                "price": "7",
                "cost": "2",
                "stock": "1",
                "reorder_level": "0",
            },
        )
        response = self.client.get("/products")
        self.assertIn(b"Second business item", response.data)
        self.assertNotIn(b"First business item", response.data)


if __name__ == "__main__":
    unittest.main()
