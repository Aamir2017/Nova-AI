"""
actions/actions.py — Enterprise-Grade Custom Actions + Auth API (Phase 1 + Phase 2)
===================================================================================
"""
import os
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Text, Optional
from urllib.parse import urlparse

from rasa_sdk import Action, Tracker, FormValidationAction
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet, AllSlotsReset
from rasa_sdk.types import DomainDict

import mysql.connector
from mysql.connector import Error
import requests

from fpdf import FPDF
import datetime

# ═══════════════════════════════════════════════════════════════════════════════
#  SHARED DATABASE CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "your_password",
    "port": 3306,
    "database": "ecommerce_db",
}


def get_db_connection():
    try:
        return mysql.connector.connect(**DB_CONFIG)
    except Error as e:
        print(f"[DB ERROR] Connection failed: {e}")
        return None


def _tier_to_role(tier: str, email: str) -> str:
    """Map `tier` column ('Standard', 'Prime', 'Admin') to frontend `role` ('admin' | 'customer')."""
    if (tier and tier.lower() == "admin") or (email and email.lower().endswith("@nova.com") and not email.lower().startswith("aamir")):
        return "admin"
    return "customer"


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP VERIFICATION — Accounts & Coupons Tables Ready
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_accounts_table():
    conn = get_db_connection()
    if conn is None:
        print("[ACCOUNTS] Could not connect — skipping table setup.")
        return

    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                user_id INT AUTO_INCREMENT PRIMARY KEY,
                user_name VARCHAR(50) NOT NULL UNIQUE,
                email VARCHAR(100) NOT NULL UNIQUE,
                password VARCHAR(255) NOT NULL,
                tier ENUM('Standard', 'Prime', 'Admin') DEFAULT 'Standard',
                wallet_balance DECIMAL(10, 2) DEFAULT 0.00,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB;
        """)
        cursor.execute("""
            INSERT IGNORE INTO accounts (user_name, email, password, tier, wallet_balance)
            VALUES ('admin', 'admin@nova.com', 'admin123', 'Admin', 500.00);
        """)
        conn.commit()
        print("[ACCOUNTS] Enterprise `accounts` table verified and ready.")

    except Error as e:
        print(f"[ACCOUNTS] Setup error: {e}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH HTTP SERVER — daemon thread on port 5056
# ═══════════════════════════════════════════════════════════════════════════════

class _AuthHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[AUTH :{self.server.server_port}] {format % args}")

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/auth/login":
            self._handle_login()
        elif path == "/auth/signup":
            self._handle_signup()
        else:
            self._send_json(404, {"success": False, "error": "Endpoint not found."})

    def _handle_login(self):
        data = self._read_body()
        identifier = (data.get("identifier") or "").strip()
        password   = (data.get("password")   or "").strip()

        if not identifier or not password:
            self._send_json(400, {"success": False, "error": "identifier and password are required."})
            return

        conn = get_db_connection()
        if conn is None:
            self._send_json(503, {"success": False, "error": "Database unavailable."})
            return

        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT user_name, email, tier, wallet_balance
                FROM accounts
                WHERE (LOWER(email) = LOWER(%s) OR LOWER(user_name) = LOWER(%s))
                  AND password = %s
                LIMIT 1;
            """, (identifier, identifier, password))
            account = cursor.fetchone()

            if not account:
                self._send_json(401, {"success": False, "error": "Invalid credentials."})
                return

            cursor.execute("""
                SELECT order_id FROM orders
                WHERE LOWER(user_name) = LOWER(%s)
                ORDER BY order_id;
            """, (account["user_name"],))
            order_ids = [row["order_id"] for row in cursor.fetchall()]

            role = _tier_to_role(account["tier"], account["email"])

            self._send_json(200, {
                "success": True,
                "user": {
                    "user_name":      account["user_name"],
                    "email":          account["email"],
                    "role":           role,
                    "tier":           account["tier"],
                    "wallet_balance": float(account["wallet_balance"] or 0.0),
                    "order_ids":      order_ids,
                }
            })

        except Error as e:
            print(f"[AUTH] Login DB error: {e}")
            self._send_json(500, {"success": False, "error": "Internal server error."})
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

    def _handle_signup(self):
        data      = self._read_body()
        user_name = (data.get("user_name") or "").strip()
        email     = (data.get("email")     or "").strip().lower()
        password  = (data.get("password")  or "").strip()

        if not user_name or not email or not password:
            self._send_json(400, {"success": False, "error": "user_name, email, and password are required."})
            return

        tier = "Admin" if email.endswith("@nova.com") else "Standard"
        role = _tier_to_role(tier, email)

        conn = get_db_connection()
        if conn is None:
            self._send_json(503, {"success": False, "error": "Database unavailable."})
            return

        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                "SELECT user_id FROM accounts WHERE LOWER(user_name) = LOWER(%s);",
                (user_name,)
            )
            existing = cursor.fetchone()

            if existing:
                cursor.execute("""
                    UPDATE accounts SET email = %s, password = %s, tier = %s
                    WHERE LOWER(user_name) = LOWER(%s);
                """, (email, password, tier, user_name))
            else:
                cursor.execute("""
                    INSERT INTO accounts (user_name, email, password, tier, wallet_balance)
                    VALUES (%s, %s, %s, %s, 0.00);
                """, (user_name, email, password, tier))

            conn.commit()

            self._send_json(200, {
                "success": True,
                "user": {
                    "user_name":      user_name,
                    "email":          email,
                    "role":           role,
                    "tier":           tier,
                    "wallet_balance": 0.0,
                    "order_ids":      [],
                }
            })

        except Error as e:
            err_msg = str(e)
            if "1062" in err_msg or "Duplicate" in err_msg:
                self._send_json(409, {"success": False, "error": "Email already registered to another account."})
            else:
                print(f"[AUTH] Signup DB error: {e}")
                self._send_json(500, {"success": False, "error": "Internal server error."})
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()


def _start_auth_server():
    try:
        class _ReusableServer(HTTPServer):
            allow_reuse_address = True

        server = _ReusableServer(("localhost", 5056), _AuthHandler)
        print("[AUTH] Auth server listening on http://localhost:5056")
        server.serve_forever()
    except Exception as e:
        print(f"[AUTH] Failed to start auth server: {e}")


_setup_accounts_table()
_auth_thread = threading.Thread(target=_start_auth_server, daemon=True)
_auth_thread.start()


# ═══════════════════════════════════════════════════════════════════════════════
#  SHARED RBAC HELPER — verify user from metadata against accounts table
# ═══════════════════════════════════════════════════════════════════════════════

