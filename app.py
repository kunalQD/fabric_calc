import os, io, uuid, json
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template, session, redirect
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
from gridfs import GridFS
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.platypus import Image as RLImage
from reportlab.lib.units import inch
from PIL import Image


PDF_CACHE = {}
app = Flask(__name__)
app.secret_key = "quilt_drapes_secure_key"

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client["fabric_app"]
fs = GridFS(db)

STATUSES = ["Pending", "Cutting", "Stitching", "Completed"]

def is_logged_in():
    return session.get("logged_in")

# ---------------- AUTH ----------------

# ---------------- DASHBOARD KPIs ----------------

@app.route("/api/dashboard/kpis")
def dashboard_kpis():
    if not is_logged_in(): 
        return "Unauthorized", 401

    orders = list(db.orders.find({}))

    total_sqft = 0
    total_panels = 0
    status_count = {s: 0 for s in STATUSES}

    for o in orders:
        status = o.get("status", "Pending")
        status_count[status] += 1

        for e in o.get("entries", []):
            total_sqft += float(e.get("SQFT", 0))
            total_panels += int(e.get("Panels", 0))

    return jsonify({
        "orders": len(orders),
        "sqft": round(total_sqft, 2),
        "panels": total_panels,
        "status": status_count
    })


@app.route("/api/orders/<oid>/pdf")
def print_order_pdf(oid):
    if not is_logged_in(): return "Unauthorized", 401

    order = db.orders.find_one({"_id": oid})
    if not order: return "Not found", 404

    cust = db.customers.find_one({"_id": ObjectId(order["customer_id"])})
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=30,leftMargin=30, topMargin=30,bottomMargin=30)
    styles = getSampleStyleSheet()
    elems = []

    elems.append(Paragraph("<b>ORDER FORM</b>", styles["Title"]))
    elems.append(Spacer(1,12))

    elems.append(Paragraph(f"<b>Name:</b> {cust['name']}", styles["Normal"]))
    elems.append(Paragraph(f"<b>Phone:</b> {cust['phone']}", styles["Normal"]))
    elems.append(Paragraph(f"<b>Address:</b> {cust['address']}", styles["Normal"]))
    elems.append(Paragraph(f"<b>Showroom:</b> {cust['showroom']}", styles["Normal"]))
    elems.append(Paragraph(f"<b>Date:</b> {order['created_at'].strftime('%d-%m-%Y %H:%M')}", styles["Normal"]))
    elems.append(Spacer(1,14))

    table_data = [["Window","Stitch","Width","Height","Qty","Track(ft)","SQFT","Panels"]]

    for e in order["entries"]:
        table_data.append([
            e.get("Window",""),
            e.get("Stitch",""),
            e.get("Width",""),
            e.get("Height",""),
            f"{e.get('Quantity',0):.2f}",
            e.get("Track",0),
            e.get("SQFT",0),
            e.get("Panels","")
        ])

    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.black),
        ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("GRID",(0,0),(-1,-1),0.5,colors.grey),
        ("FONT",(0,0),(-1,0),"Helvetica-Bold"),
        ("ALIGN",(2,1),(-1,-1),"CENTER"),
        ("BOTTOMPADDING",(0,0),(-1,0),8)
    ]))

    elems.append(table)
    doc.build(elems)

    buffer.seek(0)
    return send_file(buffer, as_attachment=False,
                     download_name=f"Order_{cust['name']}.pdf",
                     mimetype="application/pdf")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form["username"] == "adminqd" and request.form["password"] == "adminQD":
            session["logged_in"] = True
            return redirect("/dashboard")
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")
    

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------------- PAGES ----------------

@app.route("/")
def home():
    return redirect("/dashboard")

@app.route("/dashboard")
def dashboard():
    if not is_logged_in(): return redirect("/login")
    return render_template("dashboard.html")

@app.route("/calculator")
def calculator():
    if not is_logged_in(): return redirect("/login")
    return render_template("index.html")

