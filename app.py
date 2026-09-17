
# ================= IMPORTS =================
import os
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
import jwt
from gridfs import GridFS
import google.generativeai as genai
import base64
import re

# ================= APP CONFIG =================

app = Flask(__name__)

# CORS configuration
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

SECRET_KEY = os.getenv("JWT_SECRET", "super_secret_key")

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["fabric_app"]
fs = GridFS(db)

STATUSES = [
    "Fabric Order Pending",
    "Fabric In Transit",
    "Stitching",
    "Hardware/Material Installation",
    "Completed"
]

# ================= AUTH =================

USERS = {
    "adminqd": {"password": "adminQD", "role": "admin"},
    "staffqd": {"password": "staffQD", "role": "staff"}
}

# ================= REPLACED AUTH DECORATOR =================

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Token missing or malformed"}), 401
        
        try:
            # Extract token from "Bearer <token>"
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
def login():
    data = request.json
    username = data.get("username")
    password = data.get("password")

    if username in USERS and USERS[username]["password"] == password:
        token = jwt.encode({
            "username": username,
            "role": USERS[username]["role"],
            "exp": datetime.utcnow() + timedelta(hours=10)
        }, SECRET_KEY, algorithm="HS256")

        return jsonify({"token": token})

    return jsonify({"error": "Invalid credentials"}), 401


# ================= DASHBOARD =================

@app.route("/api/dashboard/kpis")
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
    
    # Initialize counts
    counts = {
        "orders": results["total"][0]["count"] if results["total"] else 0,
        "fabric_pending": 0, "stitching": 0, "installation": 0, "completed": 0, "transit": 0
    }
    
    # Map results from the single DB trip
    for item in results["by_status"]:
        status = item["_id"]
        count = item["count"]
        if status in ["Fabric Order Pending", "Pending"]: counts["fabric_pending"] += count
        elif status == "Stitching": counts["stitching"] = count
        elif status in ["Hardware/Material Installation", "Installation"]: counts["installation"] = count
        elif status == "Completed": counts["completed"] = count
        elif status in ["Fabric In Transit", "Cutting"]: counts["transit"] = count

    return jsonify(counts)

# ================= CREATE ORDER =================

@app.route("/api/orders", methods=["POST"])
@app.route("/orders", methods=["POST"])
@token_required
def create_order():
    data = request.json

    # ---- Validate required fields ----
    if not data.get("customer_name") and not data.get("name"):
        return jsonify({"error": "Customer name required"}), 400

    cust_name = (data.get("customer_name") or data.get("name") or "").strip()
    cust_phone = (data.get("phone") or "").strip()
    cust_address = (data.get("address") or "").strip()
    cust_showroom = (data.get("showroom") or "").strip()

    cust = {
        "name": cust_name,
        "phone": cust_phone,
        "address": cust_address,
        "showroom": cust_showroom
    }

    entries = data.get("entries", [])

    # ---- Find or create customer ----
    customer = None
    if cust_phone:
        customer = db.customers.find_one({"phone": cust_phone})
    if not customer and cust_name:
        customer = db.customers.find_one({"name": cust_name})

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

    order_id = data.get("order_id") or data.get("_id") or str(uuid.uuid4())

    # Auto-link quotation if customer has a saved quotation and order doesn't have quotation items yet
    quotation_data = data.get("quotation_data")
    quotation_id = data.get("quotation_id", "")
    if not quotation_data or not quotation_data.get("items") or len(quotation_data.get("items")) == 0:
        found_quote = None
        if quotation_id:
            found_quote = db.quotations.find_one({"id": quotation_id})
        if not found_quote and cust_phone:
            found_quote = db.quotations.find_one({"phone": cust_phone})
        if not found_quote and cust_name:
            found_quote = db.quotations.find_one({"customer_name": {"$regex": f"^{re.escape(cust_name)}$", "$options": "i"}})
        if found_quote:
            quotation_data = {
                "id": found_quote.get("id"),
                "customer_name": found_quote.get("customer_name"),
                "phone": found_quote.get("phone", ""),
                "date": found_quote.get("date", ""),
                "items": found_quote.get("items", []),
                "additional_discount": float(found_quote.get("additional_discount", 0) or 0),
                "gst_percent": float(found_quote.get("gst_percent", 0) or 0),
                "terms": found_quote.get("terms_conditions") or found_quote.get("terms", ""),
                "total_amount": float(found_quote.get("total_amount", 0) or 0)
            }
            quotation_id = found_quote.get("id", "")

    total_bill = float(data.get("total_bill", 0) or 0)
    if total_bill == 0 and quotation_data and quotation_data.get("total_amount"):
        total_bill = float(quotation_data.get("total_amount", 0) or 0)

    # ---- ALWAYS store customer fields directly on order as well as customer_id ----
    order = {
        "_id": order_id,
        "order_id": order_id,
        "customer_id": ObjectId(cid) if cid and ObjectId.is_valid(str(cid)) else str(cid),
        "customer_name": cust_name,
        "name": cust_name,
        "phone": cust_phone,
        "address": cust_address,
        "showroom": cust_showroom,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "status": status,
        "status_dates": data.get("status_dates", {}),
        "status_history": data.get("status_history", []),
        "measurement_date": data.get("measurement_date", ""),
        "booking_amount_taken": bool(data.get("booking_amount_taken")),
        "booking_amount": float(data.get("booking_amount", 0) or 0),
        "booking_date": data.get("booking_date", ""),
        "products": data.get("products", ["Curtains"]),
        "due_date": data.get("due_date"),
        "completed_at": completed_at,
        "delay_comment": data.get("delay_comment", ""),
        "tailor": data.get("tailor") or "None",
        "fitter": data.get("fitter") or "None",
        "entries": entries,
        "quotation_data": quotation_data,
        "quotation_id": quotation_id,
        "payments": data.get("payments", []),
        "total_bill": total_bill
    }

    db.orders.update_one({"_id": order_id}, {"$set": order}, upsert=True)

    return jsonify({"status": "success", "order_id": order["_id"]})


