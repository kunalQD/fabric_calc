
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

# ================= APP CONFIG =================

app = Flask(__name__)

# CORS configuration
CORS(
    app,
    resources={r"/api/*": {
        "origins": [
            "https://fabricapp.quiltanddrapes.com",
            "https://nestjs-fabric-app.vercel.app",
            "http://localhost:3000"
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


# ================= CREATE ORDER =================

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
        "tailor": data.get("tailor") or "None",
        "fitter": data.get("fitter") or "None",
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
        cust = db.customers.find_one({"_id": o.get("customer_id")})
        if not cust:
            try:
                cust = db.customers.find_one({"_id": ObjectId(o.get("customer_id"))})
            except:
                continue

        raw_status = o.get("status", "")
        if raw_status == "Pending":
            mapped_status = "Fabric Order Pending"
        elif raw_status == "Cutting":
            mapped_status = "Fabric In Transit"
        else:
            mapped_status = raw_status

        sqft = panels = 0
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
            "tailor": o.get("tailor") or "None",
            "fitter": o.get("fitter") or "None",
            "item_count": len(o.get("entries") or []),
            "panels": panels,
            "sqft": round(sqft, 2)
        })
    return jsonify(out)


# ================= GET ORDER (EDIT) =================

@app.route("/api/orders/<oid>")
@token_required
def get_order(oid):
    o = db.orders.find_one({"_id": oid})
    if not o:
        return jsonify({"error": "Not found"}), 404

    cust = db.customers.find_one({"_id": o.get("customer_id")})
    if not cust:
        try:
            cust = db.customers.find_one({"_id": ObjectId(o.get("customer_id"))})
        except:
            cust = {}

    for e in o.get("entries", []):
        if "Images" not in e:
            e["Images"] = []

    status = o.get("status")
    if status == "Pending":
        status = "Fabric Order Pending"
    elif status == "Cutting":
        status = "Fabric In Transit"

    return jsonify({
        "order_id": o["_id"],
        "customer_name": cust.get("name", ""),
        "phone": cust.get("phone", ""),
        "address": cust.get("address", ""),
        "showroom": cust.get("showroom", ""),
        "status": status,
        "due_date": o.get("due_date"),
        "tailor": o.get("tailor") or "None",
        "fitter": o.get("fitter") or "None",
        "entries": o.get("entries", [])
    })


# ================= UPDATE ORDER =================

@app.route("/api/orders/<oid>", methods=["PUT"])
@token_required
def update_order(oid):
    data = request.json
    
    o = db.orders.find_one({"_id": oid})
    if o and "customer" in data:
        cust = data["customer"]
        cid = o.get("customer_id")
        update_fields = {
            "name": cust.get("name"),
            "phone": cust.get("phone"),
            "address": cust.get("address"),
            "showroom": cust.get("showroom"),
            "updated_at": datetime.utcnow()
        }
        try:
            db.customers.update_one({"_id": cid}, {"$set": update_fields})
        except:
            try:
                db.customers.update_one({"_id": ObjectId(str(cid))}, {"$set": update_fields})
            except:
                pass

    tailor = data.get("tailor") or "None"
    fitter = data.get("fitter") or "None"

    db.orders.update_one(
        {"_id": oid},
        {"$set": {
            "entries": data.get("entries"),
            "status": data.get("status"),
            "due_date": data.get("due_date"),
            "tailor": tailor,
            "fitter": fitter,
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


# ================= BILLING =================

@app.route("/api/billing")
@token_required
def billing_data():
    if request.user["role"] != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    result = []
    for o in db.orders.find({}):
        cust_id = o.get("customer_id")
        cust = db.customers.find_one({"_id": cust_id})
        if not cust and cust_id:
            try:
                cust = db.customers.find_one({"_id": ObjectId(str(cust_id))})
            except:
                pass
        
        if not cust:
            continue

        tailor = o.get("tailor") or "None"
        fitter = o.get("fitter") or "None"
        stitching_total = fitting_total = 0
        stitching_breakup = []
        fitting_breakup = []

        for e in o.get("entries", []):
            stitch_type = (e.get("Stitch") or "").strip()
            panels = int(float(e.get("Panels") or 0))
            sqft = float(e.get("SQFT") or 0)
            window_name = (e.get("Window") or "").strip()

            if window_name and fitter != "None" and fitter != "":
                rate = 200 if "Double" in window_name else 150
                amount = rate
                fitting_total += amount
                fitting_breakup.append({
                    "type": window_name,
                    "qty": 1,
                    "rate": rate,
                    "amount": amount
                })

            rate = amount = 0
            if tailor != "None" and tailor != "":
                if stitch_type in ["Pleated", "Eyelet", "Ripple"]:
                    if panels > 0:
                        if tailor == "Dev":
                            rate = {"Pleated": 90, "Eyelet": 130, "Ripple": 120}.get(stitch_type, 0)
                        elif tailor == "Dinesh":
                            rate = 90
                        amount = panels * rate
                elif "Roman" in stitch_type:
                    if sqft > 0:
                        rate = 125 if tailor == "Dev" else (100 if tailor == "Dinesh" else 0)
                        amount = sqft * rate

            if amount > 0:
                qty_value = round(sqft, 2) if "Roman" in stitch_type else panels
                stitching_total += amount
                stitching_breakup.append({
                    "type": stitch_type,
                    "subtype": window_name,
                    "qty": qty_value,
                    "rate": rate,
                    "amount": amount
                })

        result.append({
            "order_id": str(o.get("_id")),
            "customer_name": cust.get("name", "Unknown Client"),
            "tailor": tailor,
            "fitter": fitter,
            "stitching_total": round(stitching_total, 2),
            "fitting_total": round(fitting_total, 2),
            "grand_total": round(stitching_total + fitting_total, 2),
            "payment_status": o.get("payment_status", "Pending"),
            "stitching_breakup": stitching_breakup,
            "fitting_breakup": fitting_breakup
        })
    return jsonify(result)

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

@app.route("/api/images/gridfs/<fid>")
def get_gridfs_image(fid):
    try:
        file = fs.get(ObjectId(fid))
        return file.read(), 200, {'Content-Type': 'image/jpeg'}
    except:
        return "Not found", 404

if __name__ == "__main__":
    app.run(debug=True, port=5000)
