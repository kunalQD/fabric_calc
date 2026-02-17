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
from reportlab.platypus import PageBreak, KeepTogether


PDF_CACHE = {}
app = Flask(__name__)
app.secret_key = "quilt_drapes_secure_key"

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


def is_logged_in():
    return session.get("logged_in")

# ---------------- AUTH ----------------

# ---------------- DASHBOARD KPIs ----------------

@app.route("/api/dashboard/kpis")
def dashboard_kpis():
    if not is_logged_in(): 
        return "Unauthorized", 401

    orders = list(db.orders.find({}))

    total_orders = len(orders)
    stitching = 0
    completed = 0
    fabric_pending = 0
    installation = 0

    for o in orders:

        status = o.get("status", "")

        # 🔄 Map old values
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
        "orders": total_orders,
        "fabric_pending": fabric_pending,
        "stitching": stitching,
        "completed": completed,
        "installation": installation
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
        users = {
            "adminqd": {"password":"adminQD","role":"admin"},
            "staffqd": {"password":"staffQD","role":"staff"}
        }

        u = request.form["username"]
        p = request.form["password"]

        if u in users and users[u]["password"] == p:
            session["logged_in"] = True
            session["role"] = users[u]["role"]
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
    if not is_logged_in():
        return redirect("/login")
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

        imgs = e.get("Images", [])
        notes = (e.get("Notes") or "").strip()

        if not imgs and not notes:
            continue

        # ---- Window title ----
        elements.append(
            Paragraph(
                f"<b>Window {idx}: {e.get('Window','')}</b>",
                styles["Heading3"]
            )
        )
        elements.append(Spacer(1, 4))

        # ---- Notes ----
        if notes:
            elements.append(
                Paragraph(
                    f"<i>Tailor Notes:</i> {notes}",
                    styles["Normal"]
                )
            )
            elements.append(Spacer(1, 6))

        # ---- Images ----
        row = []

        for ref in imgs:
            try:
                fid = ref.replace("gridfs:", "")
                f = fs.get(ObjectId(fid))

                raw = Image.open(io.BytesIO(f.read()))
                raw.thumbnail((500, 500))

                compressed = io.BytesIO()
                raw.save(compressed, format="JPEG", quality=60, optimize=True)
                compressed.seek(0)

                row.append(
                    RLImage(compressed, width=1.8 * inch, height=1.8 * inch)
                )

                if len(row) == 4:
                    elements.append(
                        KeepTogether([
                            Table([row], colWidths=[110] * 4),
                            Spacer(1, 6)
                        ])
                    )
                    row = []

            except:
                continue

        if row:
            elements.append(
                KeepTogether([
                    Table([row], colWidths=[110] * len(row)),
                    Spacer(1, 8)
                ])
            )

        elements.append(Spacer(1, 10))


        # ---- Keep everything together + page break ----


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
        "tailor": request.form.get("tailor",""),
        "fitter": request.form.get("fitter",""),
        "showroom": request.form["showroom"]
    }

    customer = db.customers.find_one({"phone": cust["phone"]})
    if customer:
        cid = str(customer["_id"])
        db.customers.update_one({"_id": customer["_id"]}, {"$set": cust})
    else:
        cid = str(db.customers.insert_one({**cust, "created_at": datetime.utcnow()}).inserted_id)

    for e in entries:
        wid = e.get("window_id")
        imgs = request.files.getlist(f"images_{wid}")

        if imgs:
            refs = []
            for img in imgs:
                fid = fs.put(
                    img,
                    filename=img.filename,
                    content_type=img.content_type
                )
                refs.append(f"gridfs:{fid}")
            e["Images"] = refs



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

        "name": cust.get("name", ""),
        "phone": cust.get("phone", ""),
        "address": cust.get("address", ""),
        "showroom": cust.get("showroom", ""),

        "status": o.get("status"),
        "due_date": o.get("due_date"),

        # ✅ ADD THESE
        "tailor": o.get("tailor", ""),
        "fitter": o.get("fitter", ""),

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
        "tailor": request.form.get("tailor", order.get("tailor","")),
        "fitter": request.form.get("fitter", order.get("fitter","")),
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
        "tailor": request.form.get("tailor", order.get("tailor","")),
        "fitter": request.form.get("fitter", order.get("fitter","")),
        "updated_at": datetime.utcnow()
    }}
)
    # 🔥 CLEAR PDF CACHE FOR THIS ORDER
    cache_key = f"order_pdf:{oid}"
    if cache_key in PDF_CACHE:
        del PDF_CACHE[cache_key]


    return jsonify({"status": "updated"})

# ---------------- DELETE ORDER ----------------