@app.route("/api/orders/<oid>/print")
def print_order(oid):
    if not is_logged_in():
        return "Unauthorized", 401

    # ✅ ALWAYS define cache_key FIRST
    cache_key = f"order_pdf:{oid}"

    # ✅ Fetch order
    order = db.orders.find_one({"_id": oid})
    if not order:
        return "Order not found", 404

    customer = db.customers.find_one(
        {"_id": ObjectId(order["customer_id"])}
    )

    filename = f"Order_{customer.get('name','').replace(' ','_')}.pdf"

    # ✅ CACHE HIT — RETURN IMMEDIATELY
    if cache_key in PDF_CACHE:
        return send_file(
            io.BytesIO(PDF_CACHE[cache_key]),
            as_attachment=True,
            download_name=filename,
            mimetype="application/pdf"
        )

    # ---------- PDF BUILD STARTS ----------
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    elements = []

    # ---------- HEADER ----------
    elements.append(Paragraph("<b>Quilt & Drapes</b>", styles["Title"]))
    elements.append(Spacer(1, 10))

    meta = [
        f"<b>Name:</b> {customer.get('name','')}",
        f"<b>Phone:</b> {customer.get('phone','')}",
        f"<b>Address:</b> {customer.get('address','')}",
        f"<b>Showroom:</b> {customer.get('showroom','')}",
        f"<b>Status:</b> {order.get('status','')}",
        f"<b>Due Date:</b> {order.get('due_date','')}"
    ]

    for m in meta:
        elements.append(Paragraph(m, styles["Normal"]))

    elements.append(Spacer(1, 14))

    # ---------- TABLE ----------
    table_data = [[
        "Window", "Stitch", "Lining", "Width", "Height",
        "Panels", "Qty (Mtrs)", "Track (ft)"
    ]]

    for e in order.get("entries", []):
        table_data.append([
            e.get("Window",""),
            e.get("Stitch",""),
            e.get("Lining",""),
            e.get("Width",""),
            e.get("Height",""),
            e.get("Panels",""),
            f"{float(e.get('Quantity',0)):.2f}",
            e.get("Track","")
        ])

    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f1f5f9")),
        ("FONT", (0,0), (-1,0), "Helvetica-Bold"),
        ("ALIGN", (3,1), (-1,-1), "CENTER"),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 16))

    # ---------- IMAGES (COMPRESSED) ----------
    from PIL import Image

    MAX_WINDOWS_WITH_IMAGES = 6

    for idx, e in enumerate(order.get("entries", []), start=1):
        if idx > MAX_WINDOWS_WITH_IMAGES:
            break

        imgs = e.get("Images", [])
        if not imgs:
            continue

        elements.append(
            Paragraph(f"<b>Window {idx}: {e.get('Window','')}</b>",
                      styles["Heading3"])
        )
        elements.append(Spacer(1, 8))

        row, rows = [], []

        for ref in imgs:
            try:
                fid = ref.replace("gridfs:", "")
                f = fs.get(ObjectId(fid))

                raw = Image.open(io.BytesIO(f.read()))
                raw.thumbnail((600, 600))

                compressed = io.BytesIO()
                raw.save(compressed, format="JPEG", quality=65, optimize=True)
                compressed.seek(0)

                img = RLImage(compressed, width=2.5*inch, height=2.5*inch)
                row.append(img)

                if len(row) == 3:
                    rows.append(row)
                    row = []
            except:
                continue

        if row:
            rows.append(row)

        for r in rows:
            elements.append(Table([r], colWidths=[130]*len(r)))
            elements.append(Spacer(1, 6))

        elements.append(Spacer(1, 12))

    # ---------- BUILD & CACHE ----------
    doc.build(elements)

    pdf_bytes = buffer.getvalue()
    PDF_CACHE[cache_key] = pdf_bytes

    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf"
    )



# ---------------- IMAGES ----------------

@app.route("/api/image/<fid>")
def image(fid):
    if not is_logged_in():
        return "Unauthorized", 401

    try:
        from bson import ObjectId

        # ✅ Convert string to ObjectId safely
        file_id = ObjectId(fid)

        f = fs.get(file_id)

        return send_file(
            io.BytesIO(f.read()),
            mimetype=f.content_type or "image/jpeg"
        )

    except Exception as e:
        print("Image load failed:", fid, e)
        return "Not found", 404


# ---------------- CREATE ORDER ----------------

@app.route("/api/orders", methods=["POST"])
def save_order():
    if not is_logged_in(): return "Unauthorized", 401

    entries = json.loads(request.form["entries"])
    cust = {
        "name": request.form["name"],
        "phone": request.form["phone"],
        "address": request.form["address"],
        "showroom": request.form["showroom"]
    }

    customer = db.customers.find_one({"phone": cust["phone"]})
    if customer:
        cid = str(customer["_id"])
        db.customers.update_one({"_id": customer["_id"]}, {"$set": cust})
    else:
        cid = str(db.customers.insert_one({**cust, "created_at": datetime.utcnow()}).inserted_id)

    for i, e in enumerate(entries):
        imgs = request.files.getlist(f"images_{i}")
        if imgs:
            refs = []
            for img in imgs:
                fid = fs.put(img, filename=img.filename)
                refs.append(f"gridfs:{fid}")
            e["Images"] = refs
        # else: DO NOT TOUCH existing Images


    order = {
        "_id": str(uuid.uuid4()),
        "customer_id": cid,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "status": request.form.get("status", "Pending"),
        "due_date": request.form.get("due_date"),
        "entries": entries
    }

    db.orders.insert_one(order)
    return jsonify({"status": "success"})

# ---------------- LOAD ORDER (EDIT) ----------------

