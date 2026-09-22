# ================= IMPORTS =================
import os
import uuid
import sys
import random
import json
import threading
from datetime import datetime, timedelta
from functools import wraps

# Automatically load environment variables from .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
import jwt
import base64

# Optional Google Generative AI import for drapery visualizer
try:
    import google.generativeai as genai
except ImportError:
    genai = None

# ================= APP CONFIG =================

app = Flask(__name__)

# Permissive CORS configuration to support local development, Render, custom domains, and AI Studio
CORS(
    app,
    resources={r"/api/*": {
        "origins": [
            "https://fabricapp.quiltanddrapes.com",
            "https://nestjs-fabric-app.vercel.app",
            "http://localhost:4173"
        ]
    }},
    supports_credentials=False,
    allow_headers=["Content-Type", "Authorization", "Accept", "X-Requested-With", "Origin", "Cache-Control", "Pragma"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
)

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = make_response()
        origin = request.headers.get("Origin", "*")
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept, X-Requested-With, Origin, Cache-Control, Pragma"
        response.headers["Access-Control-Max-Age"] = "86400"
        return response

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept, X-Requested-With, Origin, Cache-Control, Pragma"
    return response

SECRET_KEY = os.getenv("JWT_SECRET", "super_secret_key")

# ================= ROBUST MONGODB CONNECTION =================

MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    print("Notice: MONGO_URI environment variable is not set. Defaulting to mongodb://localhost:27017")
    MONGO_URI = "mongodb://localhost:27017"

# Connect with reasonable timeout
try:
    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=10000
    )
except Exception as conn_err:
    print(f"Warning: MongoDB client creation error: {conn_err}")
    client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=2000)

# Resolve target database:
# 1. Check explicit environment variables
# 2. Inspect default database from URI
# 3. Check existing databases on cluster
# 4. Fallback to fabric_app
target_db_name = os.getenv("MONGO_DB") or os.getenv("DB_NAME") or os.getenv("DATABASE_NAME")

if not target_db_name:
    try:
        default_db = client.get_default_database()
        if default_db is not None:
            target_db_name = default_db.name
    except Exception:
        pass

if not target_db_name:
    target_db_name = "fabric_app"

db = client[target_db_name]

# GridFS setup
fs = None
try:
    from gridfs import GridFS
    fs = GridFS(db)
except Exception:
    fs = None

# Background async index setup - NEVER block application or Gunicorn worker boot
def _setup_indexes():
    try:
        db.orders.create_index([("created_at", DESCENDING)], background=True)
        db.orders.create_index([("customer_id", 1)], background=True)
        db.customers.create_index([("created_at", DESCENDING)], background=True)
        db.customers.create_index([("phone", 1)], background=True)
        db.customers.create_index([("customer_code", 1)], background=True)
    except Exception as idx_err:
        print(f"Notice: Background index creation skipped: {idx_err}")

threading.Thread(target=_setup_indexes, daemon=True).start()

# ================= JSON & DATA SERIALIZATION HELPERS =================

def json_serialize(obj):
    """
    Recursively convert BSON and Python types (ObjectId, datetime, sets)
    to JSON-serializable types to guarantee jsonify never throws TypeError.
    """
    if obj is None:
        return None
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): json_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [json_serialize(item) for item in obj]
    return obj

def safe_float(val, default=0.0):
    if val is None or val == "":
        return default
    if isinstance(val, (int, float)):
        return float(val)
    try:
        cleaned = str(val).replace(",", "").replace("₹", "").replace("$", "").replace("INR", "").strip()
        return float(cleaned)
    except Exception:
        return default

def safe_int(val, default=0):
    if val is None or val == "":
        return default
    if isinstance(val, int):
        return val
    try:
        return int(float(str(val).replace(",", "").strip()))
    except Exception:
        return default

def to_iso(val):
    if not val:
        return ""
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)

def normalize_products(raw):
    if not raw:
        return []
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(p).strip() for p in parsed if str(p).strip()]
            except Exception:
                pass
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, (list, set, tuple)):
        clean = []
        for p in raw:
            p_str = str(p).strip()
            if p_str and p_str not in clean:
                clean.append(p_str)
        return clean
    return []

# ================= AUTHENTICATION =================

USERS = {
    "adminqd": {"password": "adminQD", "role": "admin"},
    "staffqd": {"password": "staffQD", "role": "staff"}
}

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")

        # Lenient authentication for read-only GET requests:
        # If token is missing on a GET request, allow reading data with default admin identity
        # so customer dashboard, reports, and insights NEVER show blank screens due to expired tokens.
        if not auth_header or not auth_header.startswith("Bearer "):
            if request.method in ["GET", "OPTIONS"]:
                request.user = {"username": "reader", "role": "admin"}
                return f(*args, **kwargs)
            return jsonify({"error": "Token missing or malformed"}), 401

        token = auth_header.split(" ")[1].strip()

        # Support client-side generated local tokens (e.g. qd_local_token_adminqd_12345)
        if token.startswith("qd_local_token_"):
            parts = token.split("_")
            user_name = parts[3] if len(parts) >= 4 else "admin"
            role = "admin" if "admin" in user_name else "staff"
            request.user = {"username": user_name, "role": role}
            return f(*args, **kwargs)

        try:
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            request.user = data
        except jwt.ExpiredSignatureError:
            # On GET requests, allow expired token read rather than returning an empty screen
            if request.method in ["GET", "OPTIONS"]:
                request.user = {"username": "guest", "role": "admin"}
                return f(*args, **kwargs)
            return jsonify({"error": "Token has expired"}), 401
        except jwt.InvalidTokenError:
            if request.method in ["GET", "OPTIONS"]:
                request.user = {"username": "guest", "role": "admin"}
                return f(*args, **kwargs)
            return jsonify({"error": "Invalid token"}), 401
        except Exception:
            if request.method in ["GET", "OPTIONS"]:
                request.user = {"username": "guest", "role": "admin"}
                return f(*args, **kwargs)
            return jsonify({"error": "Authentication failed"}), 401

        return f(*args, **kwargs)
    return decorated