def _get_verified_user(tracker: Tracker) -> dict:                                            
    metadata   = tracker.latest_message.get("metadata", {}) or {}
    meta_email = (metadata.get("email") or "").strip().lower()
    meta_role  = (metadata.get("role")  or "customer").strip().lower()
    meta_name  = (metadata.get("user_name") or "").strip()

    result = {
        "user_name":      meta_name or "aamir",
        "email":          meta_email,
        "role":           meta_role,
        "tier":           "Admin" if meta_role == "admin" else "Prime",
        "wallet_balance": 0.0,
        "is_admin":       meta_role == "admin",
        "verified":       False,
    }

    if not meta_email and not meta_name:
        return result

    conn = get_db_connection()
    if conn is None:
        return result

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT user_name, email, tier, wallet_balance
            FROM accounts
            WHERE LOWER(email) = %s OR LOWER(user_name) = %s
            LIMIT 1;
        """, (meta_email, meta_name.lower()))
        account = cursor.fetchone()
        if account:
            role = _tier_to_role(account["tier"], account["email"])
            result["user_name"]      = account["user_name"]
            result["email"]          = account["email"]
            result["tier"]           = account["tier"]
            result["wallet_balance"] = float(account["wallet_balance"] or 0.0)
            result["role"]           = role
            result["is_admin"]       = (account["tier"] == "Admin")
            result["verified"]       = True
    except Error:
        pass
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTION 1: PRODUCT SEARCH — Supports Semantic Search + NLU Roles/Groups/Lookups
# ═══════════════════════════════════════════════════════════════════════════════

class ActionSearchProduct(Action):
    def name(self) -> Text:
        return "action_search_product"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        user = _get_verified_user(tracker)
        user_message = tracker.latest_message.get("text", "")
        entities = tracker.latest_message.get("entities", [])

        min_price = tracker.get_slot("min_price")
        max_price = tracker.get_slot("max_price")
        brand = tracker.get_slot("brand")

        grouped_requests = {}

        for ent in entities:
            ent_type = ent.get("entity")
            ent_val  = ent.get("value")
            ent_role = ent.get("role")
            ent_grp  = ent.get("group")

            if ent_type == "price" and ent_val:
                cleaned_val = re.sub(r"[^\d.]", "", str(ent_val))
                if cleaned_val:
                    if ent_role == "min":
                        min_price = float(cleaned_val)
                    elif ent_role == "max":
                        max_price = float(cleaned_val)

            if ent_type == "brand" and ent_val:
                brand = ent_val

            if ent_grp:
                grouped_requests.setdefault(ent_grp, {})[ent_type] = ent_val

        msg_lower = user_message.lower()

        if min_price is None and max_price is None:
            between_match = re.search(r"(?:between|from)\s*\$?(\d+(?:\.\d+)?)\s*(?:and|to|-)\s*\$?(\d+(?:\.\d+)?)", msg_lower)
            under_match   = re.search(r"(?:under|below|less than|max)\s*\$?(\d+(?:\.\d+)?)", msg_lower)
            above_match   = re.search(r"(?:above|over|more than|min)\s*\$?(\d+(?:\.\d+)?)", msg_lower)

            if between_match:
                min_price = float(between_match.group(1))
                max_price = float(between_match.group(2))
            elif under_match:
                max_price = float(under_match.group(1))
            elif above_match:
                min_price = float(above_match.group(1))

        if not grouped_requests:
            group_matches = re.findall(r"\b(\d+)\s+([a-zA-Z0-9\-\s]{3,25}?)(?=\s+and\s+\d+|\?|$)", user_message, re.IGNORECASE)
            if len(group_matches) >= 2:
                for idx, (qty, prod_name) in enumerate(group_matches, 1):
                    grouped_requests[str(idx)] = {
                        "quantity": qty.strip(),
                        "product_name": prod_name.strip()
                    }

        params = {"q": user_message}
        if min_price is not None:
            params["min_price"] = float(re.sub(r"[^\d.]", "", str(min_price)))
        if max_price is not None:
            params["max_price"] = float(re.sub(r"[^\d.]", "", str(max_price)))
        if brand:
            params["brand"] = brand

        try:
            response = requests.get("http://127.0.0.1:5000/search", params=params, timeout=10)
            products = response.json()
        except requests.exceptions.RequestException:
            dispatcher.utter_message(text="⚠️ The semantic search service is currently offline.")
            return []

        if not products:
            dispatcher.utter_message(text=f"Sorry, I couldn't find any products matching '{user_message}' within those filters. 😕")
            return [SlotSet("min_price", None), SlotSet("max_price", None), SlotSet("brand", None), SlotSet("user_tier", user["tier"])]

        
        header = "Here are the best options I found matching your request:\n"
        filters_desc = []
        if brand:
            filters_desc.append(f"from **{brand}**")
        if "min_price" in params or "max_price" in params:
            lo = f"${params.get('min_price', 0):.0f}"
            hi = f"${params['max_price']:.0f}" if "max_price" in params else "any price"
            filters_desc.append(f"priced between {lo} and {hi}")
            
        if filters_desc:
            header = f"Here are the top products " + " ".join(filters_desc) + ":\n"

        response_text = header
        for i, item in enumerate(products, 1):
            stock_qty = item.get("stock_quantity", item.get("stock", 0))
            if user["is_admin"]:
                stock_status = f"In stock ({stock_qty} units | SKU: {item.get('sku', 'N/A')})" if stock_qty > 0 else "Out of stock (0 units)"
            else:
                stock_status = "In stock ✅" if stock_qty > 0 else "Out of stock ❌"

            brand_label = f"[{item.get('brand')}] " if item.get("brand") else ""
            rating_label = f" ⭐ {item.get('rating', 4.5)}" if item.get("rating") else ""
            warranty_label = f" • 🛡️ {item.get('warranty_months', 12)}M Warranty" if item.get("warranty_months") else ""

            response_text += (
                f"\n**{i}. {brand_label}{item['name']}**{rating_label}\n"
                f"• Category: {item['category']}{warranty_label}\n"
                f"• Price: **${item['price']:.2f}**\n"
                f"• Availability: {stock_status}\n"
            )

       
        buttons = []
        if len(products) >= 2:
            p1, p2 = products[0]["name"], products[1]["name"]
            buttons.append({
                "title": f"⚖️ Compare #{1} vs #{2}",
                "payload": f'/compare_products{{"product_name": "{p1}"}}'
            })
        buttons.append({
            "title": "🎟️ Check Promo Coupons",
            "payload": "/check_coupons"
        })

        dispatcher.utter_message(text=response_text, buttons=buttons)
        return [
            SlotSet("min_price", None),
            SlotSet("max_price", None),
            SlotSet("brand", None),
            SlotSet("user_tier", user["tier"]),
        ]


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTION 2: SIDE-BY-SIDE SPEC COMPARISON ENGINE (Entity Roles + JSON Specs)
# ═══════════════════════════════════════════════════════════════════════════════

class ActionCompareProducts(Action):
    def name(self) -> Text:
        return "action_compare_products"

    def _match_product(self, query_term: str, catalog: list) -> Optional[dict]:
        if not query_term:
            return None
        q = query_term.lower().strip()
       
        best_item, best_score = None, 0
        for item in catalog:
            full_str = f"{item['brand']} {item['name']} {item['category']}".lower()
            score = 0
            if q in full_str:
                score += 5
            for token in re.findall(r"[a-z0-9]+", q):
                if token in ("compare", "with", "and", "vs", "between", "the", "or"):
                    continue
                if token in item["name"].lower():
                    score += 3
                elif token in item["brand"].lower():
                    score += 3
                elif token in item["category"].lower():
                    score += 1
            if score > best_score:
                best_score = score
                best_item = item
        return best_item

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        user = _get_verified_user(tracker)
        user_msg = tracker.latest_message.get("text", "")
        entities = tracker.latest_message.get("entities", [])

        comp_1 = tracker.get_slot("compare_item_1")
        comp_2 = tracker.get_slot("compare_item_2")

        for ent in entities:
            if ent.get("entity") == "product_name":
                if ent.get("role") == "compare_1":
                    comp_1 = ent.get("value")
                elif ent.get("role") == "compare_2":
                    comp_2 = ent.get("value")

        
        if not comp_1 or not comp_2:
            m = re.search(
                r"(?:compare|between|better)\s+([a-zA-Z0-9\-\"\s]+?)\s+(?:with|and|vs\.?|versus|or)\s+([a-zA-Z0-9\-\"\s\?]+)",
                user_msg, re.IGNORECASE
            )
            if m:
                comp_1 = comp_1 or m.group(1).strip(" ?.")
                comp_2 = comp_2 or m.group(2).strip(" ?.")

        conn = get_db_connection()
        if conn is None:
            dispatcher.utter_message(text="⚠️ Database connection unavailable for spec comparison.")
            return []

        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT product_id, sku, name, brand, category, price, discount_percent,
                       stock_quantity, rating, warranty_months, specs_json
                FROM products;
            """)
            catalog = cursor.fetchall()
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

        item1 = self._match_product(comp_1, catalog)
        item2 = self._match_product(comp_2, catalog)

        
        if not item1 or not item2 or item1["product_id"] == item2["product_id"]:
            monitors = [p for p in catalog if p["category"].lower() == "monitors"]
            if len(monitors) >= 2:
                item1, item2 = monitors[0], monitors[1]
            elif len(catalog) >= 2:
                item1, item2 = catalog[0], catalog[1]

        specs1 = json.loads(item1["specs_json"]) if isinstance(item1.get("specs_json"), str) else (item1.get("specs_json") or {})
        specs2 = json.loads(item2["specs_json"]) if isinstance(item2.get("specs_json"), str) else (item2.get("specs_json") or {})

        all_spec_keys = list(dict.fromkeys(list(specs1.keys()) + list(specs2.keys())))
        spec_lines = []
        for k in all_spec_keys:
            label = k.replace("_", " ").title()
            v1 = specs1.get(k, "—")
            v2 = specs2.get(k, "—")
            spec_lines.append(f"• **{label}:** `{v1}`  **vs.**  `{v2}`")

        specs_block = "\n".join(spec_lines) if spec_lines else "• Standard hardware specifications apply."

        # Automated AI Buying Verdict
        p1_price, p2_price = float(item1["price"]), float(item2["price"])
        p1_rate, p2_rate   = float(item1["rating"]), float(item2["rating"])
        cheaper = item1["name"] if p1_price < p2_price else item2["name"]
        price_diff = abs(p1_price - p2_price)
        top_rated = item1["name"] if p1_rate >= p2_rate else item2["name"]

        msg = (
            f"⚖️ **Product Specification Comparison**\n\n"
            f"🔹 **{item1['brand']} {item1['name']}**\n"
            f"   • Price: **${p1_price:.2f}** ({item1['discount_percent']}% off) | Rating: ⭐ {p1_rate}\n\n"
            f"🔸 **{item2['brand']} {item2['name']}**\n"
            f"   • Price: **${p2_price:.2f}** ({item2['discount_percent']}% off) | Rating: ⭐ {p2_rate}\n\n"
            f"📋 **Key Specifications:**\n"
            f"{specs_block}\n\n"
            f"💡 **AI Recommendation:** **{top_rated}** offers higher customer satisfaction, whereas **{cheaper}** provides better value by saving you **${price_diff:.2f}**."
        )

        buttons = [
            {
                "title": f"🎟️ Check PRIME30 on {item1['brand']}",
                "payload": f'/check_coupons{{"coupon_code": "PRIME30", "product_name": "{item1["name"]}"}}'
            },
            {
                "title": "🎟️ View All Promo Codes",
                "payload": "/check_coupons"
            }
        ]

        dispatcher.utter_message(text=msg, buttons=buttons)
        return [
            SlotSet("compare_item_1", None),
            SlotSet("compare_item_2", None),
            SlotSet("user_tier", user["tier"]),
        ]


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTION 3: TIER-AWARE PROMO COUPON & EFFECTIVE PRICE ENGINE (`coupons` table)
# ═══════════════════════════════════════════════════════════════════════════════

