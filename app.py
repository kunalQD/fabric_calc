# ================= IMPORTS =================
import os
import uuid
import sys
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
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]
)


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = make_response()
        origin = request.headers.get("Origin", "*")
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept, X-Requested-With"
        response.headers["Access-Control-Max-Age"] = "86400"
        return response

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept, X-Requested-With"
    return response

SECRET_KEY = os.getenv("JWT_SECRET", "super_secret_key")

MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    print("\n" + "=" * 70)
    print("⚠️  WARNING: MONGO_URI environment variable is not set!")
    print("PyMongo is attempting to connect to mongodb://localhost:27017.")
    print("If you are using MongoDB Atlas (cloud database):")
    print("  Set MONGO_URI in your .env file or environment, e.g.:")
    print("  MONGO_URI=mongodb+srv://<user>:<password>@cluster0.mongodb.net/fabric_app?retryWrites=true&w=majority")
    print("If you are using local MongoDB on Windows:")
    print("  Ensure MongoDB Server service is running (e.g. net start MongoDB or run services.msc).")
    print("=" * 70 + "\n")
    # Default to localhost if not specified
    MONGO_URI = "mongodb://localhost:27017"

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)

# Resolve target database: inspect default database from URI, fallback to fabric_app
_default_db = None
try:
    _default_db = client.get_default_database()
except Exception:
    _default_db = None

db = _default_db if _default_db is not None else client["fabric_app"]
try:
    from gridfs import GridFS
    fs = GridFS(db)
except Exception:
    fs = None

# Ensure indexes exist on orders and customers to prevent in-memory sort limits
try:
    db.orders.create_index([("created_at", DESCENDING)], background=True)
    db.orders.create_index([("customer_id", 1)], background=True)
    db.customers.create_index([("created_at", DESCENDING)], background=True)
    db.customers.create_index([("phone", 1)], background=True)
except Exception as idx_err:
    print(f"Notice: Index setup deferred or skipped: {idx_err}")

STATUSES = [
    "Fabric Order Pending",
    "Fabric In Transit",
    "Stitching",
    "Hardware/Material Installation",
    "Completed"
]

# Helper for ISO dates
def to_iso(val):
    if not val:
        return ""
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)

# ================= HEALTH CHECK =================

@app.route("/", methods=["GET"])
@app.route("/api/health", methods=["GET"])
def health_check():
    cust_count = 0
    ord_count = 0
    try:
        # Quick ping to MongoDB
        db.command("ping")
        db_status = "connected"
        cust_count = db.customers.count_documents({})
        ord_count = db.orders.count_documents({})
    except Exception as e:
        db_status = f"error: {str(e)}"
    return jsonify({
        "status": "ok",
        "service": "fabric-calc-backend",
        "database": db_status,
        "database_name": db.name,
        "total_customers": cust_count,
        "total_orders": ord_count,
        "time": datetime.utcnow().isoformat()
    })

# ================= AUTH =================

USERS = {
    "adminqd": {"password": "adminQD", "role": "admin"},
    "staffqd": {"password": "staffQD", "role": "staff"}
}

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")

        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Token missing or malformed"}), 401

        try:
            token = auth_header.split(" ")[1]
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            request.user = data
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token has expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        except Exception:
            return jsonify({"error": "Authentication failed"}), 401

        return f(*args, **kwargs)
    return decorated


@app.route("/api/login", methods=["POST"])
@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if username in USERS and USERS[username]["password"] == password:
        token = jwt.encode({
            "username": username,
            "role": USERS[username]["role"],
            "exp": datetime.utcnow() + timedelta(hours=24)
        }, SECRET_KEY, algorithm="HS256")

        return jsonify({
            "token": token,
            "user": {
                "username": username,
                "role": USERS[username]["role"]
            }
        })

    return jsonify({"error": "Invalid credentials"}), 401


# ================= DASHBOARD =================