# ================= LIST ORDERS =================

@app.route("/api/orders/list")
@token_required
def list_orders():
    search_query = request.args.get("search", "").strip()
    status_query = request.args.get("status", "").strip()
    include_completed_str = request.args.get("include_completed", "true").strip().lower()
    include_completed = include_completed_str in ["true", "1", "yes"]

    # Pagination params
    page = int(request.args.get("page", 1) or 1)
    limit = int(request.args.get("limit", 0) or 0) # 0 means all (up to 1000)

    query_filter = {}

    if status_query and status_query.upper() != "ALL":
        query_filter["status"] = status_query
    elif not include_completed:
        query_filter["status"] = {"$ne": "Completed"}

    if search_query:
        # Match customers by name, phone, or showroom
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

        # Build search or conditions safely
        or_conditions = [
            {"customer_id": {"$in": cust_ids}},
            {"status": {"$regex": search_query, "$options": "i"}},
            {"tailor": {"$regex": search_query, "$options": "i"}},
            {"fitter": {"$regex": search_query, "$options": "i"}},
            {"customer_name": {"$regex": search_query, "$options": "i"}},
            {"phone": {"$regex": search_query, "$options": "i"}},
            {"showroom": {"$regex": search_query, "$options": "i"}}
        ]

        if ObjectId.is_valid(search_query):
            or_conditions.append({"_id": ObjectId(search_query)})

        if query_filter:
            query_filter = {"$and": [query_filter, {"$or": or_conditions}]}
        else:
            query_filter = {"$or": or_conditions}

    pipeline = [
        {"$match": query_filter},

        {
            "$addFields": {
                "customer_id_obj": {
                    "$cond": {
                        "if": {"$eq": [{"$type": "$customer_id"}, "objectId"]},
                        "then": "$customer_id",
                        "else": {
                            "$cond": {
                                "if": {"$eq": [{"$type": "$customer_id"}, "string"]},
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

        {"$unwind": {"path": "$customer_info", "preserveNullAndEmptyArrays": True}},
        {"$sort": {"created_at": -1}}
    ]

    if limit > 0:
        skip_count = max(0, (page - 1) * limit)
        if skip_count > 0:
            pipeline.append({"$skip": skip_count})
        pipeline.append({"$limit": limit})
    else:
        pipeline.append({"$limit": 1000})

    orders = list(db.orders.aggregate(pipeline))

    out = []

    for o in orders:
        cust = o.get("customer_info") or {}
        entries = o.get("entries") or []

        sqft = sum(float(e.get("SQFT", 0) or 0) for e in entries)

        # Dynamic fallback for completed orders
        completed_at = o.get("completed_at") or ""
        if o.get("status", "").strip().lower() == "completed" and not completed_at:
            fallback_dt = o.get("updated_at") or o.get("created_at")
            if fallback_dt:
                if isinstance(fallback_dt, datetime):
                    completed_at = fallback_dt.isoformat()
                else:
                    completed_at = str(fallback_dt)
            else:
                completed_at = datetime.utcnow().isoformat()

        cust_name = cust.get("name") or o.get("customer_name") or o.get("name") or "Unknown Client"
        cust_phone = cust.get("phone") or o.get("phone", "")
        cust_address = cust.get("address") or o.get("address", "")
        cust_showroom = cust.get("showroom") or o.get("showroom", "")

        quotation_data = o.get("quotation_data")
        quotation_id = o.get("quotation_id", "")
        if not quotation_data or not quotation_data.get("items") or len(quotation_data.get("items")) == 0:
            found_quote = None
            if quotation_id:
                found_quote = db.quotations.find_one({"id": quotation_id})
            if not found_quote and cust_phone:
                found_quote = db.quotations.find_one({"phone": cust_phone})
            if not found_quote and cust_name and cust_name != "Unknown Client":
                found_quote = db.quotations.find_one({"customer_name": {"$regex": f"^{re.escape(cust_name)}$", "$options": "i"}})
            if found_quote:
                quotation_data = {
                    "id": found_quote.get("id"),
                    "customer_name": found_quote.get("customer_name"),
                    "phone": found_quote.get("phone", ""),
                    "date": found_quote.get("date", ""),
                    "items": found_quote.get("items", []),
                    "additional_discount": float(found_quote.get("additional_discount", 0) or 0),
                    "gst_percent": float(found_quote.get("gst_percent", 0) or 0),
                    "terms": found_quote.get("terms_conditions") or found_quote.get("terms", ""),
                    "total_amount": float(found_quote.get("total_amount", 0) or 0)
                }
                quotation_id = found_quote.get("id", "")

        total_bill = float(o.get("total_bill", 0) or 0)
        if total_bill == 0 and quotation_data and quotation_data.get("total_amount"):
            total_bill = float(quotation_data.get("total_amount", 0) or 0)

        # Check if booking advance / deposit is in payments
        order_payments = o.get("payments", [])
        booking_taken = bool(o.get("booking_amount_taken", False))
        booking_amt = float(o.get("booking_amount", 0) or 0)
        booking_dt = o.get("booking_date", "")

        if not booking_amt and order_payments:
            for p in order_payments:
                meth = str(p.get("method", "")).lower()
                ref = str(p.get("reference", "")).lower()
                if "booking" in meth or "advance" in meth or "booking" in ref or "advance" in ref or "deposit" in meth or "deposit" in ref:
                    booking_amt = float(p.get("amount", 0) or 0)
                    booking_dt = p.get("date", "")
                    booking_taken = True
                    break
            if not booking_amt and len(order_payments) > 0 and float(order_payments[0].get("amount", 0) or 0) > 0:
                booking_amt = float(order_payments[0].get("amount", 0) or 0)
                booking_dt = order_payments[0].get("date", "")
                booking_taken = True

        if booking_amt > 0:
            booking_taken = True

        out.append({
            "order_id": str(o["_id"]),
            "name": cust_name,
            "customer_name": cust_name,
            "phone": cust_phone,
            "address": cust_address,
            "status": o.get("status", ""),
            "status_dates": o.get("status_dates", {}),
            "status_history": o.get("status_history", []),
            "created_at": o.get("created_at"),
            "due_date": o.get("due_date"),
            "measurement_date": o.get("measurement_date", ""),
            "booking_amount_taken": booking_taken,
            "booking_amount": booking_amt,
            "booking_date": booking_dt,
            "products": o.get("products", ["Curtains"]),
            "completed_at": completed_at,
            "delay_comment": o.get("delay_comment", ""),
            "showroom": cust_showroom,
            "tailor": o.get("tailor") or "None",
            "fitter": o.get("fitter") or "None",
            "item_count": len(entries),
            "entries": entries,
            "quotation_data": quotation_data,
            "quotation_id": quotation_id,
            "payments": order_payments,
            "total_bill": total_bill,
            "sqft": round(sqft, 2)
        })

    return jsonify(out)
# ================= REPLACE get_order in app.py =================
@app.route("/api/orders/<oid>")
@app.route("/orders/<oid>")
@token_required
def get_order(oid):
    o = db.orders.find_one({"_id": oid})
    if not o:
        o = db.orders.find_one({"order_id": oid})
    if not o and ObjectId.is_valid(oid):
        o = db.orders.find_one({"_id": ObjectId(oid)})

    if not o:
        return jsonify({"error": "Not found"}), 404

    # Robust ID lookup
    cid = o.get("customer_id")
    cust = None
    if cid:
        cust = db.customers.find_one({"_id": ObjectId(str(cid))}) if ObjectId.is_valid(str(cid)) else None
        if not cust:
            cust = db.customers.find_one({"_id": cid})

    if not cust:
        cust = {}

    cust_name = cust.get("name") or o.get("customer_name") or o.get("name") or "Unknown Client"
    cust_phone = cust.get("phone") or o.get("phone", "")
    cust_address = cust.get("address") or o.get("address", "")
    cust_showroom = cust.get("showroom") or o.get("showroom", "")

    # Auto-link quotation if customer has a saved quotation and order doesn't have quotation items yet
    quotation_data = o.get("quotation_data")
    quotation_id = o.get("quotation_id", "")
    if not quotation_data or not quotation_data.get("items") or len(quotation_data.get("items")) == 0:
        found_quote = None
        if quotation_id:
            found_quote = db.quotations.find_one({"id": quotation_id})
        if not found_quote and cust_phone:
            found_quote = db.quotations.find_one({"phone": cust_phone})
        if not found_quote and cust_name and cust_name != "Unknown Client":
            found_quote = db.quotations.find_one({"customer_name": {"$regex": f"^{re.escape(cust_name)}$", "$options": "i"}})
        if found_quote:
            quotation_data = {
                "id": found_quote.get("id"),
                "customer_name": found_quote.get("customer_name"),
                "phone": found_quote.get("phone", ""),
                "date": found_quote.get("date", ""),
                "items": found_quote.get("items", []),
                "additional_discount": float(found_quote.get("additional_discount", 0) or 0),
                "gst_percent": float(found_quote.get("gst_percent", 0) or 0),
                "terms": found_quote.get("terms_conditions") or found_quote.get("terms", ""),
                "total_amount": float(found_quote.get("total_amount", 0) or 0)
            }
            quotation_id = found_quote.get("id", "")

    total_bill = float(o.get("total_bill", 0) or 0)
    if total_bill == 0 and quotation_data and quotation_data.get("total_amount"):
        total_bill = float(quotation_data.get("total_amount", 0) or 0)

    # Map legacy field names to frontend expected names
    completed_at = o.get("completed_at") or ""
    if o.get("status", "").strip().lower() == "completed" and not completed_at:
        fallback_dt = o.get("updated_at") or o.get("created_at")
        if fallback_dt:
            if isinstance(fallback_dt, datetime):
                completed_at = fallback_dt.isoformat()
            else:
                completed_at = str(fallback_dt)
        else:
            completed_at = datetime.utcnow().isoformat()

    order_payments = o.get("payments", [])
    booking_taken = bool(o.get("booking_amount_taken", False))
    booking_amt = float(o.get("booking_amount", 0) or 0)
    booking_dt = o.get("booking_date", "")

    if not booking_amt and order_payments:
        for p in order_payments:
            meth = str(p.get("method", "")).lower()
            ref = str(p.get("reference", "")).lower()
            if "booking" in meth or "advance" in meth or "booking" in ref or "advance" in ref or "deposit" in meth or "deposit" in ref:
                booking_amt = float(p.get("amount", 0) or 0)
                booking_dt = p.get("date", "")
                booking_taken = True
                break
        if not booking_amt and len(order_payments) > 0 and float(order_payments[0].get("amount", 0) or 0) > 0:
            booking_amt = float(order_payments[0].get("amount", 0) or 0)
            booking_dt = order_payments[0].get("date", "")
            booking_taken = True

    if booking_amt > 0:
        booking_taken = True

    return jsonify({
        "order_id": str(o.get("_id")),
        "customer_name": cust_name,
        "name": cust_name,
        "phone": cust_phone,
        "address": cust_address,
        "showroom": cust_showroom,
        "status": o.get("status", "Fabric Order Pending"),
        "status_dates": o.get("status_dates", {}),
        "status_history": o.get("status_history", []),
        "due_date": o.get("due_date", ""),
        "measurement_date": o.get("measurement_date", ""),
        "booking_amount_taken": booking_taken,
        "booking_amount": booking_amt,
        "booking_date": booking_dt,
        "products": o.get("products", ["Curtains"]),
        "completed_at": completed_at,
        "delay_comment": o.get("delay_comment", ""),
        "tailor": o.get("tailor") or "None",
        "fitter": o.get("fitter") or "None",
        "entries": o.get("entries", []),
        "quotation_data": quotation_data,
        "quotation_id": quotation_id,
        "payments": order_payments, 
        "total_bill": total_bill 
    })

# ================= UPDATE ORDER =================

@app.route("/api/orders/<oid>", methods=["PUT"])
@app.route("/orders/<oid>", methods=["PUT"])
@token_required
def update_order(oid):
    data = request.json

    # ---- Check order exists ----
    existing_order = db.orders.find_one({"_id": oid})
    if not existing_order:
        existing_order = db.orders.find_one({"order_id": oid})
    if not existing_order and ObjectId.is_valid(oid):
        existing_order = db.orders.find_one({"_id": ObjectId(oid)})

    if not existing_order:
        # If not existing, create it via upsert
        return create_order()

    # ---- Handle customer update safely ----
    cid = existing_order.get("customer_id")
    cust_name = (data.get("customer_name") or data.get("name") or existing_order.get("customer_name") or existing_order.get("name") or "").strip()
    cust_phone = (data.get("phone") if data.get("phone") is not None else existing_order.get("phone", "")).strip()
    cust_address = (data.get("address") if data.get("address") is not None else existing_order.get("address", "")).strip()
    cust_showroom = (data.get("showroom") if data.get("showroom") is not None else existing_order.get("showroom", "")).strip()

    cust_updates = {}
    if cust_name:
        cust_updates["name"] = cust_name
    if cust_phone is not None:
        cust_updates["phone"] = cust_phone
    if cust_address is not None:
        cust_updates["address"] = cust_address
    if cust_showroom is not None:
        cust_updates["showroom"] = cust_showroom
    cust_updates["updated_at"] = datetime.utcnow()

    if cid:
        if isinstance(cid, str) and ObjectId.is_valid(cid):
            cid = ObjectId(cid)
        db.customers.update_one(
            {"_id": cid},
            {"$set": cust_updates}
        )
    else:
        customer = None
        if cust_phone:
            customer = db.customers.find_one({"phone": cust_phone})
        if not customer and cust_name:
            customer = db.customers.find_one({"name": cust_name})
        if customer:
            cid = customer["_id"]
            db.customers.update_one({"_id": cid}, {"$set": cust_updates})
        elif cust_name:
            cid = db.customers.insert_one({**cust_updates, "created_at": datetime.utcnow()}).inserted_id

    # Auto-link quotation if customer has a saved quotation and order doesn't have quotation items yet
    quotation_data = data.get("quotation_data", existing_order.get("quotation_data"))
    quotation_id = data.get("quotation_id", existing_order.get("quotation_id", ""))
    if not quotation_data or not quotation_data.get("items") or len(quotation_data.get("items")) == 0:
        found_quote = None
        if quotation_id:
            found_quote = db.quotations.find_one({"id": quotation_id})
        if not found_quote and cust_phone:
            found_quote = db.quotations.find_one({"phone": cust_phone})
        if not found_quote and cust_name:
            found_quote = db.quotations.find_one({"customer_name": {"$regex": f"^{re.escape(cust_name)}$", "$options": "i"}})
        if found_quote:
            quotation_data = {
                "id": found_quote.get("id"),
                "customer_name": found_quote.get("customer_name"),
                "phone": found_quote.get("phone", ""),
                "date": found_quote.get("date", ""),
                "items": found_quote.get("items", []),
                "additional_discount": float(found_quote.get("additional_discount", 0) or 0),
                "gst_percent": float(found_quote.get("gst_percent", 0) or 0),
                "terms": found_quote.get("terms_conditions") or found_quote.get("terms", ""),
                "total_amount": float(found_quote.get("total_amount", 0) or 0)
            }
            quotation_id = found_quote.get("id", "")

    # ---- Update order safely ----
    status = data.get("status") or existing_order.get("status", "")
    completed_at = data.get("completed_at") or ""
    if status.strip().lower() == "completed":
        if not completed_at:
            existing_completed_at = existing_order.get("completed_at") or ""
            if existing_completed_at:
                completed_at = existing_completed_at
            else:
                completed_at = datetime.utcnow().isoformat()
    else:
        completed_at = ""

    total_bill = data.get("total_bill", existing_order.get("total_bill", 0))
    if float(total_bill or 0) == 0 and quotation_data and quotation_data.get("total_amount"):
        total_bill = float(quotation_data.get("total_amount", 0) or 0)

    order_updates = {
        "customer_id": ObjectId(cid) if cid and ObjectId.is_valid(str(cid)) else str(cid) if cid else None,
        "customer_name": cust_name,
        "name": cust_name,
        "phone": cust_phone,
        "address": cust_address,
        "showroom": cust_showroom,
        "entries": data.get("entries", existing_order.get("entries", [])),
        "status": status,
        "status_dates": data.get("status_dates", existing_order.get("status_dates", {})),
        "status_history": data.get("status_history", existing_order.get("status_history", [])),
        "due_date": data.get("due_date", existing_order.get("due_date")),
        "measurement_date": data.get("measurement_date", existing_order.get("measurement_date", "")),
        "booking_amount_taken": bool(data.get("booking_amount_taken", existing_order.get("booking_amount_taken", False))),
        "booking_amount": float(data.get("booking_amount", existing_order.get("booking_amount", 0)) or 0),
        "booking_date": data.get("booking_date", existing_order.get("booking_date", "")),
        "products": data.get("products", existing_order.get("products", ["Curtains"])),
        "completed_at": completed_at,
        "delay_comment": data.get("delay_comment", existing_order.get("delay_comment", "")),
        "tailor": data.get("tailor", existing_order.get("tailor", "None")),
        "fitter": data.get("fitter", existing_order.get("fitter", "None")),
        "quotation_data": quotation_data,
        "quotation_id": quotation_id,
        "payments": data.get("payments", existing_order.get("payments", [])),
        "total_bill": total_bill,
        "updated_at": datetime.utcnow()
    }

    target_id = existing_order.get("_id") or oid
    db.orders.update_one(
        {"_id": target_id},
        {"$set": order_updates}
    )

    return jsonify({"status": "updated", "order_id": str(target_id)})

# ================= DELETE ORDER =================

@app.route("/api/orders/<oid>", methods=["DELETE"])
@token_required
def delete_order(oid):
    if request.user["role"] != "admin":
        return jsonify({"error": "Not allowed"}), 403
    db.orders.delete_one({"_id": oid})
    return jsonify({"status": "deleted"})


# ================= BILLING =================
@app.route("/api/billing")
@token_required
def billing_data():
    if request.user["role"] != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    start_date_str = request.args.get("start_date")
    end_date_str = request.args.get("end_date")
    
    match_filter = {}
    if start_date_str and end_date_str:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            # End date should include the whole day
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d") + timedelta(days=1)
            match_filter["created_at"] = {"$gte": start_date, "$lt": end_date}
        except ValueError:
            pass

    # ================= FIXED BILLING PIPELINE =================
    pipeline = []
    if match_filter:
        pipeline.append({"$match": match_filter})
        
    pipeline.extend([
        {
            "$addFields": {
                # Fixes the type mismatch: converts String IDs to ObjectIds if they look like one
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
            # Only fetch necessary fields to keep the query fast
            "$project": {
                "customer_id_obj": 1, "tailor": 1, "fitter": 1, 
                "entries": 1, "payment_status": 1, "status": 1,
                "payments": 1, "total_bill": 1, "created_at": 1
            }
        },
        {
            "$lookup": {
                "from": "customers",
                "localField": "customer_id_obj", # Join using the fixed ID
                "foreignField": "_id",
                "as": "customer"
            }
        },
        {
            # preserveNullAndEmptyArrays prevents orders from disappearing
            "$unwind": {
                "path": "$customer",
                "preserveNullAndEmptyArrays": True
            }
        }
    ])
    # ... rest of your logic remains the same
    
    orders = list(db.orders.aggregate(pipeline))
    result = []
    
    # Pre-define rates for faster access
    dev_rates = {"Pleated": 90, "Eyelet": 130, "Ripple": 120}

    for o in orders:
        cust = o.get("customer", {})
        tailor = o.get("tailor") or "None"
        fitter = o.get("fitter") or "None"
        stitching_total = fitting_total = 0
        stitching_breakup = []
        fitting_breakup = []

        for e in o.get("entries", []):
            try:
                # SUPPORT BOTH OLD & NEW SCHEMA
                stitch_type = (e.get("stitch_type") or e.get("Stitch") or "").strip()
                panels = int(float(e.get("panels") or e.get("Panels") or 0))
                sqft = float(e.get("sqft") or e.get("SQFT") or 0)
                window_name = (e.get("window_name") or e.get("Window") or "").strip()

                # Fitting calculation
                if window_name and fitter not in ["None", ""]:
                    rate = 200 if "Double" in window_name else 150
                    fitting_total += rate
                    fitting_breakup.append({"type": window_name, "qty": 1, "rate": rate, "amount": rate})

                # Stitching calculation
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
            "total_bill": o.get("total_bill", 0)
        })
    return jsonify(result)

@app.route("/api/billing/<oid>/status", methods=["PATCH"])
@token_required
def update_billing_status(oid):
    if request.user["role"] != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    
    data = request.json
    new_status = data.get("payment_status")
    if new_status not in ["Paid", "Pending"]:
        return jsonify({"error": "Invalid status"}), 400
        
    result = db.orders.update_one({"_id": oid}, {"$set": {"payment_status": new_status}})
    
    if result.matched_count == 0:
        # Fallback for ObjectId if it's not a UUID string
        if ObjectId.is_valid(oid):
            db.orders.update_one({"_id": ObjectId(oid)}, {"$set": {"payment_status": new_status}})
            
    return jsonify({"status": "updated"})

@app.route("/api/orders/<oid>/payments", methods=["POST"])
@token_required
def add_order_payment(oid):
    data = request.json
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
        # Obtain API key from environment ONLY
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return jsonify({"error": "Backend API_KEY not configured"}), 500
        
        genai.configure(api_key=api_key)
        
        data = request.json
        window_b64 = data.get("window_image")
        fabric_b64 = data.get("fabric_image")
        mode = data.get("mode", "Curtain")
        sub_type = data.get("sub_type", "Ripple Fold")
        style_prompt = data.get("style_prompt", "modern interior design")

        if not window_b64 or not fabric_b64:
            return jsonify({"error": "Missing image data"}), 400

        window_bytes = base64.b64decode(window_b64)
        fabric_bytes = base64.b64decode(fabric_b64)

        # Pro model usage for high quality
        model = genai.GenerativeModel('gemini-3-pro-image-preview')
        
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

        # Find the image part in the response
        for part in response.candidates[0].content.parts:
            if part.inline_data:
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
@app.route("/api/quotations", methods=["GET"])
@app.route("/quotations/list", methods=["GET"])
@app.route("/quotations", methods=["GET"])
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
@app.route("/quotations/<qid>", methods=["GET"])
@token_required
def get_quotation(qid):
    try:
        q = db.quotations.find_one({"id": qid})
        if not q:
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
@app.route("/quotations", methods=["POST"])
@token_required
def create_quotation():
    try:
        data = request.json
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
            "fabric_discount_percent": data.get("fabric_discount_percent", 0),
            "additional_discount": data.get("additional_discount", 0),
            "gst_percent": data.get("gst_percent", 0),
            "terms_conditions": data.get("terms_conditions", ""),
            "total_amount": data.get("total_amount", 0)
        }
        
        db.quotations.insert_one(payload)
        return jsonify({"status": "created", "id": qid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/quotations/<qid>", methods=["PUT"])
@app.route("/quotations/<qid>", methods=["PUT"])
@token_required
def update_quotation(qid):
    try:
        data = request.json
        payload = {
            "id": qid,
            "customer_name": data.get("customer_name"),
            "phone": data.get("phone", ""),
            "date": data.get("date") or datetime.utcnow().isoformat(),
            "items": data.get("items", []),
            "misc_charges": data.get("misc_charges", []),
            "fabric_discount_percent": data.get("fabric_discount_percent", 0),
            "additional_discount": data.get("additional_discount", 0),
            "gst_percent": data.get("gst_percent", 0),
            "terms_conditions": data.get("terms_conditions", ""),
            "total_amount": data.get("total_amount", 0)
        }
        
        result = db.quotations.update_one({"id": qid}, {"$set": payload}, upsert=True)
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/quotations/<qid>", methods=["DELETE"])
@app.route("/quotations/<qid>", methods=["DELETE"])
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
    app.run(debug=True, port=5000)
