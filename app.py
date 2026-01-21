import os
import io
import uuid
import json
import math
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template, session, redirect, url_for
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
from gridfs import GridFS
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

app = Flask(__name__)
app.secret_key = "quilt_drapes_secure_key"  # Required for sessions

# Configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://kunal-qd:Password_5202@cluster0.zem6dyp.mongodb.net/?appName=Cluster0")
client = MongoClient(MONGO_URI)
db = client["fabric_app"]
fs = GridFS(db)

# --- Authentication Wrapper ---
def is_logged_in():
    return session.get('logged_in')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username') == 'adminqd' and request.form.get('password') == 'adminQD':
            session['logged_in'] = True
            return redirect(url_for('index'))
        return render_template('login.html', error="Invalid Credentials")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
def index():
    if not is_logged_in():
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/api/image/<fid>')
def get_image(fid):
    if not is_logged_in(): return "Unauthorized", 401
    try:
        if fid.startswith("gridfs:"):
            fid = fid.replace("gridfs:", "")
        file_data = fs.get(ObjectId(fid))
        return send_file(io.BytesIO(file_data.read()), mimetype=file_data.content_type or 'image/jpeg')
    except Exception as e:
        return str(e), 404

@app.route('/api/customers/search', methods=['GET'])
def search_customers():
    if not is_logged_in(): return "Unauthorized", 401
    term = request.args.get('term', '').strip()
    query = {"$or": [{"name": {"$regex": term, "$options": "i"}}, {"phone": {"$regex": term, "$options": "i"}}]} if term else {}
    cursor = db.customers.find(query).sort("created_at", DESCENDING).limit(10)
    
    results = []
    for d in cursor:
        cid = str(d["_id"])
        latest_order = db.orders.find_one({"customer_id": cid}, sort=[("created_at", -1)])
        results.append({
            "id": cid,
            "name": d.get("name", ""),
            "phone": d.get("phone", ""),
            "address": d.get("address", ""),
            "showroom": d.get("showroom", ""),
            "previous_entries": latest_order.get("entries", []) if latest_order else []
        })
    return jsonify(results)

@app.route('/api/orders', methods=['POST'])
def save_order():
    if not is_logged_in(): return "Unauthorized", 401
    customer_id = request.form.get('customer_id')
    name = request.form.get('name')
    phone = request.form.get('phone')
    address = request.form.get('address')
    showroom = request.form.get('showroom')
    entries = json.loads(request.form.get('entries'))
    
    cust_data = {"name": name, "phone": phone, "address": address, "showroom": showroom}
    if customer_id and customer_id not in ["null", "undefined"]:
        db.customers.update_one({"_id": ObjectId(customer_id)}, {"$set": cust_data})
    else:
        res = db.customers.insert_one({**cust_data, "created_at": datetime.utcnow()})
        customer_id = str(res.inserted_id)

    order_id = str(uuid.uuid4())
    processed_entries = []

    for i, entry in enumerate(entries):
        image_refs = []
        files = request.files.getlist(f'images_{i}')
        for f in files:
            fid = fs.put(f, filename=f.filename)
            image_refs.append(f"gridfs:{str(fid)}")
        
        entry['Images'] = entry.get('Images', []) + image_refs
        processed_entries.append(entry)

    db.orders.insert_one({
        "_id": order_id,
        "customer_id": customer_id,
        "created_at": datetime.utcnow(),
        "entries": processed_entries
    })
    return jsonify({"status": "success", "order_id": order_id})

@app.route('/api/download-pdf', methods=['POST'])
def download_pdf():
    if not is_logged_in(): return "Unauthorized", 401
    data = request.json
    cust = data['customer']
    entries = data['entries']
    
    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    story = [Paragraph(f"<b>Quilt and Drapes - Order Form</b>", styles["Title"]), Spacer(1, 12)]
    story.append(Paragraph(f"<b>Branch:</b> {cust.get('showroom', 'N/A')}<br/><b>Name:</b> {cust['name']}<br/><b>Phone:</b> {cust['phone']}", styles["Normal"]))
    story.append(Spacer(1, 12))

    table_data = [["Window", "Stitch Type", "Dim.", "Qty", "Track"]]
    for e in entries:
        dims = f"{e.get('Width (inches)',0)}\"x{e.get('Height (inches)',0)}\""
        table_data.append([
            e.get('Window',''), 
            e.get('Stitch Type',''), 
            dims, 
            round(float(e.get('Quantity',0)), 2),
            f"{e.get('Track (ft)', 0)} ft"
        ])
    
    t = Table(table_data, colWidths=[150, 100, 80, 50, 50])
    t.setStyle(TableStyle([('GRID', (0,0), (-1,-1), 0.5, colors.grey), ('FONTSIZE', (0,0), (-1,-1), 9)]))
    story.append(t)
    pdf.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"Order_{cust['name']}.pdf")

if __name__ == '__main__':
    app.run(debug=True, port=5000)