@app.route("/api/orders/<oid>", methods=["DELETE"])
def delete_order(oid):
    if not is_logged_in(): return "Unauthorized", 401

    if session.get("role") != "admin":
        return jsonify({"error":"Not allowed"}), 403


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

    # ✅ If status filter is still sent from frontend, support it
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

        # ✅ SHOWROOM FILTER
        if showroom:
            allowed = showroom.split(",")
            if cust.get("showroom") not in allowed:
                continue

        # 🔄 AUTO-MAP OLD STATUS VALUES
        raw_status = o.get("status", "")

        if raw_status == "Pending":
            mapped_status = "Fabric Order Pending"
        elif raw_status == "Cutting":
            mapped_status = "Fabric In Transit"
        elif raw_status == "Stitching":
            mapped_status = "Stitching"
        elif raw_status == "Completed":
            mapped_status = "Completed"
        else:
            mapped_status = raw_status  # already new format

        # 🔹 Calculate totals
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
            "tailor": o.get("tailor",""),
            "fitter": o.get("fitter",""),
            "item_count": len(o.get("entries") or []),
            "panels": panels,
            "sqft": round(sqft, 2)
        })

    return jsonify(out)

@app.route("/api/orders/<oid>/status", methods=["PUT"])
def update_order_status(oid):
    if not is_logged_in():
        return "Unauthorized", 401

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

@app.route("/api/analytics/stages")
def stage_analytics():
    if not is_logged_in():
        return "Unauthorized", 401

    pipeline = [
        {"$group": {"_id": "$status", "count": {"$sum": 1}}}
    ]

    result = list(db.orders.aggregate(pipeline))

    return jsonify(result)

@app.route("/api/analytics/daily-assignments")
def daily_assignments():
    if not is_logged_in():
        return "Unauthorized", 401

    today = datetime.utcnow().date()

    orders = list(db.orders.find({}))

    summary = {
        "tailors": {},
        "fitters": {}
    }

    for o in orders:
        created = o.get("created_at")
        if not created:
            continue

        if created.date() != today:
            continue

        t = o.get("tailor", "")
        f = o.get("fitter", "")

        if t:
            summary["tailors"][t] = summary["tailors"].get(t, 0) + 1

        if f:
            summary["fitters"][f] = summary["fitters"].get(f, 0) + 1

    return jsonify(summary)

@app.route("/billing")
def billing_page():
    if not is_logged_in() or session.get("role") != "admin":
        return "Unauthorized", 403
    return render_template("billing.html")