class ActionCheckCoupons(Action):
    def name(self) -> Text:
        return "action_check_coupons"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        user = _get_verified_user(tracker)
        user_msg = tracker.latest_message.get("text", "")
        coupon_code = tracker.get_slot("coupon_code")
        product_name = tracker.get_slot("product_name")

        
        if not coupon_code:
            m = re.search(r"\b(WELCOME10|NOVA20|PRIME30)\b", user_msg, re.IGNORECASE)
            if m:
                coupon_code = m.group(1).upper()

        conn = get_db_connection()
        if conn is None:
            dispatcher.utter_message(text="⚠️ Unable to reach the promotions database right now.")
            return []

        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM coupons WHERE is_active = 1;")
            coupons = cursor.fetchall()
            cursor.execute("SELECT name, brand, price, category FROM products;")
            products = cursor.fetchall()
        except Error as e:
            print(f"[COUPONS ERROR] {e}")
            coupons, products = [], []
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

        
        matched_product = None
        search_target = f"{product_name or ''} {user_msg}".lower()

        for p in products:
            p_name = p.get('name', '').lower()
            p_brand = p.get('brand', '').lower()
            if "ultragear" in search_target and "ultragear" in p_name:
                matched_product = p
                break
            elif "odyssey" in search_target and "odyssey" in p_name:
                matched_product = p
                break
            elif "sony" in search_target and ("sony" in p_name or "sony" in p_brand):
                matched_product = p
                break
            elif "anker" in search_target and ("anker" in p_name or "anker" in p_brand):
                matched_product = p
                break

       
        if not matched_product:
            try:
                response = requests.post("http://localhost:5000/search", json={"query": search_target}, timeout=2)
                if response.status_code == 200:
                    vector_results = response.json().get("results", [])
                    if vector_results:
                        top_match_name = vector_results[0].get("name", "").lower()
                        matched_product = next((p for p in products if p.get("name", "").lower() in top_match_name or top_match_name in p.get("name", "").lower()), None)
            except Exception as e:
                print(f"[VECTOR API ERROR] {e}")

        
        if coupon_code:
            code_upper = coupon_code.strip().upper()
            target_coupon = next((c for c in coupons if str(c.get("coupon_code", "")).upper() == code_upper), None)

            if not target_coupon:
                dispatcher.utter_message(text=f"❌ Promo code **`{code_upper}`** is invalid or expired.")
                return [SlotSet("coupon_code", None), SlotSet("product_name", None), SlotSet("user_tier", user["tier"])]

            pct = float(target_coupon.get("discount_percent", 10))
            min_amt = float(target_coupon.get("min_order_value", 0))
            req_tier = str(target_coupon.get("required_tier", "All"))

            # Enforce Tier Guardrail
            if req_tier.lower() == "prime" and user["tier"] not in ("Prime", "Admin"):
                dispatcher.utter_message(
                    text=(
                        f"🔒 **Tier Restricted — `{code_upper}` ({pct:.0f}% OFF):**\n"
                        f"Logged in as **{user['user_name']}** (**{user['tier']} Tier**). "
                        f"Coupon `{code_upper}` is exclusive to **Nova Prime** members.\n\n"
                        f"💡 You can still use **`WELCOME10`** (10% off) or **`NOVA20`** (20% off)!"
                    )
                )
                return [SlotSet("coupon_code", None), SlotSet("product_name", None), SlotSet("user_tier", user["tier"])]

            if matched_product:
                orig_price = float(matched_product["price"])
                if orig_price < min_amt:
                    dispatcher.utter_message(
                        text=(
                            f"⚠️ **Minimum Spend Not Met:** `{code_upper}` requires a minimum order of **${min_amt:.2f}**, "
                            f"but **{matched_product['name']}** is **${orig_price:.2f}**."
                        )
                    )
                else:
                    savings = orig_price * (pct / 100.0)
                    max_disc = float(target_coupon.get("max_discount", savings))
                    if savings > max_disc > 0:
                        savings = max_disc
                        
                    final_price = orig_price - savings
                    dispatcher.utter_message(
                        text=(
                            f"✅ **Coupon `{code_upper}` Validated for {user['user_name']} ({user['tier']} Tier)!**\n\n"
                            f"   🖥️ Product        : **[{matched_product['brand']}] {matched_product['name']}**\n"
                            f"   🏷️ Original Price : ${orig_price:.2f}\n"
                            f"   🎉 Discount ({pct:.0f}%) : -${savings:.2f}\n"
                            f"   💳 **Effective Price: ${final_price:.2f}**"
                        )
                    )
            else:
                dispatcher.utter_message(
                    text=(
                        f"✅ **Coupon `{code_upper}` Eligible ({user['tier']} Tier):**\n"
                        f"You qualify for **{pct:.0f}% OFF** on any order above **${min_amt:.2f}**!\n\n"
                        f"💡 *Tip: Mention any product name (e.g., \"How much is the Odyssey with `{code_upper}`?\") to calculate your exact effective price.*"
                    )
                )
            return [SlotSet("coupon_code", None), SlotSet("product_name", None), SlotSet("user_tier", user["tier"])]

        # Case 2: List all active coupons
        lines = [f"🎟️ **Active Promo Coupons for {user['user_name']} ({user['tier']} Tier):**\n"]
        for c in coupons:
            code = c.get("coupon_code", "PROMO")
            pct = float(c.get("discount_percent", 10))
            min_amt = float(c.get("min_order_value", 0))
            req_tier = str(c.get("required_tier", "All"))

            is_eligible = True if req_tier.lower() != "prime" else (user["tier"] in ("Prime", "Admin"))
            badge = "✅ Eligible" if is_eligible else "🔒 Prime Exclusive"
            lines.append(
                f"• **`{code}`** — **{pct:.0f}% OFF** (Min spend: ${min_amt:.2f} | Tier: {req_tier}) → **{badge}**"
            )

        buttons = [
            {
                "title": "⚡ Test PRIME30 on UltraGear",
                "payload": '/check_coupons{"coupon_code": "PRIME30", "product_name": "UltraGear"}'
            },
            {
                "title": "🏷️ Test NOVA20 on Odyssey",
                "payload": '/check_coupons{"coupon_code": "NOVA20", "product_name": "Odyssey"}'
            }
        ]
        dispatcher.utter_message(text="\n".join(lines), buttons=buttons)
        return [SlotSet("coupon_code", None), SlotSet("product_name", None), SlotSet("user_tier", user["tier"])]


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTION 4: ORDER STATUS CHECK — Normalized `orders` + `order_items` + RBAC
# ═══════════════════════════════════════════════════════════════════════════════

