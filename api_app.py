# ================= IMPORTS =================
import os
import io
import uuid
import json
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
from gridfs import GridFS
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
import jwt
from functools import wraps

# ================= APP CONFIG =================

app = Flask(__name__)
CORS(app,
     supports_credentials=True,
     origins=["http://localhost:3000"])  # ✅ UNCHANGED (as requested)

SECRET_KEY = os.getenv("JWT_SECRET", "super_secret_key")

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["fabric_app"]
fs = GridFS(db)

PDF_CACHE = {}

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

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization")
        if not token:
            return jsonify({"error": "Token missing"}), 401
        try:
            token = token.split(" ")[1]
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            request.user = data
        except:
            return jsonify({"error": "Invalid token"}), 401
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

    orders = list(db.orders.find({}))

    fabric_pending = stitching = installation = completed = 0

    for o in orders:
        status = o.get("status", "")
        if status == "Pending":
            status = "Fabric Order Pending"
        elif status == "Cutting":
            status = "Fabric In Transit"

        if status == "Fabric Order Pending":
            fabric_pending += 1
        elif status == "Stitching":
            stitching += 1
        elif status == "Hardware/Material Installation":
            installation += 1
        elif status == "Completed":
            completed += 1

    return jsonify({
        "orders": len(orders),
        "fabric_pending": fabric_pending,
        "stitching": stitching,
        "installation": installation,
        "completed": completed
    })


# ================= ORDERS =================

@app.route("/api/orders", methods=["POST"])
@token_required
def create_order():

    data = request.json
    cust = data["customer"]
    entries = data["entries"]

    customer = db.customers.find_one({"phone": cust["phone"]})

    if customer:
        cid = customer["_id"]
        db.customers.update_one({"_id": cid}, {"$set": cust})
    else:
        cid = db.customers.insert_one({
            **cust,
            "created_at": datetime.utcnow()
        }).inserted_id

    order = {
        "_id": str(uuid.uuid4()),
        "customer_id": cid,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "status": data.get("status", "Fabric Order Pending"),
        "due_date": data.get("due_date"),
        "tailor": data.get("tailor", ""),
        "fitter": data.get("fitter", ""),
        "entries": entries
    }

    db.orders.insert_one(order)
    return jsonify({"status": "success"})


@app.route("/api/orders/list")
@token_required
def list_orders():
    out = []
    for o in db.orders.find({}).sort("created_at", DESCENDING):

        cust = db.customers.find_one({"_id": o["customer_id"]})
        if not cust:
            continue

        sqft = sum(float(e.get("SQFT", 0)) for e in o.get("entries", []))
        panels = sum(int(float(e.get("Panels", 0))) for e in o.get("entries", []))

        out.append({
            "order_id": o["_id"],
            "name": cust.get("name"),
            "phone": cust.get("phone"),
            "status": o.get("status"),
            "created_at": o.get("created_at"),
            "due_date": o.get("due_date"),
            "showroom": cust.get("showroom"),
            "tailor": o.get("tailor"),
            "fitter": o.get("fitter"),
            "panels": panels,
            "sqft": round(sqft, 2)
        })
    return jsonify(out)


@app.route("/api/orders/<oid>", methods=["GET"])
@token_required
def get_order(oid):

    o = db.orders.find_one({"_id": oid})
    if not o:
        return jsonify({"error": "Not found"}), 404

    cust = db.customers.find_one({"_id": o["customer_id"]})

    return jsonify({
        "order_id": o["_id"],
        "customer": cust,
        "status": o.get("status"),
        "due_date": o.get("due_date"),
        "tailor": o.get("tailor"),
        "fitter": o.get("fitter"),
        "entries": o.get("entries", [])
    })


@app.route("/api/orders/<oid>", methods=["PUT"])
@token_required
def update_order(oid):

    data = request.json

    db.orders.update_one(
        {"_id": oid},
        {"$set": {
            "entries": data.get("entries"),
            "status": data.get("status"),
            "due_date": data.get("due_date"),
            "tailor": data.get("tailor"),
            "fitter": data.get("fitter"),
            "updated_at": datetime.utcnow()
        }}
    )

    PDF_CACHE.pop(f"order_pdf:{oid}", None)
    return jsonify({"status": "updated"})


@app.route("/api/orders/<oid>", methods=["DELETE"])
@token_required
def delete_order(oid):
    if request.user["role"] != "admin":
        return jsonify({"error": "Not allowed"}), 403

    db.orders.delete_one({"_id": oid})
    return jsonify({"status": "deleted"})


@app.route("/api/orders/<oid>/status", methods=["PUT"])
@token_required
def update_order_status(oid):

    new_status = request.json.get("status")
    if new_status not in STATUSES:
        return jsonify({"error": "Invalid status"}), 400

    db.orders.update_one(
        {"_id": oid},
        {"$set": {
            "status": new_status,
            "updated_at": datetime.utcnow()
        }}
    )

    return jsonify({"status": "updated"})


# ================= BILLING =================

@app.route("/api/billing")
@token_required
def billing_data():

    if request.user["role"] != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    orders = list(db.orders.find({}))
    result = []

    for o in orders:

        cust = db.customers.find_one({"_id": o.get("customer_id")})
        if not cust:
            continue

        stitching_total = fitting_total = 0
        stitching_breakup = []
        fitting_breakup = []

        for e in o.get("entries", []):
            panels = int(float(e.get("Panels") or 0))
            sqft = float(e.get("SQFT") or 0)

            if panels > 0:
                amount = panels * 100
                stitching_total += amount
                stitching_breakup.append({
                    "type": e.get("Stitch"),
                    "qty": panels,
                    "rate": 100,
                    "amount": amount
                })

            if sqft > 0:
                amount = sqft * 50
                fitting_total += amount
                fitting_breakup.append({
                    "type": e.get("Window"),
                    "qty": sqft,
                    "rate": 50,
                    "amount": amount
                })

        result.append({
            "order_id": str(o["_id"]),
            "customer": cust.get("name"),
            "stitching_total": stitching_total,
            "fitting_total": fitting_total,
            "grand_total": stitching_total + fitting_total,
            "stitching_breakup": stitching_breakup,
            "fitting_breakup": fitting_breakup,
            "payment_status": o.get("payment_status", "Pending"),
            "paid_date": o.get("paid_date")
        })

    return jsonify(result)


@app.route("/api/billing/<oid>/mark-paid", methods=["PUT"])
@token_required
def mark_paid(oid):

    if request.user["role"] != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    db.orders.update_one(
        {"_id": oid},
        {"$set": {
            "payment_status": "Paid",
            "paid_date": datetime.utcnow()
        }}
    )

    return jsonify({"status": "updated"})


# ================= ANALYTICS =================

@app.route("/api/analytics/stages")
@token_required
def stage_analytics():
    pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
    return jsonify(list(db.orders.aggregate(pipeline)))


@app.route("/api/analytics/daily-assignments")
@token_required
def daily_assignments():

    today = datetime.utcnow().date()
    summary = {"tailors": {}, "fitters": {}}

    for o in db.orders.find({}):
        created = o.get("created_at")
        if created and created.date() == today:

            t = o.get("tailor")
            f = o.get("fitter")

            if t:
                summary["tailors"][t] = summary["tailors"].get(t, 0) + 1
            if f:
                summary["fitters"][f] = summary["fitters"].get(f, 0) + 1

    return jsonify(summary)


# ================= RUN =================

if __name__ == "__main__":
    app.run(debug=True, port=5000)