@app.route("/api/billing")
def billing_data():
    if not is_logged_in() or session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        orders = list(db.orders.find({}))
        result = []

        for o in orders:

            cust = db.customers.find_one({"_id": o.get("customer_id")})
            if not cust:
                try:
                    cust = db.customers.find_one(
                        {"_id": ObjectId(o.get("customer_id"))}
                    )
                except:
                    continue

            tailor = o.get("tailor", cust.get("tailor", ""))
            fitter = o.get("fitter", cust.get("fitter", ""))

            stitching_total = 0
            fitting_total = 0
            stitching_breakup = []
            fitting_breakup = []

            for e in o.get("entries", []):

                stitch_type = (e.get("Stitch") or "").strip()
                panels = int(float(e.get("Panels") or 0))
                sqft = float(e.get("SQFT") or 0)
                window_name = (e.get("Window") or "").strip()

                if window_name:
                    rate = 200 if "Double" in window_name else 150
                    qty = 1
                    amount = qty * rate
                    fitting_total += amount
                    fitting_breakup.append({
                        "type": window_name,
                        "qty": qty,
                        "rate": rate,
                        "amount": amount
                    })

                amount = 0
                rate = 0

                # PLEATED / EYELET / RIPPLE → per panel
                if stitch_type in ["Pleated", "Eyelet", "Ripple"]:
                    if panels > 0:
                        if tailor == "Dev":
                            rate = {"Pleated":90,"Eyelet":130,"Ripple":120}.get(stitch_type,0)
                        elif tailor == "Dinesh":
                            rate = 90
                        amount = panels * rate

                # ROMAN → per SQFT
                elif "Roman" in stitch_type:
                    if sqft > 0:
                        if tailor == "Dev":
                            rate = 125
                        elif tailor == "Dinesh":
                            rate = 100
                        amount = sqft * rate


                if amount > 0:

                    # Roman blinds use SQFT as qty
                    if "Roman" in stitch_type:
                        qty_value = round(sqft, 2)
                    else:
                        qty_value = panels

                    stitching_total += amount
                    stitching_breakup.append({
                        "type": stitch_type,
                        "qty": qty_value,
                        "rate": rate,
                        "amount": amount
                    })


            override = o.get("billing_override")
            if override:
                stitching_total = override.get("stitching_total", stitching_total)
                fitting_total = override.get("fitting_total", fitting_total)
                stitching_breakup = override.get("stitching_breakup", stitching_breakup)
                fitting_breakup = override.get("fitting_breakup", fitting_breakup)

            result.append({
                "order_id": str(o["_id"]),
                "customer": cust.get("name", ""),
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

    except Exception as e:
        print("Billing error:", e)
        return jsonify({"error": "Server error"}), 500

@app.route("/api/billing/<oid>/print")
def print_billing_pdf(oid):
    if not is_logged_in() or session.get("role") != "admin":
        return "Unauthorized", 403

    order = db.orders.find_one({"_id": oid})
    if not order:
        return "Not found", 404

    cust = db.customers.find_one({"_id": ObjectId(order["customer_id"])})

    # Use billing_override if exists
    override = order.get("billing_override")

    # Recalculate if no override
    billing = next((b for b in billing_data().json if b["order_id"] == oid), None)

    stitching_breakup = billing["stitching_breakup"]
    fitting_breakup = billing["fitting_breakup"]
    stitching_total = billing["stitching_total"]
    fitting_total = billing["fitting_total"]
    grand_total = billing["grand_total"]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elems = []

    elems.append(Paragraph("<b>Quilt & Drapes – Bill</b>", styles["Title"]))
    elems.append(Spacer(1,12))

    elems.append(Paragraph(f"<b>Customer:</b> {cust.get('name','')}", styles["Normal"]))
    elems.append(Paragraph(f"<b>Tailor:</b> {order.get('tailor','')}", styles["Normal"]))
    elems.append(Paragraph(f"<b>Fitter:</b> {order.get('fitter','')}", styles["Normal"]))
    elems.append(Spacer(1,12))

    # Stitching Table
    data = [["Type","Qty","Rate","Amount"]]
    for s in stitching_breakup:
        data.append([s["type"], s["qty"], s["rate"], s["amount"]])

    data.append(["","","Total", stitching_total])

    table = Table(data)
    table.setStyle(TableStyle([
        ("GRID",(0,0),(-1,-1),0.5,colors.grey),
        ("BACKGROUND",(0,0),(-1,0),colors.lightgrey),
        ("FONT",(0,0),(-1,0),"Helvetica-Bold")
    ]))

    elems.append(Paragraph("<b>Stitching</b>", styles["Heading3"]))
    elems.append(Spacer(1,6))
    elems.append(table)
    elems.append(Spacer(1,12))

    # Fitting Table
    data2 = [["Type","Qty","Rate","Amount"]]
    for f in fitting_breakup:
        data2.append([f["type"], f["qty"], f["rate"], f["amount"]])

    data2.append(["","","Total", fitting_total])

    table2 = Table(data2)
    table2.setStyle(TableStyle([
        ("GRID",(0,0),(-1,-1),0.5,colors.grey),
        ("BACKGROUND",(0,0),(-1,0),colors.lightgrey),
        ("FONT",(0,0),(-1,0),"Helvetica-Bold")
    ]))

    elems.append(Paragraph("<b>Fitting</b>", styles["Heading3"]))
    elems.append(Spacer(1,6))
    elems.append(table2)
    elems.append(Spacer(1,18))

    elems.append(Paragraph(f"<b>Grand Total: ₹ {grand_total}</b>", styles["Heading2"]))

    doc.build(elems)

    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"Bill_{cust.get('name','')}.pdf",
        mimetype="application/pdf"
    )


@app.route("/api/billing/<oid>/update", methods=["PUT"])
def update_billing_override(oid):
    if not is_logged_in() or session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    try:
        data = request.json

        db.orders.update_one(
            {"_id": oid},
            {"$set": {
                "billing_override": {
                    "stitching_total": data.get("stitching_total", 0),
                    "fitting_total": data.get("fitting_total", 0),
                    "stitching_breakup": data.get("stitching_breakup", []),
                    "fitting_breakup": data.get("fitting_breakup", []),
                    "updated_at": datetime.utcnow()
                }
            }}
        )

        return jsonify({"status": "billing updated"})

    except Exception as e:
        print("Billing update error:", e)
        return jsonify({"error": "Server error"}), 500




@app.route("/calendar")
def calendar_view():
    if not is_logged_in():
        return redirect("/login")
    return render_template("calendar.html")

@app.route("/api/billing/<oid>/update-assignment", methods=["PUT"])
def update_billing_assignment(oid):
    if not is_logged_in() or session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json

    db.orders.update_one(
        {"_id": oid},
        {"$set": {
            "tailor": data.get("tailor",""),
            "fitter": data.get("fitter",""),
            "updated_at": datetime.utcnow()
        }}
    )

    return jsonify({"status": "updated"})


@app.route("/api/billing/<oid>/mark-paid", methods=["PUT"])
def mark_paid(oid):
    if not is_logged_in() or session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    db.orders.update_one(
        {"_id": oid},
        {"$set": {
            "payment_status": "Paid",
            "paid_date": datetime.utcnow()
        }}
    )

    return jsonify({"status": "updated"})



# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