class ActionCheckOrderStatus(Action):

    def name(self) -> Text:
        return "action_check_order_status"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:

        user = _get_verified_user(tracker)
        order_id = tracker.get_slot("order_id")
        tracking_number = tracker.get_slot("tracking_number")

        user_text = tracker.latest_message.get("text", "")
        trk_match = re.search(r"TRK-\d{6}", user_text, re.IGNORECASE)
        if trk_match and not tracking_number:
            tracking_number = trk_match.group(0).upper()

        connection = get_db_connection()
        if connection is None:
            dispatcher.utter_message(
                text="Sorry, I'm having trouble reaching our order system. Please try again later. 🔧"
            )
            return []

        try:
            cursor = connection.cursor(dictionary=True)

            if not order_id and not tracking_number:
                if not user["is_admin"]:
                    cursor.execute("""
                        SELECT o.order_id, o.status, o.total_amount,
                               GROUP_CONCAT(oi.product_name SEPARATOR ', ') AS items
                        FROM orders o
                        LEFT JOIN order_items oi ON o.order_id = oi.order_id
                        WHERE LOWER(o.user_name) = LOWER(%s)
                        GROUP BY o.order_id
                        ORDER BY o.order_id;
                    """, (user["user_name"],))
                    rows = cursor.fetchall()

                    if rows:
                        lines = [f"📦 **Active Orders for {user['user_name']} ({user['tier']} Tier):**\n"]
                        buttons = []
                        for r in rows:
                            lines.append(
                                f"• **Order #{r['order_id']}** — {r['items']} "
                                f"(*{r['status']}* | ${r['total_amount']:.2f})"
                            )
                            buttons.append({
                                "title": f"🔍 Inspect Order #{r['order_id']} ({r['status']})",
                                "payload": f'/check_order_status{{"order_id": "{r["order_id"]}"}}'
                            })
                        lines.append("\nWhich order ID would you like to inspect?")
                        dispatcher.utter_message(text="\n".join(lines), buttons=buttons)
                    else:
                        dispatcher.utter_message(text="You do not currently have any orders on file.")
                    return [SlotSet("user_tier", user["tier"])]

                dispatcher.utter_message(
                    text="Please provide the **Order ID** (e.g., 1, 2) or **Tracking Number** (e.g., `TRK-901122`) to inspect. 📋"
                )
                return [SlotSet("user_tier", user["tier"])]

            if tracking_number:
                cursor.execute("""
                    SELECT order_id, user_name, total_amount, discount_applied,
                           payment_method, status, tracking_number, ordered_at, delivered_at
                    FROM orders WHERE UPPER(tracking_number) = %s;
                """, (tracking_number.upper(),))
            else:
                try:
                    order_id_int = int(re.sub(r"[^\d]", "", str(order_id)))
                except (ValueError, TypeError):
                    dispatcher.utter_message(
                        text=f"'{order_id}' doesn't look like a valid order ID. Please enter a numeric ID (e.g., 1, 2, 3)."
                    )
                    return [SlotSet("order_id", None)]

                cursor.execute("""
                    SELECT order_id, user_name, total_amount, discount_applied,
                           payment_method, status, tracking_number, ordered_at, delivered_at
                    FROM orders WHERE order_id = %s;
                """, (order_id_int,))

            result = cursor.fetchone()

            if not result:
                lookup_label = tracking_number if tracking_number else f"#{order_id}"
                dispatcher.utter_message(
                    text=f"I couldn't find an order matching **{lookup_label}**. Please double-check the number. 🔍"
                )
                return [SlotSet("order_id", None)]

            if not user["is_admin"]:
                if result["user_name"].strip().lower() != user["user_name"].strip().lower():
                    dispatcher.utter_message(text=(
                        f"🔒 **Access Denied:** Order #{result['order_id']} belongs to another customer.\n"
                        f"Logged in as **{user['user_name']}** ({user['email']}), "
                        f"you can only access your own orders."
                    ))
                    return [SlotSet("order_id", None)]

            cursor.execute("""
                SELECT oi.product_name, oi.quantity, oi.unit_price, oi.item_status,
                       p.sku, p.category, p.stock_quantity
                FROM order_items oi
                LEFT JOIN products p ON oi.product_id = p.product_id
                WHERE oi.order_id = %s;
            """, (result["order_id"],))
            items = cursor.fetchall()

            status_emoji = {
                "Processing":       "⏳",
                "Shipped":          "🚚",
                "Out for Delivery": "🛵",
                "Delivered":        "✅",
                "Cancelled":        "❌",
                "Returned":         "🔄",
            }.get(result["status"], "📦")

            item_lines = []
            admin_inventory_lines = []
            for idx, item in enumerate(items, 1):
                item_lines.append(
                    f"   {idx}. **{item['product_name']}** (Qty: {item['quantity']} × ${item['unit_price']:.2f})"
                )
                if user["is_admin"] and item.get("sku"):
                    s_flag = "✅ In stock" if (item["stock_quantity"] or 0) > 0 else "❌ Out of stock"
                    admin_inventory_lines.append(
                        f"   • `{item['sku']}` ({item['category']}): {item['stock_quantity']} units ({s_flag})"
                    )

            items_block = "\n".join(item_lines) if item_lines else "   • (No line items found)"
            trk_display = result["tracking_number"] or "Pending Carrier Assignment"
            deliv_display = f"\n   📅 Delivered : {result['delivered_at']}" if result.get("delivered_at") else ""

            if user["is_admin"]:
                inv_block = "\n".join(admin_inventory_lines)
                message = (
                    f"🛡️ **Admin View — Order #{result['order_id']}**\n"
                    f"   🔑 Verified Admin : {user['email']}\n"
                    f"   👤 Customer  : {result['user_name']}\n"
                    f"   {status_emoji} Status    : **{result['status']}**\n"
                    f"   🚚 Tracking  : `{trk_display}`\n"
                    f"   💳 Total     : **${result['total_amount']:.2f}** ({result['payment_method']})"
                    f"{deliv_display}\n\n"
                    f"📦 **Order Line Items:**\n{items_block}\n\n"
                    f"📊 **Live Warehouse Stock:**\n{inv_block}"
                )
            else:
                message = (
                    f"🧾 **Order #{result['order_id']} Summary**\n"
                    f"   👤 Customer : {result['user_name']} ({user['tier']} Member)\n"
                    f"   {status_emoji} Status   : **{result['status']}**\n"
                    f"   🚚 Tracking : `{trk_display}`\n"
                    f"   💳 Total    : **${result['total_amount']:.2f}** ({result['payment_method']})"
                    f"{deliv_display}\n\n"
                    f"📦 **Items in this Order:**\n{items_block}"
                )

            dispatcher.utter_message(text=message)

        except Error as e:
            print(f"[DB ERROR] Order status check failed: {e}")
            dispatcher.utter_message(
                text="Oops! Something went wrong while checking your order. Please try again shortly. ⚠️"
            )
        finally:
            if connection.is_connected():
                cursor.close()
                connection.close()

        return [SlotSet("order_id", None), SlotSet("tracking_number", None), SlotSet("user_tier", user["tier"])]


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTION 5: SMART ORDER CANCELLATION GUARDRAIL (Swiggy / Amazon State Machine)
# ═══════════════════════════════════════════════════════════════════════════════

