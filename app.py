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
from bson import ObjectId

# ================= APP CONFIG =================

app = Flask(__name__)

# ✅ KEEPING CORS EXACTLY SAME (UNCHANGED)
CORS(app,
     supports_credentials=True,
     origins=["https://nestjs-fabric-app.vercel.app", "http://localhost:3000/"],)

SECRET_KEY = os.getenv("JWT_SECRET", "super_secret_key")

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["fabric_app"]

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

        # 🔄 STATUS MAPPING
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


# ================= CREATE ORDER =================

@app.route("/api/orders", methods=["POST"])
@token_required
def create_order():

    data = request.json
    cust = data["customer"]
    entries = data["entries"]

    # -------- CUSTOMER --------
    customer = db.customers.find_one({"phone": cust["phone"]})

    if customer:
        cid = customer["_id"]
        db.customers.update_one({"_id": cid}, {"$set": cust})
    else:
        cid = db.customers.insert_one({
            **cust,
            "created_at": datetime.utcnow()
        }).inserted_id

    # -------- ORDER --------
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


# ================= LIST ORDERS =================

@app.route("/api/orders/list")
@token_required
def list_orders():

    out = []

    for o in db.orders.find({}).sort("created_at", DESCENDING):

        # Safe customer resolution
        cust = db.customers.find_one({"_id": o.get("customer_id")})
        if not cust:
            try:
                cust = db.customers.find_one(
                    {"_id": ObjectId(o.get("customer_id"))}
                )
            except:
                continue

        raw_status = o.get("status", "")

        # 🔄 STATUS MAPPING
        if raw_status == "Pending":
            mapped_status = "Fabric Order Pending"
        elif raw_status == "Cutting":
            mapped_status = "Fabric In Transit"
        else:
            mapped_status = raw_status

        sqft = 0
        panels = 0

        for e in o.get("entries") or []:
            sqft += float(e.get("SQFT", 0) or 0)
            panels += int(float(e.get("Panels", 0) or 0))

        out.append({
            "order_id": o["_id"],
            "name": cust.get("name"),
            "phone": cust.get("phone"),
            "status": mapped_status,
            "created_at": o.get("created_at"),
            "updated_at": o.get("updated_at"),
            "due_date": o.get("due_date"),
            "showroom": cust.get("showroom", ""),
            "tailor": o.get("tailor", ""),
            "fitter": o.get("fitter", ""),
            "item_count": len(o.get("entries") or []),
            "panels": panels,
            "sqft": round(sqft, 2)
        })

    return jsonify(out)


# ================= GET ORDER (EDIT) =================

@app.route("/api/orders/<oid>", methods=["GET"])
@token_required
def get_order(oid):

    o = db.orders.find_one({"_id": oid})
    if not o:
        return jsonify({"error": "Not found"}), 404

    # Safe customer resolution
    cust = db.customers.find_one({"_id": o.get("customer_id")})
    if not cust:
        try:
            cust = db.customers.find_one(
                {"_id": ObjectId(o.get("customer_id"))}
            )
        except:
            cust = {}

    # Normalize Images
    for e in o.get("entries", []):
        if "Images" not in e:
            e["Images"] = []

    # 🔄 STATUS MAPPING
    status = o.get("status")
    if status == "Pending":
        status = "Fabric Order Pending"
    elif status == "Cutting":
        status = "Fabric In Transit"

    return jsonify({
        "order_id": o["_id"],
        "name": cust.get("name", ""),
        "phone": cust.get("phone", ""),
        "address": cust.get("address", ""),
        "showroom": cust.get("showroom", ""),
        "status": status,
        "due_date": o.get("due_date"),
        "tailor": o.get("tailor", ""),
        "fitter": o.get("fitter", ""),
        "entries": o.get("entries", [])
    })


# ================= UPDATE ORDER =================

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
            "tailor": data.get("tailor", ""),
            "fitter": data.get("fitter", ""),
            "updated_at": datetime.utcnow()
        }}
    )

    return jsonify({"status": "updated"})


# ================= DELETE ORDER =================