@app.route("/api/orders/<order_id>")
def get_order(order_id):
    if not is_logged_in():
        return "Unauthorized", 401

    o = db.orders.find_one({"_id": order_id})
    if not o:
        return "Not found", 404

    # 🔹 Resolve customer safely (string or ObjectId)
    cust = db.customers.find_one({"_id": o.get("customer_id")})
    if not cust:
        try:
            cust = db.customers.find_one({"_id": ObjectId(o.get("customer_id"))})
        except:
            cust = {}

    # 🔹 Normalize Images key for frontend
    for e in o.get("entries", []):
        if "Images" not in e:
            e["Images"] = []

    return jsonify({
        "order_id": o["_id"],

        # ✅ THESE WERE MISSING
        "name": cust.get("name", ""),
        "phone": cust.get("phone", ""),
        "address": cust.get("address", ""),
        "showroom": cust.get("showroom", ""),

        "status": o.get("status"),
        "due_date": o.get("due_date"),
        "entries": o.get("entries", [])
    })


# ---------------- UPDATE ORDER ----------------

@app.route("/api/orders/<oid>", methods=["PUT"])
def update_order(oid):
    if not is_logged_in(): return "Unauthorized", 401

    order = db.orders.find_one({"_id": oid})
    if not order:
        return jsonify({"error": "Order not found"}), 404

    entries = json.loads(request.form["entries"])

    deleted_images = json.loads(request.form.get("deleted_images", "{}"))

    for wid, files in deleted_images.items():
        for fid in files:
            try:
                fs.delete(ObjectId(fid))
            except:
                pass


    cust = {
        "name": request.form["name"],
        "phone": request.form["phone"],
        "address": request.form["address"],
        "showroom": request.form["showroom"]
    }

    db.customers.update_one({"_id": ObjectId(order["customer_id"])}, {"$set": cust})
    old_map = {
        e.get("window_id"): e.get("Images", [])
        for e in order.get("entries", [])
    }

    for e in entries:
        wid = e.get("window_id")

        # start ONLY from what frontend sent
        preserved = e.get("Images", [])

        # append new uploads
        for img in request.files.getlist(f"images_{wid}"):
            fid = fs.put(img, filename=img.filename, content_type=img.content_type)
            preserved.append(f"gridfs:{fid}")

        e["Images"] = preserved



    db.orders.update_one(
        {"_id": oid},
        {"$set": {
            "entries": entries,
            "status": request.form.get("status"),
            "due_date": request.form.get("due_date"),
            "updated_at": datetime.utcnow()
        }}
    )
    return jsonify({"status": "updated"})

# ---------------- DELETE ORDER ----------------

@app.route("/api/orders/<oid>", methods=["DELETE"])
def delete_order(oid):
    if not is_logged_in(): return "Unauthorized", 401

    order = db.orders.find_one({"_id": oid})
    if not order:
        return jsonify({"error": "Order not found"}), 404

    for e in order.get("entries", []):
        for img in e.get("Images", []):
            if img.startswith("gridfs:"):
                try:
                    fs.delete(ObjectId(img.replace("gridfs:", "")))
                except:
                    pass

    db.orders.delete_one({"_id": oid})
    return jsonify({"status": "deleted"})

# ---------------- DASHBOARD LIST ----------------

@app.route("/api/orders/list")
def list_orders():

    print("Listing orders with args:", request.args)
    if not is_logged_in():
        return jsonify({"error": "unauthorized"}), 401

    status = request.args.get("status")
    showroom = request.args.get("showroom")

    q = {}

    # ✅ STATUS FILTER (orders collection)
    if status:
        q["status"] = {"$in": status.split(",")}

    out = []

    for o in db.orders.find(q).sort("created_at", DESCENDING):

        # 🔹 Fetch customer safely
        cust = db.customers.find_one({"_id": o.get("customer_id")})
        if not cust:
            try:
                cust = db.customers.find_one({"_id": ObjectId(o.get("customer_id"))})
            except:
                cust = None

        if not cust:
            continue

        # ✅ SHOWROOM FILTER (customer collection)
        if showroom:
            allowed = showroom.split(",")
            if cust.get("showroom") not in allowed:
                continue

        sqft = 0
        panels = 0

        for e in o.get("entries") or []:
            sqft += float(e.get("SQFT", 0) or 0)
            panels += int(float(e.get("Panels", 0) or 0))

        out.append({
            "order_id": o["_id"],
            "name": cust.get("name"),
            "phone": cust.get("phone"),
            "status": o.get("status", "Pending"),
            "created_at": o.get("created_at"),
            "updated_at": o.get("updated_at"),
            "due_date": o.get("due_date"),
            "showroom": cust.get("showroom", ""),
            "item_count": len(o.get("entries") or []),
            "panels": panels,
            "sqft": round(sqft, 2)
        })

    return jsonify(out)



# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