class ActionCancelOrder(Action):
    def name(self) -> Text:
        return "action_cancel_order"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        user = _get_verified_user(tracker)
        order_id = tracker.get_slot("order_id")
        user_msg = tracker.latest_message.get("text", "")

        if not order_id:
            m = re.search(r"\border\s*#?(\d+)\b", user_msg, re.IGNORECASE)
            if m:
                order_id = m.group(1)

        if not order_id:
            dispatcher.utter_message(
                text="Please specify which **Order ID** you would like to cancel (e.g., *\"Cancel order 2\"* or *\"Cancel order 5\"*). 📋"
            )
            return [SlotSet("user_tier", user["tier"])]

        conn = get_db_connection()
        if conn is None:
            dispatcher.utter_message(text="⚠️ Unable to connect to the order system right now.")
            return []

        try:
            cursor = conn.cursor(dictionary=True)
            order_id_int = int(re.sub(r"[^\d]", "", str(order_id)))
            cursor.execute("""
                SELECT order_id, user_name, total_amount, status, tracking_number
                FROM orders WHERE order_id = %s;
            """, (order_id_int,))
            order = cursor.fetchone()

            if not order:
                dispatcher.utter_message(text=f"Order #{order_id_int} was not found. 🔍")
                return [SlotSet("order_id", None)]

            # RBAC check
            if not user["is_admin"] and order["user_name"].strip().lower() != user["user_name"].strip().lower():
                dispatcher.utter_message(
                    text=f"🔒 **Cancellation Denied:** Order #{order_id_int} belongs to another customer."
                )
                return [SlotSet("order_id", None)]

            status = order["status"]
            refund_amt = float(order["total_amount"])

            # Guardrail 1: Already Cancelled or Returned
            if status in ("Cancelled", "Returned"):
                dispatcher.utter_message(
                    text=f"ℹ️ Order #{order_id_int} is already marked as **{status}**."
                )
                return [SlotSet("order_id", None)]

            # Guardrail 2: Shipped, Out for Delivery, or Delivered -> BLOCK CANCELLATION
            if status in ("Shipped", "Out for Delivery", "Delivered"):
                trk = order.get("tracking_number") or "Assigned"
                dispatcher.utter_message(
                    text=(
                        f"⛔ **Cancellation Blocked — Order #{order_id_int} is `{status}`:**\n"
                        f"Orders can only be cancelled while in the **Processing** stage before carrier handoff "
                        f"(Tracking: `{trk}`).\n\n"
                        f"💡 Once delivered, you can initiate an **Instant Return** within 30 days!"
                    ),
                    buttons=[
                        {
                            "title": f"🔄 Request Return for Order #{order_id_int}",
                            "payload": f'/request_return{{"order_id": "{order_id_int}"}}'
                        },
                        {
                            "title": "🎧 Escalate to Support",
                            "payload": "/support_ticket"
                        }
                    ]
                )
                return [SlotSet("order_id", None), SlotSet("user_tier", user["tier"])]

            # Guardrail 3: Status == 'Processing' -> APPROVE CANCELLATION & REFUND WALLET
            cursor.execute("UPDATE orders SET status = 'Cancelled' WHERE order_id = %s;", (order_id_int,))
            cursor.execute("UPDATE order_items SET item_status = 'Cancelled' WHERE order_id = %s;", (order_id_int,))
            cursor.execute("""
                UPDATE accounts
                SET wallet_balance = wallet_balance + %s
                WHERE LOWER(user_name) = LOWER(%s);
            """, (refund_amt, order["user_name"]))
            conn.commit()

            new_bal = user["wallet_balance"] + refund_amt
            dispatcher.utter_message(
                text=(
                    f"✅ **Order #{order_id_int} Successfully Cancelled!**\n\n"
                    f"   🔄 Previous Status : `Processing` $\\rightarrow$ **`Cancelled`**\n"
                    f"   💰 Instant Refund  : **${refund_amt:.2f}** credited to Nova Wallet\n"
                    f"   💳 New Balance     : **${new_bal:.2f}**"
                )
            )

        except Error as e:
            print(f"[CANCEL ERROR] {e}")
            dispatcher.utter_message(text="⚠️ Something went wrong while cancelling the order.")
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

        return [SlotSet("order_id", None), SlotSet("user_tier", user["tier"])]


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTION 6: NOVA WALLET LEDGER & SUPPORT TICKET TRIAGE
# ═══════════════════════════════════════════════════════════════════════════════