@app.route("/api/orders/<oid>", methods=["DELETE"])
@token_required
def delete_order(oid):

    if request.user["role"] != "admin":
        return jsonify({"error": "Not allowed"}), 403

    db.orders.delete_one({"_id": oid})
    return jsonify({"status": "deleted"})


# ================= BILLING (FIXED VERSION) =================

@app.route("/api/billing")
@token_required
def billing_data():
    if request.user["role"] != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    result = []
    # Loop through all orders to calculate fabrication and installation costs
    for o in db.orders.find({}):
        
        # --- SAFE CUSTOMER RESOLUTION (The fix) ---
        cust_id = o.get("customer_id")
        cust = db.customers.find_one({"_id": cust_id})
        
        if not cust and cust_id:
            try:
                # If direct lookup fails, try converting to ObjectId
                cust = db.customers.find_one({"_id": ObjectId(str(cust_id))})
            except Exception:
                pass
        
        # If we still can't find a customer, we skip this record to prevent errors
        if not cust:
            continue

        tailor = o.get("tailor", "")
        fitter = o.get("fitter", "")

        stitching_total = 0
        fitting_total = 0
        stitching_breakup = []
        fitting_breakup = []

        # Process each window unit in the order
        for e in o.get("entries", []):
            stitch_type = (e.get("Stitch") or "").strip()
            panels = int(float(e.get("Panels") or 0))
            sqft = float(e.get("SQFT") or 0)
            window_name = (e.get("Window") or "").strip()

            # Installation / Fitting Logic
            if window_name:
                # Charge more for Double Tracks
                rate = 200 if "Double" in window_name else 150
                amount = rate
                fitting_total += amount
                fitting_breakup.append({
                    "type": window_name,
                    "qty": 1,
                    "rate": rate,
                    "amount": amount
                })

            # Fabrication / Stitching Logic
            rate = 0
            amount = 0
            if stitch_type in ["Pleated", "Eyelet", "Ripple"]:
                if panels > 0:
                    if tailor == "Dev":
                        rate = {"Pleated": 90, "Eyelet": 130, "Ripple": 120}.get(stitch_type, 0)
                    elif tailor == "Dinesh":
                        rate = 90
                    amount = panels * rate
            elif "Roman" in stitch_type:
                if sqft > 0:
                    if tailor == "Dev":
                        rate = 125
                    elif tailor == "Dinesh":
                        rate = 100
                    amount = sqft * rate

            if amount > 0:
                qty_value = round(sqft, 2) if "Roman" in stitch_type else panels
                stitching_total += amount
                stitching_breakup.append({
                    "type": stitch_type,
                    "qty": qty_value,
                    "rate": rate,
                    "amount": amount
                })

        result.append({
            "order_id": str(o.get("_id")),
            "customer": cust.get("name", "Unknown Client"),
            "tailor": tailor,
            "fitter": fitter,
            "stitching_total": round(stitching_total, 2),
            "fitting_total": round(fitting_total, 2),
            "grand_total": round(stitching_total + fitting_total, 2),
            "payment_status": o.get("payment_status", "Pending"),
            "paid_date": o.get("paid_date"),
            "stitching_breakup": stitching_breakup,
            "fitting_breakup": fitting_breakup
        })

    return jsonify(result)

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

            t = o.get("tailor", "")
            f = o.get("fitter", "")

            if t:
                summary["tailors"][t] = summary["tailors"].get(t, 0) + 1
            if f:
                summary["fitters"][f] = summary["fitters"].get(f, 0) + 1

    return jsonify(summary)

fs = GridFS(db)

@app.route("/api/images/gridfs/<fid>")
def get_gridfs_image(fid):
    try:
        # Some IDs might come with a suffix like :1, we strip it
        clean_id = fid.split(':')[0]
        file = fs.get(ObjectId(clean_id))
        return file.read(), 200, {'Content-Type': 'image/jpeg'}
    except Exception as e:
        print(f"GridFS Error: {e}")
        return "Image not found", 404

# ================= RUN =================

if __name__ == "__main__":
    app.run(debug=True, port=5000)