@app.route("/api/dashboard/kpis", methods=["GET"])
@token_required
def dashboard_kpis():
    pipeline = [
        {"$facet": {
            "total": [{"$count": "count"}],
            "by_status": [
                {"$group": {"_id": "$status", "count": {"$sum": 1}}}
            ]
        }}
    ]

    results = list(db.orders.aggregate(pipeline))[0]

    counts = {
        "orders": results["total"][0]["count"] if results["total"] else 0,
        "fabric_pending": 0, "stitching": 0, "installation": 0, "completed": 0, "transit": 0
    }

    for item in results["by_status"]:
        status = item.get("_id") or ""
        count = item.get("count", 0)
        s_lower = status.lower()
        if "pending" in s_lower or "order pending" in s_lower:
            counts["fabric_pending"] += count
        elif "stitching" in s_lower:
            counts["stitching"] += count
        elif "installation" in s_lower or "hardware" in s_lower:
            counts["installation"] += count
        elif "completed" in s_lower:
            counts["completed"] += count
        elif "transit" in s_lower or "cutting" in s_lower:
            counts["transit"] += count

    return jsonify(counts)

# ================= CREATE ORDER =================

@app.route("/api/orders", methods=["POST"])
@token_required
def create_order():
    data = request.json or {}

    if not data.get("customer_name") or not data.get("phone"):
        return jsonify({"error": "Customer name and phone required"}), 400

    cust = {
        "name": data.get("customer_name", "").strip(),
        "phone": data.get("phone", "").strip(),
        "address": data.get("address", "").strip(),
        "showroom": data.get("showroom", "Anna Nagar").strip()
    }

    entries = data.get("entries", [])

    customer = db.customers.find_one({"phone": cust["phone"]})

    if customer:
        cid = customer["_id"]
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

    # Products extraction
    products = data.get("products")
    if not products or not isinstance(products, list):
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

    order = {
        "_id": str(uuid.uuid4()),
        "customer_id": ObjectId(cid),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "status": status,
        "status_dates": data.get("status_dates", {}),
        "measurement_date": data.get("measurement_date", ""),
        "due_date": data.get("due_date", ""),
        "completed_at": completed_at,
        "delay_comment": data.get("delay_comment", ""),
        "booking_amount_taken": bool(data.get("booking_amount_taken", False)),
        "booking_amount": float(data.get("booking_amount", 0) or 0),
        "booking_date": data.get("booking_date", ""),
        "products": products,
        "tailor": data.get("tailor") or "None",
        "fitter": data.get("fitter") or "None",
        "entries": entries,
        "payments": data.get("payments", []),
        "total_bill": float(data.get("total_bill", 0) or 0),
        "quotation_id": data.get("quotation_id", "")
    }

    db.orders.insert_one(order)

    return jsonify({"status": "success", "order_id": order["_id"]})


# ================= LIST ORDERS (FULL LIST INCLUDING COMPLETED) =================