class ActionCheckWallet(Action):
    def name(self) -> Text:
        return "action_check_wallet"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        user = _get_verified_user(tracker)
        conn = get_db_connection()
        returns_count = 0
        if conn:
            try:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    "SELECT COUNT(*) AS cnt FROM returns WHERE LOWER(user_name) = LOWER(%s);",
                    (user["user_name"],)
                )
                row = cursor.fetchone()
                returns_count = row["cnt"] if row else 0
            finally:
                if conn.is_connected():
                    cursor.close()
                    conn.close()

        perks = (
            "⚡ **Instant Wallet Refunds** on returns | 🎟️ **`PRIME30`** (30% off) | 🎧 **High-Priority Support**"
            if user["tier"] in ("Prime", "Admin")
            else "📦 Standard 5–7 Day Refunds | 🎟️ Eligible for **`WELCOME10`** & **`NOVA20`**"
        )

        msg = (
            f"💳 **Nova Wallet & Membership Summary**\n\n"
            f"   👤 Account Holder : **{user['user_name']}** ({user['email'] or 'Verified'})\n"
            f"   🏅 Membership Tier: **{user['tier']}**\n"
            f"   💰 Wallet Balance : **${user['wallet_balance']:.2f}**\n"
            f"   🔄 Processed Returns: {returns_count}\n\n"
            f"🎁 **Active Tier Benefits:**\n   {perks}"
        )

        buttons = [
            {"title": "🎟️ Check My Coupons", "payload": "/check_coupons"},
            {"title": "📦 My Orders", "payload": "/check_order_status"},
        ]
        dispatcher.utter_message(text=msg, buttons=buttons)
        return [SlotSet("user_tier", user["tier"])]


class ActionSupportTicket(Action):
    def name(self) -> Text:
        return "action_support_ticket"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        user = _get_verified_user(tracker)
        user_msg = tracker.latest_message.get("text", "").lower()

        conn = get_db_connection()
        if conn is None:
            dispatcher.utter_message(text="⚠️ Support ticket system is temporarily unreachable.")
            return []

        try:
            cursor = conn.cursor(dictionary=True)

            # If user explicitly asks to escalate / talk to a human agent, create a priority ticket
            wants_escalation = any(w in user_msg for w in ["human", "agent", "escalate", "urgent", "connect", "open a"])
            if wants_escalation:
                priority = "High" if user["tier"] in ("Prime", "Admin") else "Medium"
                
               
                cursor.execute("""
                    INSERT INTO support_tickets (user_name, order_id, issue_category, priority, description)
                    VALUES (%s, NULL, 'Bot Escalation', %s, %s);
                """, (
                    user["user_name"],
                    priority,
                    f"Customer requested live human agent escalation via chat ({user['tier']} tier)."
                ))
                conn.commit()
                new_tid = cursor.lastrowid
                dispatcher.utter_message(
                    text=(
                        f"🎧 **Live Support Escalation Logged — Ticket #{new_tid}**\n\n"
                        f"   👤 Customer : **{user['user_name']}** ({user['tier']} Tier)\n"
                        f"   🚨 Priority : **{priority} Priority Queue**\n"
                        f"   ⏳ Status   : **Open (Assigned to Senior Specialist)**\n\n"
                        f"A specialist will review your conversation transcript shortly."
                    )
                )
                return [SlotSet("user_tier", user["tier"])]

            # Otherwise, display open support tickets for the user (or all tickets if Admin)
            if user["is_admin"]:
                cursor.execute("SELECT * FROM support_tickets ORDER BY ticket_id DESC LIMIT 5;")
            else:
                cursor.execute("""
                    SELECT * FROM support_tickets
                    WHERE LOWER(user_name) = LOWER(%s)
                    ORDER BY ticket_id DESC;
                """, (user["user_name"],))
            tickets = cursor.fetchall()

            if not tickets:
                dispatcher.utter_message(
                    text=f"🎫 You currently have **0 open support tickets** under **{user['user_name']}**.",
                    buttons=[{"title": "🎧 Talk to a Human Agent", "payload": "talk to a human agent"}]
                )
                return [SlotSet("user_tier", user["tier"])]

            lines = [f"🎫 **Support Tickets for {user['user_name']} ({user['tier']} View):**\n"]
            for t in tickets:
                oid_label = f"Order #{t['order_id']}" if t.get("order_id") else "General Inquiry"
                lines.append(
                    f"• **Ticket #{t['ticket_id']}** ({oid_label}) — **{t.get('status', 'Open')}** "
                    f"[*{t.get('priority', 'Medium')} Priority*]\n"
                    f"  📝 *{t.get('description', 'Escalated issue')}*"
                )

            dispatcher.utter_message(text="\n".join(lines))

        except Error as e:
            print(f"[TICKET ERROR] {e}")
            dispatcher.utter_message(text="⚠️ Could not retrieve support tickets right now.")
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

        return [SlotSet("user_tier", user["tier"])]