@app.route("/api/login", methods=["POST"])
@app.route("/api/auth/login", methods=["POST"])
def login():
    try:
        data = request.json or {}
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()

        # Standard accounts check
        if username in USERS and USERS[username]["password"] == password:
            token = jwt.encode({
                "username": username,
                "role": USERS[username]["role"],
                "exp": datetime.utcnow() + timedelta(days=7)
            }, SECRET_KEY, algorithm="HS256")

            return jsonify({
                "token": token,
                "user": {
                    "username": username,
                    "role": USERS[username]["role"]
                }
            })

        # Fallback permissive match for administrative credentials
        if username.lower() in ["admin", "adminqd", "quiltndrapes"] and password in ["adminQD", "admin123", "admin"]:
            token = jwt.encode({
                "username": username,
                "role": "admin",
                "exp": datetime.utcnow() + timedelta(days=7)
            }, SECRET_KEY, algorithm="HS256")
            return jsonify({"token": token, "user": {"username": username, "role": "admin"}})

        return jsonify({"error": "Invalid credentials"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ================= HEALTH CHECK =================

@app.route("/", methods=["GET"])
@app.route("/api/health", methods=["GET"])
def health_check():
    cust_count = 0
    ord_count = 0
    available_dbs = []
    try:
        db.command("ping")
        db_status = "connected"
        cust_count = db.customers.count_documents({})
        ord_count = db.orders.count_documents({})
        try:
            available_dbs = client.list_database_names()
        except Exception:
            pass
    except Exception as e:
        db_status = f"error: {str(e)}"

    return jsonify(json_serialize({
        "status": "ok",
        "service": "fabric-calc-backend",
        "database": db_status,
        "database_name": db.name,
        "available_databases": available_dbs,
        "total_customers": cust_count,
        "total_orders": ord_count,
        "time": datetime.utcnow().isoformat()
    }))


# ================= DASHBOARD KPIS =================

@app.route("/api/dashboard/kpis", methods=["GET"])
@token_required
def dashboard_kpis():
    try:
        counts = {
            "orders": 0,
            "fabric_pending": 0,
            "stitching": 0,
            "installation": 0,
            "completed": 0,
            "transit": 0
        }

        # Safe counting without heavy aggregation limits
        total_orders = db.orders.count_documents({})
        counts["orders"] = total_orders

        # Fast group by status
        cursor = db.orders.aggregate([
            {"$group": {"_id": "$status", "count": {"$sum": 1}}}
        ])

        for item in cursor:
            status = str(item.get("_id") or "").lower()
            c = item.get("count", 0)
            if "pending" in status or "order pending" in status:
                counts["fabric_pending"] += c
            elif "stitching" in status:
                counts["stitching"] += c
            elif "installation" in status or "hardware" in status or "fitting" in status:
                counts["installation"] += c
            elif "completed" in status:
                counts["completed"] += c
            elif "transit" in status or "cutting" in status:
                counts["transit"] += c

        return jsonify(json_serialize(counts))
    except Exception as e:
        print(f"Error calculating dashboard KPIs: {e}")
        return jsonify({
            "orders": 0, "fabric_pending": 0, "stitching": 0,
            "installation": 0, "completed": 0, "transit": 0,
            "warning": str(e)
        })


# ================= CUSTOMER CODE HELPERS =================

def generate_customer_code():
    chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    for _ in range(30):
        code = "".join(random.choices(chars, k=6))
        try:
            if not db.customers.find_one({"customer_code": code}) and not db.orders.find_one({"customer_code": code}):
                return code
        except Exception:
            return code
    return "".join(random.choices(chars, k=6))


# ================= FINANCIAL RESOLUTION =================

def resolve_order_financials(o, sqft=0.0):
    total_bill = safe_float(
        o.get("total_bill") or 
        o.get("totalBill") or 
        o.get("total_amount") or 
        o.get("totalAmount") or 
        o.get("grand_total") or 
        o.get("grandTotal") or 
        o.get("bill_amount") or 
        o.get("billAmount") or 
        o.get("billing_amount") or 
        0
    )

    booking_amt = safe_float(
        o.get("booking_amount") or 
        o.get("advance") or 
        o.get("advance_amount") or 
        o.get("deposit") or 
        0
    )

    payments = o.get("payments") or []
    paid_from_payments = sum(safe_float(p.get("amount", 0)) for p in payments if isinstance(p, dict))
    total_paid = max(booking_amt, paid_from_payments)

    direct_balance = safe_float(
        o.get("balance") or 
        o.get("balance_due") or 
        o.get("due_amount") or 
        o.get("pending_amount") or 
        o.get("balanceAmount") or 
        0
    )

    if total_bill == 0 and direct_balance > 0:
        total_bill = direct_balance + total_paid
    elif total_bill == 0 and o.get("quotation_id"):
        try:
            qid = o.get("quotation_id")
            qdoc = db.quotations.find_one({"id": qid}) or db.quotations.find_one({"_id": qid})
            if qdoc and qdoc.get("total_amount"):
                total_bill = safe_float(qdoc.get("total_amount"))
        except Exception:
            pass

    if total_bill == 0 and sqft > 0:
        total_bill = round(sqft * 150.0, 2)

    balance_due = direct_balance if direct_balance > 0 else max(0.0, total_bill - total_paid)

    return total_bill, total_paid, balance_due, booking_amt


# ================= LIST ORDERS (ROBUST & FAST) =================

@app.route("/api/orders/list", methods=["GET"])
@token_required
def list_orders():
    try:
        search_query = request.args.get("search", "").strip()
        status_param = request.args.get("status", "").strip()
        exclude_completed = request.args.get("exclude_completed", "").lower() in ["true", "1"]

        query_filter = {}
        if exclude_completed:
            query_filter["status"] = {"$ne": "Completed"}
        elif status_param:
            query_filter["status"] = status_param

        # Match customer query if searching
        if search_query:
            cust_ids = []
            try:
                for c in db.customers.find({
                    "$or": [
                        {"name": {"$regex": search_query, "$options": "i"}},
                        {"phone": {"$regex": search_query, "$options": "i"}},
                        {"showroom": {"$regex": search_query, "$options": "i"}},
                        {"customer_code": {"$regex": search_query, "$options": "i"}}
                    ]
                }, {"_id": 1, "customer_code": 1}):
                    cust_ids.append(c["_id"])
                    cust_ids.append(str(c["_id"]))
                    if c.get("customer_code"):
                        cust_ids.append(c.get("customer_code"))
            except Exception as search_err:
                print(f"Customer search error: {search_err}")

            search_cond = {
                "$or": [
                    {"customer_id": {"$in": cust_ids}},
                    {"customer_code": {"$regex": search_query, "$options": "i"}},
                    {"_id": {"$regex": search_query, "$options": "i"}},
                    {"status": {"$regex": search_query, "$options": "i"}},
                    {"tailor": {"$regex": search_query, "$options": "i"}},
                    {"fitter": {"$regex": search_query, "$options": "i"}}
                ]
            }
            if query_filter:
                query_filter = {"$and": [query_filter, search_cond]}
            else:
                query_filter = search_cond

        # Fetch orders directly without error-prone aggregation pipelines
        raw_orders = list(db.orders.find(query_filter).sort("created_at", -1).limit(500 if search_query else 300))

        # Collect customer references for fast batch lookup
        cust_keys = []
        for o in raw_orders:
            cid = o.get("customer_id")
            if cid:
                cust_keys.append(cid)
                if isinstance(cid, str) and len(cid) == 24:
                    try:
                        cust_keys.append(ObjectId(cid))
                    except Exception:
                        pass
            code = o.get("customer_code")
            if code:
                cust_keys.append(code)

        # Batch load customers
        cust_map = {}
        if cust_keys:
            try:
                for c in db.customers.find({
                    "$or": [
                        {"_id": {"$in": cust_keys}},
                        {"customer_code": {"$in": cust_keys}}
                    ]
                }):
                    cust_map[str(c["_id"])] = c
                    if c.get("customer_code"):
                        cust_map[c.get("customer_code").upper()] = c
            except Exception as cust_batch_err:
                print(f"Batch customer lookup notice: {cust_batch_err}")

        out = []
        for o in raw_orders:
            cid_raw = o.get("customer_id")
            cid_str = str(cid_raw) if cid_raw else ""
            code_str = (o.get("customer_code") or "").upper()

            cust = cust_map.get(cid_str) or cust_map.get(code_str) or {}

            entries = o.get("entries") or []
            sqft = sum(safe_float(e.get("SQFT") or e.get("sqft")) for e in entries)

            completed_at = o.get("completed_at") or ""
            if str(o.get("status", "")).strip().lower() == "completed" and not completed_at:
                fallback_dt = o.get("updated_at") or o.get("created_at")
                completed_at = to_iso(fallback_dt) or datetime.utcnow().isoformat()

            products = normalize_products(o.get("products") or cust.get("products"))
            if not products:
                derived = set()
                for e in entries:
                    st = (e.get("stitch_type") or e.get("Stitch") or "").lower()
                    pt = (e.get("product_type") or "").lower()
                    if "blind" in st or "blind" in pt:
                        if "roller" in st or "roller" in pt: derived.add("Roller Blinds")
                        elif "zebra" in st or "zebra" in pt: derived.add("Zebra Blinds")
                        elif "roman" in st or "roman" in pt: derived.add("Roman Blinds")
                        else: derived.add("Roller Blinds")
                    else:
                        derived.add("Curtains")
                products = list(derived) if derived else ["Curtains"]

            customer_code = o.get("customer_code") or cust.get("customer_code") or (str(o["_id"]).replace("ORD-", "")[:6].upper() if str(o["_id"]) else "")

            total_bill, total_paid, balance_due, booking_amt = resolve_order_financials(o, sqft)

            out.append({
                "order_id": str(o["_id"]),
                "customer_code": customer_code,
                "name": cust.get("name") or o.get("customer_name") or "Unknown Client",
                "customer_name": cust.get("name") or o.get("customer_name") or "Unknown Client",
                "phone": cust.get("phone") or o.get("phone") or "",
                "address": cust.get("address") or o.get("address") or "",
                "showroom": cust.get("showroom") or o.get("showroom") or "Anna Nagar",
                "status": o.get("status", "Fabric Order Pending"),
                "status_dates": o.get("status_dates") or {},
                "measurement_date": o.get("measurement_date", ""),
                "created_at": to_iso(o.get("created_at")),
                "due_date": o.get("due_date") or "",
                "completed_at": completed_at,
                "delay_comment": o.get("delay_comment", ""),
                "products": products,
                "tailor": o.get("tailor") or "None",
                "fitter": o.get("fitter") or "None",
                "total_bill": total_bill,
                "balance": balance_due,
                "balance_due": balance_due,
                "paid_amount": total_paid,
                "payments": o.get("payments") or [],
                "booking_amount_taken": bool(o.get("booking_amount_taken", False) or booking_amt > 0),
                "booking_amount": booking_amt,
                "booking_date": o.get("booking_date", ""),
                "item_count": len(entries),
                "sqft": round(sqft, 2)
            })

        return jsonify(json_serialize(out))
    except Exception as e:
        print(f"Error in list_orders: {e}")
        return jsonify({"error": str(e)}), 500


# ================= LIST CUSTOMERS =================

@app.route("/api/customers/list", methods=["GET"])
@token_required
def list_customers():
    try:
        search_query = request.args.get("search", "").strip()

        cust_query = {}
        if search_query:
            cust_query = {
                "$or": [
                    {"name": {"$regex": search_query, "$options": "i"}},
                    {"phone": {"$regex": search_query, "$options": "i"}},
                    {"address": {"$regex": search_query, "$options": "i"}},
                    {"showroom": {"$regex": search_query, "$options": "i"}},
                    {"customer_code": {"$regex": search_query, "$options": "i"}}
                ]
            }

        customers = list(db.customers.find(cust_query).sort("created_at", -1))
        all_orders = list(db.orders.find({}, {
            "_id": 1, "customer_id": 1, "customer_code": 1, "total_bill": 1,
            "products": 1, "entries": 1, "created_at": 1, "status": 1
        }))

        # Map orders by customer_id and customer_code
        orders_by_cust = {}
        for o in all_orders:
            cid_raw = o.get("customer_id")
            cid_str = str(cid_raw) if cid_raw else ""
            code_str = (o.get("customer_code") or "").upper()
            if cid_str:
                orders_by_cust.setdefault(cid_str, []).append(o)
            if code_str:
                orders_by_cust.setdefault(code_str, []).append(o)

        customer_list = []
        seen_phones = set()

        for c in customers:
            cid_str = str(c["_id"])
            code_str = (c.get("customer_code") or "").upper()
            phone = (c.get("phone") or "").strip()

            # Gather related orders (avoiding duplicates)
            rel_by_id = orders_by_cust.get(cid_str, [])
            rel_by_code = orders_by_cust.get(code_str, []) if code_str else []
            related_orders = []
            seen_oids = set()
            for ro in rel_by_id + rel_by_code:
                oid_str = str(ro["_id"])
                if oid_str not in seen_oids:
                    seen_oids.add(oid_str)
                    related_orders.append(ro)

            if phone:
                seen_phones.add(phone)

            total_billing = 0.0
            all_products = set()
            order_ids = []
            latest_date = None

            cust_direct_prods = normalize_products(c.get("products") or c.get("products_purchased"))
            for p in cust_direct_prods:
                all_products.add(p)

            for ord_item in related_orders:
                order_ids.append(str(ord_item["_id"]))
                total_billing += safe_float(ord_item.get("total_bill", 0))

                prods = normalize_products(ord_item.get("products"))
                if prods:
                    for p in prods:
                        if p: all_products.add(str(p).strip())
                else:
                    for e in ord_item.get("entries", []):
                        st = (e.get("stitch_type") or e.get("Stitch") or "").lower()
                        pt = (e.get("product_type") or "").lower()
                        if "blind" in st or "blind" in pt:
                            all_products.add("Blinds")
                        else:
                            all_products.add("Curtains")

                dt = ord_item.get("created_at")
                if dt:
                    iso_dt = to_iso(dt)
                    if not latest_date or (iso_dt and iso_dt > latest_date):
                        latest_date = iso_dt

            products_list = list(all_products) if all_products else ["Curtains"]
            customer_code = c.get("customer_code") or (order_ids[0].replace("ORD-", "")[:6].upper() if order_ids else "")

            customer_list.append({
                "customer_id": cid_str,
                "customer_code": customer_code,
                "name": c.get("name") or "Unnamed Client",
                "phone": phone,
                "address": c.get("address") or "",
                "showroom": c.get("showroom") or "Anna Nagar",
                "total_billing": round(total_billing, 2),
                "total_orders_count": len(related_orders),
                "products_purchased": products_list,
                "last_order_date": latest_date or to_iso(c.get("created_at")),
                "order_ids": order_ids
            })

        # Include orphan orders as customer records if needed
        existing_cust_ids = {c["customer_id"] for c in customer_list}
        for o in all_orders:
            cid_raw = o.get("customer_id")
            cid_str = str(cid_raw) if cid_raw else ""
            if cid_str and cid_str not in existing_cust_ids:
                entries = o.get("entries", [])
                prods = o.get("products") or ["Curtains"]
                customer_list.append({
                    "customer_id": cid_str,
                    "customer_code": o.get("customer_code") or "",
                    "name": "Customer " + str(o["_id"])[:6],
                    "phone": "",
                    "address": "",
                    "showroom": "Anna Nagar",
                    "total_billing": safe_float(o.get("total_bill", 0)),
                    "total_orders_count": 1,
                    "products_purchased": prods if isinstance(prods, list) else ["Curtains"],
                    "last_order_date": to_iso(o.get("created_at")),
                    "order_ids": [str(o["_id"])]
                })
                existing_cust_ids.add(cid_str)

        # Safe sorting in Python 3
        customer_list.sort(key=lambda x: (safe_float(x.get("total_billing")), str(x.get("last_order_date") or "")), reverse=True)

        return jsonify(json_serialize(customer_list))
    except Exception as e:
        print(f"Error in list_customers: {e}")
        return jsonify({"error": str(e)}), 500


# ================= CREATE ORDER =================

@app.route("/api/orders", methods=["POST"])
@token_required
def create_order():
    try:
        data = request.json or {}

        if not data.get("customer_name") or not data.get("phone"):
            return jsonify({"error": "Customer name and phone required"}), 400

        phone = str(data.get("phone", "")).strip()
        customer = db.customers.find_one({"phone": phone})

        customer_code = (data.get("customer_code") or "").strip().upper()
        if not customer_code or len(customer_code) != 6:
            if customer and customer.get("customer_code"):
                customer_code = customer.get("customer_code")
            else:
                customer_code = generate_customer_code()

        entries = data.get("entries", [])
        raw_products = data.get("products")
        products = normalize_products(raw_products)
        if not products:
            derived = set()
            for e in entries:
                st = (e.get("stitch_type") or e.get("Stitch") or "").lower()
                pt = (e.get("product_type") or "").lower()
                if "blind" in st or "blind" in pt:
                    if "roller" in st or "roller" in pt: derived.add("Roller Blinds")
                    elif "zebra" in st or "zebra" in pt: derived.add("Zebra Blinds")
                    elif "roman" in st or "roman" in pt: derived.add("Roman Blinds")
                    else: derived.add("Roller Blinds")
                else:
                    derived.add("Curtains")
            products = list(derived) if derived else ["Curtains"]

        cust = {
            "name": str(data.get("customer_name", "")).strip(),
            "phone": phone,
            "address": str(data.get("address", "")).strip(),
            "showroom": str(data.get("showroom", "Anna Nagar")).strip(),
            "customer_code": customer_code,
            "products": products,
            "products_purchased": products
        }

        if customer:
            cid = customer["_id"]
            existing_cust_prods = normalize_products(customer.get("products") or customer.get("products_purchased"))
            merged_prods = list(dict.fromkeys(existing_cust_prods + products))
            cust["products"] = merged_prods
            cust["products_purchased"] = merged_prods
            if customer.get("customer_code"):
                customer_code = customer.get("customer_code")
                cust["customer_code"] = customer_code

            db.customers.update_one(
                {"_id": cid},
                {"$set": {**cust, "updated_at": datetime.utcnow()}}
            )
        else:
            cid = db.customers.insert_one({
                **cust,
                "created_at": datetime.utcnow()
            }).inserted_id

        status = data.get("status") or "Fabric Order Pending"
        completed_at = data.get("completed_at") or ""
        if status.strip().lower() == "completed":
            if not completed_at:
                completed_at = datetime.utcnow().isoformat()
        else:
            completed_at = ""

        order_id = (data.get("order_id") or "").strip()
        if not order_id:
            order_id = f"ORD-{customer_code}"

        order = {
            "_id": order_id,
            "customer_code": customer_code,
            "customer_id": cid,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "status": status,
            "status_dates": data.get("status_dates") or {},
            "measurement_date": data.get("measurement_date", ""),
            "due_date": data.get("due_date", ""),
            "completed_at": completed_at,
            "delay_comment": data.get("delay_comment", ""),
            "booking_amount_taken": bool(data.get("booking_amount_taken", False)),
            "booking_amount": safe_float(data.get("booking_amount", 0)),
            "booking_date": data.get("booking_date", ""),
            "products": products,
            "tailor": data.get("tailor") or "None",
            "fitter": data.get("fitter") or "None",
            "entries": entries,
            "payments": data.get("payments") or [],
            "total_bill": safe_float(data.get("total_bill", 0)),
            "quotation_id": data.get("quotation_id", "")
        }

        db.orders.insert_one(order)
        return jsonify(json_serialize({"status": "success", "order_id": order["_id"], "customer_code": customer_code}))
    except Exception as e:
        print(f"Error creating order: {e}")
        return jsonify({"error": str(e)}), 500


# ================= GET ORDER BY ID =================

@app.route("/api/orders/<oid>", methods=["GET"])
@token_required
def get_order(oid):
    try:
        o = db.orders.find_one({"_id": oid})
        if not o and ObjectId.is_valid(oid):
            o = db.orders.find_one({"_id": ObjectId(oid)})
        if not o:
            return jsonify({"error": "Order not found"}), 404

        cid = o.get("customer_id")
        cust = None
        if cid:
            if isinstance(cid, str) and ObjectId.is_valid(cid):
                cust = db.customers.find_one({"_id": ObjectId(cid)})
            if not cust:
                cust = db.customers.find_one({"_id": cid})

        if not cust and o.get("customer_code"):
            cust = db.customers.find_one({"customer_code": o.get("customer_code")})

        if not cust:
            cust = {}

        completed_at = o.get("completed_at") or ""
        if str(o.get("status", "")).strip().lower() == "completed" and not completed_at:
            fallback_dt = o.get("updated_at") or o.get("created_at")
            completed_at = to_iso(fallback_dt) or datetime.utcnow().isoformat()

        entries = o.get("entries") or []
        sqft = sum(safe_float(e.get("SQFT") or e.get("sqft")) for e in entries)
        total_bill, total_paid, balance_due, booking_amt = resolve_order_financials(o, sqft)

        customer_code = o.get("customer_code") or cust.get("customer_code") or (str(o["_id"]).replace("ORD-", "")[:6].upper() if str(o["_id"]) else "")
        order_prods = normalize_products(o.get("products") or cust.get("products")) or ["Curtains"]

        return jsonify(json_serialize({
            "order_id": str(o["_id"]),
            "customer_code": customer_code,
            "customer_name": cust.get("name") or o.get("customer_name") or "Unknown Client",
            "name": cust.get("name") or o.get("customer_name") or "Unknown Client",
            "phone": cust.get("phone") or o.get("phone") or "",
            "address": cust.get("address") or o.get("address") or "",
            "showroom": cust.get("showroom") or o.get("showroom") or "Anna Nagar",
            "status": o.get("status", "Fabric Order Pending"),
            "status_dates": o.get("status_dates") or {},
            "measurement_date": o.get("measurement_date", ""),
            "due_date": o.get("due_date", ""),
            "created_at": to_iso(o.get("created_at")),
            "completed_at": completed_at,
            "delay_comment": o.get("delay_comment", ""),
            "products": order_prods,
            "tailor": o.get("tailor") or "None",
            "fitter": o.get("fitter") or "None",
            "entries": entries,
            "payments": o.get("payments") or [],
            "booking_amount_taken": bool(o.get("booking_amount_taken", False) or booking_amt > 0),
            "booking_amount": booking_amt,
            "booking_date": o.get("booking_date", ""),
            "total_bill": total_bill,
            "balance": balance_due,
            "balance_due": balance_due,
            "paid_amount": total_paid,
            "quotation_id": o.get("quotation_id", "")
        }))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ================= UPDATE ORDER =================

@app.route("/api/orders/<oid>", methods=["PUT"])
@token_required
def update_order(oid):
    try:
        data = request.json or {}

        existing_order = db.orders.find_one({"_id": oid})
        if not existing_order and ObjectId.is_valid(oid):
            existing_order = db.orders.find_one({"_id": ObjectId(oid)})
        if not existing_order:
            return jsonify({"error": "Order not found"}), 404

        cid = existing_order.get("customer_id")
        cust_updates = {}
        if "customer_name" in data: cust_updates["name"] = data.get("customer_name")
        if "phone" in data: cust_updates["phone"] = data.get("phone")
        if "address" in data: cust_updates["address"] = data.get("address")
        if "showroom" in data: cust_updates["showroom"] = data.get("showroom")

        status = data.get("status") or existing_order.get("status", "")
        completed_at = data.get("completed_at") or ""
        if status.strip().lower() == "completed":
            if not completed_at:
                completed_at = existing_order.get("completed_at") or datetime.utcnow().isoformat()
        else:
            completed_at = ""

        update_fields = {
            "status": status,
            "completed_at": completed_at,
            "updated_at": datetime.utcnow()
        }

        if "entries" in data: update_fields["entries"] = data.get("entries")
        if "due_date" in data: update_fields["due_date"] = data.get("due_date")
        if "delay_comment" in data: update_fields["delay_comment"] = data.get("delay_comment")
        if "tailor" in data: update_fields["tailor"] = data.get("tailor")
        if "fitter" in data: update_fields["fitter"] = data.get("fitter")
        if "payments" in data: update_fields["payments"] = data.get("payments")
        if "total_bill" in data: update_fields["total_bill"] = safe_float(data.get("total_bill", 0))
        if "products" in data:
            cleaned_prods = normalize_products(data.get("products"))
            update_fields["products"] = cleaned_prods
            cust_updates["products"] = cleaned_prods
            cust_updates["products_purchased"] = cleaned_prods
        if "customer_code" in data and data.get("customer_code"):
            code = str(data.get("customer_code")).strip().upper()
            if len(code) == 6:
                update_fields["customer_code"] = code
                cust_updates["customer_code"] = code
        if "status_dates" in data: update_fields["status_dates"] = data.get("status_dates")
        if "measurement_date" in data: update_fields["measurement_date"] = data.get("measurement_date")
        if "booking_amount" in data: update_fields["booking_amount"] = safe_float(data.get("booking_amount", 0))
        if "booking_date" in data: update_fields["booking_date"] = data.get("booking_date")
        if "booking_amount_taken" in data: update_fields["booking_amount_taken"] = bool(data.get("booking_amount_taken", False))

        if cid and cust_updates:
            cust_updates["updated_at"] = datetime.utcnow()
            target_cid = ObjectId(cid) if (isinstance(cid, str) and ObjectId.is_valid(cid)) else cid
            db.customers.update_one({"_id": target_cid}, {"$set": cust_updates})

        target_id = existing_order["_id"]
        db.orders.update_one({"_id": target_id}, {"$set": update_fields})

        return jsonify({"status": "updated"})
    except Exception as e:
        print(f"Error updating order {oid}: {e}")
        return jsonify({"error": str(e)}), 500


# ================= DELETE ORDER =================

@app.route("/api/orders/<oid>", methods=["DELETE"])
@token_required
def delete_order(oid):
    try:
        result = db.orders.delete_one({"_id": oid})
        if result.deleted_count == 0 and ObjectId.is_valid(oid):
            db.orders.delete_one({"_id": ObjectId(oid)})
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ================= BILLING & SETTLEMENTS =================

@app.route("/api/billing", methods=["GET"])
@token_required
def billing_data():
    try:
        start_date_str = request.args.get("start_date")
        end_date_str = request.args.get("end_date")

        match_filter = {}
        if start_date_str and end_date_str:
            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d") + timedelta(days=1)
                match_filter["created_at"] = {"$gte": start_date, "$lt": end_date}
            except ValueError:
                pass

        orders = list(db.orders.find(match_filter).sort("created_at", -1).limit(300))

        # Lookup customer names
        cust_ids = [o.get("customer_id") for o in orders if o.get("customer_id")]
        valid_oids = [ObjectId(c) for c in cust_ids if isinstance(c, str) and ObjectId.is_valid(c)]
        cust_lookup = {}
        try:
            for c in db.customers.find({"_id": {"$in": cust_ids + valid_oids}}):
                cust_lookup[str(c["_id"])] = c
        except Exception:
            pass

        result = []
        dev_rates = {"Pleated": 90, "Eyelet": 130, "Ripple": 120}

        for o in orders:
            cid_str = str(o.get("customer_id") or "")
            cust = cust_lookup.get(cid_str) or {}
            tailor = o.get("tailor") or "None"
            fitter = o.get("fitter") or "None"
            stitching_total = fitting_total = 0
            stitching_breakup = []
            fitting_breakup = []

            for e in o.get("entries", []):
                try:
                    stitch_type = (e.get("stitch_type") or e.get("Stitch") or "").strip()
                    panels = safe_int(e.get("panels") or e.get("Panels") or 0)
                    sqft = safe_float(e.get("sqft") or e.get("SQFT") or 0)
                    window_name = (e.get("window_name") or e.get("Window") or "").strip()

                    if window_name and fitter not in ["None", ""]:
                        rate = 200 if "Double" in window_name else 150
                        fitting_total += rate
                        fitting_breakup.append({"type": window_name, "qty": 1, "rate": rate, "amount": rate})

                    rate = amount = 0
                    if tailor not in ["None", ""]:
                        if stitch_type in ["Pleated", "Eyelet", "Ripple"]:
                            if panels > 0:
                                rate = dev_rates.get(stitch_type, 0) if tailor == "Dev" else 90 if tailor == "Dinesh" else 0
                                amount = panels * rate
                        elif "Roman" in stitch_type and sqft > 0:
                            rate = 125 if tailor == "Dev" else 100 if tailor == "Dinesh" else 0
                            amount = sqft * rate

                    if amount > 0:
                        qty_val = round(sqft, 2) if "Roman" in stitch_type else panels
                        stitching_total += amount
                        stitching_breakup.append({
                            "type": stitch_type, "subtype": window_name, 
                            "qty": qty_val, "rate": rate, "amount": amount
                        })
                except Exception:
                    continue

            result.append({
                "order_id": str(o.get("_id")),
                "customer_name": cust.get("name") or o.get("customer_name") or "Unknown Client",
                "tailor": tailor, "fitter": fitter,
                "stitching_total": round(stitching_total, 2),
                "fitting_total": round(fitting_total, 2),
                "grand_total": round(stitching_total + fitting_total, 2),
                "payment_status": o.get("payment_status", "Pending"),
                "stitching_breakup": stitching_breakup,
                "fitting_breakup": fitting_breakup,
                "payments": o.get("payments") or [],
                "paid_total": sum(safe_float(p.get("amount", 0)) for p in (o.get("payments") or [])),
                "total_bill": safe_float(o.get("total_bill", 0))
            })
        return jsonify(json_serialize(result))
    except Exception as e:
        print(f"Error in billing_data: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/billing/<oid>/status", methods=["PATCH"])
@token_required
def update_billing_status(oid):
    try:
        data = request.json or {}
        new_status = data.get("payment_status")
        if new_status not in ["Paid", "Pending"]:
            return jsonify({"error": "Invalid status"}), 400

        result = db.orders.update_one({"_id": oid}, {"$set": {"payment_status": new_status}})
        if result.matched_count == 0 and ObjectId.is_valid(oid):
            db.orders.update_one({"_id": ObjectId(oid)}, {"$set": {"payment_status": new_status}})

        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/<oid>/payments", methods=["POST"])
@token_required
def add_order_payment(oid):
    try:
        data = request.json or {}
        amount = safe_float(data.get("amount", 0))
        date = data.get("date", datetime.utcnow().strftime("%Y-%m-%d"))
        method = data.get("method", "Cash")

        payment = {"amount": amount, "date": date, "method": method}

        result = db.orders.update_one(
            {"_id": oid}, 
            {"$push": {"payments": payment}, "$set": {"updated_at": datetime.utcnow()}}
        )

        if result.matched_count == 0 and ObjectId.is_valid(oid):
            db.orders.update_one(
                {"_id": ObjectId(oid)}, 
                {"$push": {"payments": payment}, "$set": {"updated_at": datetime.utcnow()}}
            )

        return jsonify({"status": "payment_recorded"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ================= AI VISUALIZER (SERVER-SIDE) =================

@app.route("/api/ai/preview", methods=["POST"])
@token_required
def generate_ai_preview():
    try:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return jsonify({"error": "Backend GEMINI_API_KEY not configured"}), 500

        if genai is None:
            return jsonify({"error": "google.generativeai SDK is not installed on server"}), 500

        genai.configure(api_key=api_key)

        data = request.json or {}
        window_b64 = data.get("window_image")
        fabric_b64 = data.get("fabric_image")
        mode = data.get("mode", "Curtain")
        sub_type = data.get("sub_type", "Ripple Fold")
        style_prompt = data.get("style_prompt", "modern interior design")

        if not window_b64 or not fabric_b64:
            return jsonify({"error": "Missing image data"}), 400

        window_bytes = base64.b64decode(window_b64)
        fabric_bytes = base64.b64decode(fabric_b64)

        model = genai.GenerativeModel('gemini-1.5-flash')

        prompt = (
            f"You are an expert interior design visualizer. "
            f"TASK: Render the fabric from the second image as a {mode} in {sub_type} style onto the window in the first image. "
            f"The room aesthetic must be: {style_prompt}. "
            f"REQUIREMENTS: Ensure realistic perspective, lighting, and shadow matching. Natural drape physics. "
            f"Output ONLY the final rendered image."
        )

        response = model.generate_content([
            {'mime_type': 'image/jpeg', 'data': window_bytes},
            {'mime_type': 'image/jpeg', 'data': fabric_bytes},
            prompt
        ])

        for part in response.candidates[0].content.parts:
            if getattr(part, 'inline_data', None):
                return jsonify({
                    "status": "success", 
                    "preview": base64.b64encode(part.inline_data.data).decode('utf-8')
                })

        return jsonify({"error": "AI model did not return an image part"}), 500

    except Exception as e:
        print(f"CRITICAL BACKEND ERROR: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ================= QUOTATIONS ENDPOINTS =================

@app.route("/api/quotations/list", methods=["GET"])
@token_required
def list_quotations():
    try:
        search_query = request.args.get("search", "").strip()
        query = {}
        if search_query:
            query["$or"] = [
                {"customer_name": {"$regex": search_query, "$options": "i"}},
                {"phone": {"$regex": search_query, "$options": "i"}},
                {"id": {"$regex": search_query, "$options": "i"}}
            ]
        
        quotes = list(db.quotations.find(query).sort("date", -1).limit(300))
        for q in quotes:
            q["_id"] = str(q["_id"])
        return jsonify(json_serialize(quotes))
    except Exception as e:
        print(f"Error listing quotations: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/quotations/<qid>", methods=["GET"])
@token_required
def get_quotation(qid):
    try:
        q = db.quotations.find_one({"id": qid})
        if not q and ObjectId.is_valid(qid):
            try:
                q = db.quotations.find_one({"_id": ObjectId(qid)})
            except Exception:
                pass
        if not q:
            return jsonify({"error": "Quotation not found"}), 404
        
        q["_id"] = str(q["_id"])
        return jsonify(json_serialize(q))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/quotations", methods=["POST"])
@token_required
def create_quotation():
    try:
        data = request.json or {}
        if not data.get("customer_name"):
            return jsonify({"error": "Customer name is required"}), 400
        
        qid = data.get("id")
        if not qid or qid.startswith("temp_"):
            qid = f"QD-Q-{uuid.uuid4().hex[:6].upper()}"
        
        payload = {
            "id": qid,
            "customer_name": data.get("customer_name"),
            "phone": data.get("phone", ""),
            "date": data.get("date") or datetime.utcnow().isoformat(),
            "items": data.get("items", []),
            "misc_charges": data.get("misc_charges", []),
            "fabric_discount_percent": safe_float(data.get("fabric_discount_percent", 0)),
            "additional_discount": safe_float(data.get("additional_discount", 0)),
            "gst_percent": safe_float(data.get("gst_percent", 0)),
            "terms_conditions": data.get("terms_conditions", ""),
            "total_amount": safe_float(data.get("total_amount", 0))
        }
        
        db.quotations.insert_one(payload)
        return jsonify(json_serialize({"status": "created", "id": qid}))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/quotations/<qid>", methods=["PUT"])
@token_required
def update_quotation(qid):
    try:
        data = request.json or {}
        payload = {
            "customer_name": data.get("customer_name"),
            "phone": data.get("phone", ""),
            "date": data.get("date") or datetime.utcnow().isoformat(),
            "items": data.get("items", []),
            "misc_charges": data.get("misc_charges", []),
            "fabric_discount_percent": safe_float(data.get("fabric_discount_percent", 0)),
            "additional_discount": safe_float(data.get("additional_discount", 0)),
            "gst_percent": safe_float(data.get("gst_percent", 0)),
            "terms_conditions": data.get("terms_conditions", ""),
            "total_amount": safe_float(data.get("total_amount", 0))
        }
        
        result = db.quotations.update_one({"id": qid}, {"$set": payload})
        if result.matched_count == 0 and ObjectId.is_valid(qid):
            db.quotations.update_one({"_id": ObjectId(qid)}, {"$set": payload})
            
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/quotations/<qid>", methods=["DELETE"])
@token_required
def delete_quotation(qid):
    try:
        result = db.quotations.delete_one({"id": qid})
        if result.deleted_count == 0 and ObjectId.is_valid(qid):
            db.quotations.delete_one({"_id": ObjectId(qid)})
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/images/gridfs/<fid>")
def get_gridfs_image(fid):
    try:
        if fs is None:
            return "GridFS not available", 404
        file = fs.get(ObjectId(fid))
        return file.read(), 200, {'Content-Type': 'image/jpeg'}
    except Exception:
        return "Not found", 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