def resolve_order_financials(o, sqft=0.0):
    # Total bill from all standard field aliases
    total_bill = float(
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

    # Booking advance
    booking_amt = float(
        o.get("booking_amount") or 
        o.get("advance") or 
        o.get("advance_amount") or 
        o.get("deposit") or 
        0
    )

    # Payments list
    payments = o.get("payments") or []
    paid_from_payments = sum(float(p.get("amount", 0) or 0) for p in payments if isinstance(p, dict))
    total_paid = max(booking_amt, paid_from_payments)

    # Direct balance stored on document
    direct_balance = float(
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
                total_bill = float(qdoc.get("total_amount"))
        except Exception:
            pass

    if total_bill == 0 and sqft > 0:
        total_bill = round(sqft * 150.0, 2)

    balance_due = direct_balance if direct_balance > 0 else max(0.0, total_bill - total_paid)

    return total_bill, total_paid, balance_due, booking_amt


@app.route("/api/orders/list", methods=["GET"])
@token_required
def list_orders():
    search_query = request.args.get("search", "").strip()
    status_param = request.args.get("status", "").strip()
    exclude_completed = request.args.get("exclude_completed", "").lower() in ["true", "1"]

    query_filter = {}
    if exclude_completed:
        query_filter["status"] = {"$ne": "Completed"}
    elif status_param:
        query_filter["status"] = status_param

    if search_query:
        cust_ids = []
        for c in db.customers.find({
            "$or": [
                {"name": {"$regex": search_query, "$options": "i"}},
                {"phone": {"$regex": search_query, "$options": "i"}},
                {"showroom": {"$regex": search_query, "$options": "i"}}
            ]
        }, {"_id": 1}):
            cust_ids.append(c["_id"])
            cust_ids.append(str(c["_id"]))

        search_cond = {
            "$or": [
                {"customer_id": {"$in": cust_ids}},
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

    pipeline = [
        {"$sort": {"created_at": -1}},
        {"$match": query_filter},
        {"$limit": 500 if search_query else 300},
        {
            "$addFields": {
                "customer_id_obj": {
                    "$cond": {
                        "if": {"$eq": [{"$type": "$customer_id"}, "objectId"]},
                        "then": "$customer_id",
                        "else": {
                            "$cond": {
                                "if": {
                                    "$and": [
                                        {"$eq": [{"$type": "$customer_id"}, "string"]},
                                        {"$eq": [{"$strLenCP": "$customer_id"}, 24]}
                                    ]
                                },
                                "then": {"$toObjectId": "$customer_id"},
                                "else": None
                            }
                        }
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "customers",
                "localField": "customer_id_obj",
                "foreignField": "_id",
                "as": "customer_info"
            }
        },
        {"$unwind": {"path": "$customer_info", "preserveNullAndEmptyArrays": True}}
    ]

    orders = list(db.orders.aggregate(pipeline, allowDiskUse=True))
    out = []

    for o in orders:
        cust = o.get("customer_info") or {}
        entries = o.get("entries") or []

        sqft = sum(float(e.get("SQFT", 0) or e.get("sqft", 0) or 0) for e in entries)

        completed_at = o.get("completed_at") or ""
        if str(o.get("status", "")).strip().lower() == "completed" and not completed_at:
            fallback_dt = o.get("updated_at") or o.get("created_at")
            completed_at = to_iso(fallback_dt) or datetime.utcnow().isoformat()

        # Derive products if not explicitly stored
        products = o.get("products")
        if not products or not isinstance(products, list):
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

        total_bill, total_paid, balance_due, booking_amt = resolve_order_financials(o, sqft)

        out.append({
            "order_id": str(o["_id"]),
            "name": cust.get("name", "Unknown Client"),
            "customer_name": cust.get("name", "Unknown Client"),
            "phone": cust.get("phone", ""),
            "address": cust.get("address", ""),
            "showroom": cust.get("showroom", "Anna Nagar"),
            "status": o.get("status", "Fabric Order Pending"),
            "status_dates": o.get("status_dates", {}),
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
            "payments": o.get("payments", []),
            "booking_amount_taken": bool(o.get("booking_amount_taken", False) or booking_amt > 0),
            "booking_amount": booking_amt,
            "booking_date": o.get("booking_date", ""),
            "item_count": len(entries),
            "sqft": round(sqft, 2)
        })

    return jsonify(out)


# ================= LIST CUSTOMERS (FULL CUSTOMER DIRECTORY) =================
# Endpoint giving list of customer name, phone number, address, total billing till date,
# products purchased, and order references.

@app.route("/api/customers/list", methods=["GET"])
@token_required
def list_customers():
    search_query = request.args.get("search", "").strip()

    # Query all customers from database
    cust_query = {}
    if search_query:
        cust_query = {
            "$or": [
                {"name": {"$regex": search_query, "$options": "i"}},
                {"phone": {"$regex": search_query, "$options": "i"}},
                {"address": {"$regex": search_query, "$options": "i"}},
                {"showroom": {"$regex": search_query, "$options": "i"}}
            ]
        }

    customers = list(db.customers.find(cust_query).sort("created_at", -1))
    
    # Pre-fetch all orders to aggregate billing and products per customer
    all_orders = list(db.orders.find({}, {
        "_id": 1, "customer_id": 1, "total_bill": 1, "products": 1,
        "entries": 1, "created_at": 1, "status": 1
    }))

    # Map orders by customer_id (supporting both ObjectId and string matching)
    orders_by_cust = {}
    for o in all_orders:
        cid_raw = o.get("customer_id")
        cid_str = str(cid_raw) if cid_raw else ""
        if cid_str:
            orders_by_cust.setdefault(cid_str, []).append(o)

    customer_list = []
    seen_phones = set()

    for c in customers:
        cid_str = str(c["_id"])
        phone = (c.get("phone") or "").strip()

        # Gather orders linked to this customer
        related_orders = orders_by_cust.get(cid_str, [])
        if phone:
            seen_phones.add(phone)

        # Aggregate total billing till date
        total_billing = 0.0
        all_products = set()
        order_ids = []
        latest_date = None

        for ord_item in related_orders:
            order_ids.append(str(ord_item["_id"]))
            total_billing += float(ord_item.get("total_bill", 0) or 0)

            # Collect products
            prods = ord_item.get("products")
            if isinstance(prods, list) and prods:
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

        # Format products list
        products_list = list(all_products) if all_products else ["Curtains"]

        customer_list.append({
            "customer_id": cid_str,
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

    # In case there are orders without a customer record, include those distinct clients too
    for o in all_orders:
        cid_raw = o.get("customer_id")
        cid_str = str(cid_raw) if cid_raw else ""
        if cid_str not in [c["customer_id"] for c in customer_list]:
            entries = o.get("entries", [])
            prods = o.get("products") or ["Curtains"]
            customer_list.append({
                "customer_id": cid_str or str(o["_id"]),
                "name": "Customer " + str(o["_id"])[:6],
                "phone": "",
                "address": "",
                "showroom": "Anna Nagar",
                "total_billing": float(o.get("total_bill", 0) or 0),
                "total_orders_count": 1,
                "products_purchased": prods if isinstance(prods, list) else ["Curtains"],
                "last_order_date": to_iso(o.get("created_at")),
                "order_ids": [str(o["_id"])]
            })

    # Sort customers by total_billing descending or newest order date
    customer_list.sort(key=lambda x: (x.get("total_billing", 0), x.get("last_order_date") or ""), reverse=True)

    return jsonify(customer_list)


# ================= GET ORDER BY ID =================

@app.route("/api/orders/<oid>", methods=["GET"])
@token_required
def get_order(oid):
    o = db.orders.find_one({"_id": oid})
    if not o and ObjectId.is_valid(oid):
        o = db.orders.find_one({"_id": ObjectId(oid)})
    if not o:
        return jsonify({"error": "Not found"}), 404

    cid = o.get("customer_id")
    cust = None
    if cid:
        if ObjectId.is_valid(str(cid)):
            cust = db.customers.find_one({"_id": ObjectId(str(cid))})
        if not cust:
            cust = db.customers.find_one({"_id": cid})

    if not cust:
        cust = {}

    completed_at = o.get("completed_at") or ""
    if str(o.get("status", "")).strip().lower() == "completed" and not completed_at:
        fallback_dt = o.get("updated_at") or o.get("created_at")
        completed_at = to_iso(fallback_dt) or datetime.utcnow().isoformat()

    entries = o.get("entries", [])
    sqft = sum(float(e.get("SQFT", 0) or e.get("sqft", 0) or 0) for e in entries)
    total_bill, total_paid, balance_due, booking_amt = resolve_order_financials(o, sqft)

    return jsonify({
        "order_id": str(o["_id"]),
        "customer_name": cust.get("name", "Unknown Client"),
        "name": cust.get("name", "Unknown Client"),
        "phone": cust.get("phone", ""),
        "address": cust.get("address", ""),
        "showroom": cust.get("showroom", "Anna Nagar"),
        "status": o.get("status", "Fabric Order Pending"),
        "status_dates": o.get("status_dates", {}),
        "measurement_date": o.get("measurement_date", ""),
        "due_date": o.get("due_date", ""),
        "created_at": to_iso(o.get("created_at")),
        "completed_at": completed_at,
        "delay_comment": o.get("delay_comment", ""),
        "products": o.get("products", ["Curtains"]),
        "tailor": o.get("tailor") or "None",
        "fitter": o.get("fitter") or "None",
        "entries": entries,
        "payments": o.get("payments", []),
        "booking_amount_taken": bool(o.get("booking_amount_taken", False) or booking_amt > 0),
        "booking_amount": booking_amt,
        "booking_date": o.get("booking_date", ""),
        "total_bill": total_bill,
        "balance": balance_due,
        "balance_due": balance_due,
        "paid_amount": total_paid,
        "quotation_id": o.get("quotation_id", "")
    })

# ================= UPDATE ORDER =================

@app.route("/api/orders/<oid>", methods=["PUT"])
@token_required
def update_order(oid):
    data = request.json or {}

    existing_order = db.orders.find_one({"_id": oid})
    if not existing_order and ObjectId.is_valid(oid):
        existing_order = db.orders.find_one({"_id": ObjectId(oid)})
    if not existing_order:
        return jsonify({"error": "Order not found"}), 404

    cid = existing_order.get("customer_id")
    if isinstance(cid, str) and ObjectId.is_valid(cid):
        cid = ObjectId(cid)

    if cid:
        cust_updates = {}
        if "customer_name" in data: cust_updates["name"] = data.get("customer_name")
        if "phone" in data: cust_updates["phone"] = data.get("phone")
        if "address" in data: cust_updates["address"] = data.get("address")
        if "showroom" in data: cust_updates["showroom"] = data.get("showroom")
        if cust_updates:
            cust_updates["updated_at"] = datetime.utcnow()
            db.customers.update_one({"_id": cid}, {"$set": cust_updates})

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
    if "total_bill" in data: update_fields["total_bill"] = float(data.get("total_bill", 0) or 0)
    if "products" in data: update_fields["products"] = data.get("products")
    if "status_dates" in data: update_fields["status_dates"] = data.get("status_dates")
    if "measurement_date" in data: update_fields["measurement_date"] = data.get("measurement_date")
    if "booking_amount" in data: update_fields["booking_amount"] = float(data.get("booking_amount", 0) or 0)
    if "booking_date" in data: update_fields["booking_date"] = data.get("booking_date")
    if "booking_amount_taken" in data: update_fields["booking_amount_taken"] = bool(data.get("booking_amount_taken", False))

    target_id = existing_order["_id"]
    db.orders.update_one({"_id": target_id}, {"$set": update_fields})

    return jsonify({"status": "updated"})

# ================= DELETE ORDER =================

@app.route("/api/orders/<oid>", methods=["DELETE"])
@token_required
def delete_order(oid):
    if request.user.get("role") != "admin":
        return jsonify({"error": "Not allowed"}), 403
    result = db.orders.delete_one({"_id": oid})
    if result.deleted_count == 0 and ObjectId.is_valid(oid):
        db.orders.delete_one({"_id": ObjectId(oid)})
    return jsonify({"status": "deleted"})


# ================= BILLING =================

@app.route("/api/billing", methods=["GET"])
@token_required
def billing_data():
    if request.user.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

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

    pipeline = []
    if match_filter:
        pipeline.append({"$match": match_filter})

    pipeline.extend([
        {
            "$addFields": {
                "customer_id_obj": {
                    "$cond": {
                        "if": {
                            "$and": [
                                {"$eq": [{"$type": "$customer_id"}, "string"]},
                                {"$eq": [{"$strLenCP": "$customer_id"}, 24]}
                            ]
                        },
                        "then": {"$toObjectId": "$customer_id"},
                        "else": "$customer_id"
                    }
                }
            }
        },
        {
            "$project": {
                "customer_id_obj": 1, "tailor": 1, "fitter": 1, 
                "entries": 1, "payment_status": 1, "status": 1,
                "payments": 1, "total_bill": 1, "created_at": 1
            }
        },
        {
            "$lookup": {
                "from": "customers",
                "localField": "customer_id_obj",
                "foreignField": "_id",
                "as": "customer"
            }
        },
        {
            "$unwind": {
                "path": "$customer",
                "preserveNullAndEmptyArrays": True
            }
        }
    ])

    orders = list(db.orders.aggregate(pipeline, allowDiskUse=True))
    result = []

    dev_rates = {"Pleated": 90, "Eyelet": 130, "Ripple": 120}

    for o in orders:
        cust = o.get("customer") or {}
        tailor = o.get("tailor") or "None"
        fitter = o.get("fitter") or "None"
        stitching_total = fitting_total = 0
        stitching_breakup = []
        fitting_breakup = []

        for e in o.get("entries", []):
            try:
                stitch_type = (e.get("stitch_type") or e.get("Stitch") or "").strip()
                panels = int(float(e.get("panels") or e.get("Panels") or 0))
                sqft = float(e.get("sqft") or e.get("SQFT") or 0)
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
            except (ValueError, TypeError):
                continue

        result.append({
            "order_id": str(o.get("_id")),
            "customer_name": cust.get("name", "Unknown Client"),
            "tailor": tailor, "fitter": fitter,
            "stitching_total": round(stitching_total, 2),
            "fitting_total": round(fitting_total, 2),
            "grand_total": round(stitching_total + fitting_total, 2),
            "payment_status": o.get("payment_status", "Pending"),
            "stitching_breakup": stitching_breakup,
            "fitting_breakup": fitting_breakup,
            "payments": o.get("payments", []),
            "paid_total": sum(float(p.get("amount", 0) or 0) for p in o.get("payments", [])),
            "total_bill": float(o.get("total_bill", 0) or 0)
        })
    return jsonify(result)

@app.route("/api/billing/<oid>/status", methods=["PATCH"])
@token_required
def update_billing_status(oid):
    if request.user.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json or {}
    new_status = data.get("payment_status")
    if new_status not in ["Paid", "Pending"]:
        return jsonify({"error": "Invalid status"}), 400

    result = db.orders.update_one({"_id": oid}, {"$set": {"payment_status": new_status}})
    if result.matched_count == 0 and ObjectId.is_valid(oid):
        db.orders.update_one({"_id": ObjectId(oid)}, {"$set": {"payment_status": new_status}})

    return jsonify({"status": "updated"})

@app.route("/api/orders/<oid>/payments", methods=["POST"])
@token_required
def add_order_payment(oid):
    data = request.json or {}
    amount = float(data.get("amount", 0))
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

# ================= AI VISUALIZER (SERVER-SIDE) =================

@app.route("/api/ai/preview", methods=["POST"])
@token_required
def generate_ai_preview():
    try:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return jsonify({"error": "Backend API_KEY not configured"}), 500

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
        
        quotes = list(db.quotations.find(query).sort("date", -1))
        for q in quotes:
            q["_id"] = str(q["_id"])
        return jsonify(quotes)
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
            except:
                pass
        if not q:
            return jsonify({"error": "Quotation not found"}), 404
        
        q["_id"] = str(q["_id"])
        return jsonify(q)
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
            "fabric_discount_percent": float(data.get("fabric_discount_percent", 0) or 0),
            "additional_discount": float(data.get("additional_discount", 0) or 0),
            "gst_percent": float(data.get("gst_percent", 0) or 0),
            "terms_conditions": data.get("terms_conditions", ""),
            "total_amount": float(data.get("total_amount", 0) or 0)
        }
        
        db.quotations.insert_one(payload)
        return jsonify({"status": "created", "id": qid})
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
            "fabric_discount_percent": float(data.get("fabric_discount_percent", 0) or 0),
            "additional_discount": float(data.get("additional_discount", 0) or 0),
            "gst_percent": float(data.get("gst_percent", 0) or 0),
            "terms_conditions": data.get("terms_conditions", ""),
            "total_amount": float(data.get("total_amount", 0) or 0)
        }
        
        result = db.quotations.update_one({"id": qid}, {"$set": payload})
        if result.matched_count == 0:
            return jsonify({"error": "Quotation not found"}), 404
            
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/quotations/<qid>", methods=["DELETE"])
@token_required
def delete_quotation(qid):
    try:
        result = db.quotations.delete_one({"id": qid})
        if result.deleted_count == 0:
            return jsonify({"error": "Quotation not found"}), 404
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/images/gridfs/<fid>")
def get_gridfs_image(fid):
    try:
        file = fs.get(ObjectId(fid))
        return file.read(), 200, {'Content-Type': 'image/jpeg'}
    except:
        return "Not found", 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