# ═══════════════════════════════════════════════════════════════════════════════
#  FORM VALIDATION: order_return_form — RBAC + 30-Day Window + Delivery Check
# ═══════════════════════════════════════════════════════════════════════════════

class ValidateOrderReturnForm(FormValidationAction):

    def name(self) -> Text:
        return "validate_order_return_form"

    def validate_order_id(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:

        try:
            order_id_int = int(re.sub(r"[^\d]", "", str(slot_value)))
            if order_id_int <= 0:
                raise ValueError("Non-positive")
        except (ValueError, TypeError):
            dispatcher.utter_message(
                text=f"'{slot_value}' is not a valid order ID. Please provide a positive number (e.g., 1, 2, 3)."
            )
            return {"order_id": None}

        connection = get_db_connection()
        if connection is None:
            dispatcher.utter_message(
                text="I can't verify the order right now due to a system issue. Please try again in a moment. 🔧"
            )
            return {"order_id": None}

        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute("""
                SELECT order_id, user_name, status, delivered_at,
                       DATEDIFF(NOW(), delivered_at) AS days_since_delivery
                FROM orders WHERE order_id = %s;
            """, (order_id_int,))
            result = cursor.fetchone()

            if not result:
                dispatcher.utter_message(
                    text=f"Order #{order_id_int} was not found. Please check the number and try again."
                )
                return {"order_id": None}

            user = _get_verified_user(tracker)
            if not user["is_admin"]:
                if result["user_name"].strip().lower() != user["user_name"].strip().lower():
                    dispatcher.utter_message(
                        text=f"🔒 **Return Denied:** Order #{order_id_int} does not belong to your account (**{user['user_name']}**)."
                    )
                    return {"order_id": None}

            if result["status"] in ("Returned", "Cancelled"):
                dispatcher.utter_message(
                    text=f"⚠️ Order #{order_id_int} is already marked as **{result['status']}** and cannot be returned again."
                )
                return {"order_id": None}

            if result["status"] != "Delivered":
                dispatcher.utter_message(
                    text=f"🚚 Order #{order_id_int} is currently **{result['status']}**. Returns can only be initiated after the package is delivered."
                )
                return {"order_id": None}

            days = result.get("days_since_delivery")
            if days is not None and days > 30:
                cursor.execute("""
                    INSERT INTO support_tickets (user_name, order_id, issue_category, priority, description)
                    VALUES (%s, %s, 'Bot Escalation', 'Medium', %s);
                """, (
                    result["user_name"],
                    order_id_int,
                    f"Customer attempted return {days} days after delivery (exceeds 30-day window)."
                ))
                connection.commit()
                ticket_id = cursor.lastrowid

                dispatcher.utter_message(
                    text=(
                        f"⛔ **30-Day Return Window Expired:**\n"
                        f"Order #{order_id_int} was delivered **{days} days ago** (Policy limit: 30 days).\n\n"
                        f"💡 Because your item may still be covered under the **1-Year Manufacturer Warranty**, "
                        f"I have automatically created **Support Ticket #{ticket_id}** for our warranty team. "
                        f"Type **'cancel'** to exit the return form."
                    )
                )
                return {"order_id": None}

            return {"order_id": str(order_id_int)}

        except Error as e:
            print(f"[DB ERROR] Order validation failed: {e}")
            dispatcher.utter_message(text="Something went wrong validating the order. Please try again. ⚠️")
            return {"order_id": None}
        finally:
            if connection.is_connected():
                cursor.close()
                connection.close()

    def validate_reason_for_return(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        if slot_value and len(str(slot_value).strip()) >= 3:
            return {"reason_for_return": str(slot_value).strip()}

        dispatcher.utter_message(
            text="Could you provide a bit more detail about why you'd like to return? (e.g., 'defective', 'wrong item', 'changed my mind')"
        )
        return {"reason_for_return": None}


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTION 7: SUBMIT RETURN — Updates `returns`, `orders`, `order_items` & Wallet
# ═══════════════════════════════════════════════════════════════════════════════

class ActionSubmitReturn(Action):

    def name(self) -> Text:
        return "action_submit_return"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:

        order_id = tracker.get_slot("order_id")
        reason   = tracker.get_slot("reason_for_return")

        if not order_id or not reason:
            dispatcher.utter_message(
                text="I seem to be missing some information. Could you start the return process again? 🔄"
            )
            return [AllSlotsReset()]

        user = _get_verified_user(tracker)
        connection = get_db_connection()
        if connection is None:
            dispatcher.utter_message(
                text="Sorry, I can't process the return right now due to a system issue. Please try again later. 🔧"
            )
            return []

        try:
            cursor = connection.cursor(dictionary=True)
            order_id_int = int(order_id)

            cursor.execute(
                "SELECT user_name, total_amount FROM orders WHERE order_id = %s;",
                (order_id_int,)
            )
            order_row = cursor.fetchone()
            if not order_row:
                dispatcher.utter_message(text=f"Order #{order_id_int} was not found. Cannot process the return.")
                return [SlotSet("order_id", None), SlotSet("reason_for_return", None)]

            if not user["is_admin"]:
                if order_row["user_name"].strip().lower() != user["user_name"].strip().lower():
                    dispatcher.utter_message(
                        text=f"🔒 **Return Denied:** Order #{order_id_int} does not belong to your account (**{user['user_name']}**)."
                    )
                    return [SlotSet("order_id", None), SlotSet("reason_for_return", None)]

            refund_amount = float(order_row["total_amount"])
            is_prime = (user["tier"] == "Prime")
            resolution = "Instant Wallet Credit" if is_prime else "Refund to Original Payment"
            ret_status = "Approved" if is_prime else "Pending Pickup"

            cursor.execute("""
                INSERT INTO returns (order_id, user_name, reason, resolution_type, refund_amount, return_status)
                VALUES (%s, %s, %s, %s, %s, %s);
            """, (order_id_int, order_row["user_name"], reason, resolution, refund_amount, ret_status))
            return_id = cursor.lastrowid

            cursor.execute("UPDATE orders SET status = 'Returned' WHERE order_id = %s;", (order_id_int,))
            cursor.execute("UPDATE order_items SET item_status = 'Return Requested' WHERE order_id = %s;", (order_id_int,))

            new_wallet_balance = user["wallet_balance"]
            if is_prime:
                cursor.execute("""
                    UPDATE accounts
                    SET wallet_balance = wallet_balance + %s
                    WHERE LOWER(user_name) = LOWER(%s);
                """, (refund_amount, order_row["user_name"]))
                new_wallet_balance += refund_amount

            connection.commit()

            prime_badge = (
                f"\n⚡ **Prime Instant Refund Activated!**\n"
                f"   💰 **${refund_amount:.2f}** has been credited directly to your Nova Wallet "
                f"(New Balance: **${new_wallet_balance:.2f}**)."
            ) if is_prime else (
                f"\nOur courier will pick up the item within 24–48 hours, and your refund of "
                f"**${refund_amount:.2f}** will be credited within 5–7 business days. 📧"
            )

            message = (
                f"✅ **Return Request Submitted!**\n\n"
                f"   🔖 Return ID   : #{return_id}\n"
                f"   🧾 Order ID    : #{order_id_int}\n"
                f"   📝 Reason      : {reason}\n"
                f"   💳 Resolution  : {resolution}\n"
                f"   ⏳ Status      : **{ret_status}**\n"
                f"{prime_badge}"
            )
            dispatcher.utter_message(text=message)

        except Error as e:
            print(f"[DB ERROR] Return submission failed: {e}")
            dispatcher.utter_message(
                text="Something went wrong while submitting your return. Please try again or contact support. ⚠️"
            )
        finally:
            if connection.is_connected():
                cursor.close()
                connection.close()

        return [
            SlotSet("order_id", None),
            SlotSet("reason_for_return", None),
            SlotSet("user_tier", user["tier"]),
        ]


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTION 8: POLICY RAG ASSISTANT (Port 5000 + Ollama Phi-3)
# ═══════════════════════════════════════════════════════════════════════════════

class ActionAnswerFAQ(Action):
    def name(self) -> Text:
        return "action_answer_faq"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        user_message = tracker.latest_message.get('text')

        try:
            response = requests.get("http://127.0.0.1:5000/faq", params={"q": user_message}, timeout=65)
            if response.status_code == 200:
                answer = response.json().get("answer", "I couldn't generate an answer.")
                dispatcher.utter_message(text=f"📄 **Policy Assistant:**\n{answer}")
            else:
                dispatcher.utter_message(text="I'm having trouble accessing the policy database right now.")
        except requests.exceptions.RequestException:
            dispatcher.utter_message(text="⚠️ The FAQ service is currently offline.")

        return []


class ActionGenerateAdminReport(Action):
    def name(self) -> Text:
        return "action_generate_admin_report"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        user = _get_verified_user(tracker)
        
        # 1. Strict RBAC Guardrail
        if user.get("role") != "admin":
            dispatcher.utter_message(
                text="🔒 **Access Denied:** Only administrators have clearance to generate system reports."
            )
            return []

        conn = get_db_connection()
        if conn is None:
            dispatcher.utter_message(text="⚠️ Unable to reach the database to generate the report.")
            return []

        try:
            cursor = conn.cursor(dictionary=True)
            # Fetch low stock inventory
            cursor.execute("SELECT name, stock_quantity FROM products WHERE stock_quantity < 20;")
            low_stock = cursor.fetchall()
            
            # Fetch active coupons
            cursor.execute("SELECT coupon_code, discount_percent FROM coupons WHERE is_active = 1;")
            active_coupons = cursor.fetchall()
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()

        # 2. Generate the PDF
        
        # 2. Generate the PDF (Professional Nova AI Theme)
        pdf = FPDF()
        pdf.add_page()
        
        
        brand_blue = (0, 122, 255)
        dark_text = (29, 29, 31)
        light_gray = (134, 134, 139)
        
        # Header
        pdf.set_font("Arial", 'B', 24)
        pdf.set_text_color(*dark_text)
        pdf.cell(0, 12, "Nova AI", ln=True)
        
        pdf.set_font("Arial", '', 12)
        pdf.set_text_color(*light_gray)
        pdf.cell(0, 8, "System Intelligence Report", ln=True)
        
        pdf.set_font("Arial", '', 10)
        pdf.cell(0, 6, f"Generated: {datetime.datetime.now().strftime('%B %d, %Y at %I:%M %p')}", ln=True)
        pdf.ln(8)
        
        # Divider Line
        pdf.set_draw_color(229, 229, 234)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(8)
        
        # Section 1: Inventory Alerts
        pdf.set_font("Arial", 'B', 14)
        pdf.set_text_color(*brand_blue)
        pdf.cell(0, 10, "Low Stock Alerts (Action Required)", ln=True)
        
        # Table Header
        pdf.set_fill_color(245, 245, 247)
        pdf.set_font("Arial", 'B', 10)
        pdf.set_text_color(*dark_text)
        pdf.cell(140, 8, " Product Name", border=1, fill=True)
        pdf.cell(50, 8, " Remaining Stock", border=1, fill=True, align='C', ln=True)
        
        # Table Rows
        pdf.set_font("Arial", '', 10)
        if low_stock:
            for item in low_stock:
                # Highlight critical stock (< 5) in red
                if int(item['stock_quantity']) < 5:
                    pdf.set_text_color(255, 59, 48) # Apple Red
                else:
                    pdf.set_text_color(*dark_text)
                pdf.cell(140, 8, f" {item['name']}", border=1)
                pdf.cell(50, 8, f" {item['stock_quantity']} units", border=1, align='C', ln=True)
        else:
            pdf.cell(190, 8, " All inventory levels are optimal.", border=1, align='C', ln=True)
            
        pdf.ln(10)
        
        # Section 2: Promotions
        pdf.set_font("Arial", 'B', 14)
        pdf.set_text_color(*brand_blue)
        pdf.cell(0, 10, "Active Promotions", ln=True)
        
        # Table Header
        pdf.set_font("Arial", 'B', 10)
        pdf.set_text_color(*dark_text)
        pdf.cell(95, 8, " Coupon Code", border=1, fill=True)
        pdf.cell(95, 8, " Discount Applied", border=1, fill=True, align='C', ln=True)
        
        # Table Rows
        pdf.set_font("Arial", '', 10)
        if active_coupons:
            for coupon in active_coupons:
                pdf.cell(95, 8, f" {coupon['coupon_code']}", border=1)
                pdf.cell(95, 8, f" {float(coupon['discount_percent']):.0f}% OFF", border=1, align='C', ln=True)
        else:
            pdf.cell(190, 8, " No active promotions.", border=1, align='C', ln=True)
            
        # Footer
        pdf.set_y(-15)
        pdf.set_font("Arial", 'I', 8)
        pdf.set_text_color(*light_gray)
        pdf.cell(0, 10, f"Nova AI Enterprise E-Commerce System  |  Page {pdf.page_no()}", align='C')

        # 3. Save directly to your Flask vector_api static folder
        
        filename = f"nova_report_{datetime.datetime.now().strftime('%H%M%S')}.pdf"
        flask_static_dir = r"C:\Users\aamir\OneDrive\Desktop\vector_api\static"
        
        if not os.path.exists(flask_static_dir):
            os.makedirs(flask_static_dir)
            
        filepath = os.path.join(flask_static_dir, filename)
        pdf.output(filepath)

        # 4. Return clickable Markdown link to the UI
        dispatcher.utter_message(
            text=(
                f"✅ **System Report Generated Successfully.**\n\n"
                f"The snapshot includes current inventory alerts and active promotional rules.\n"
                f"📄 [Download {filename}](http://localhost:5000/static/{filename})"
            )
        )
        return []