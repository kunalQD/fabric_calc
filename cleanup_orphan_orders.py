from pymongo import MongoClient
from bson import ObjectId
from dotenv import load_dotenv
import os
import sys
from datetime import datetime

# ================= LOAD ENV =================

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    print("❌ MONGO_URI not found in environment.")
    sys.exit(1)

client = MongoClient(MONGO_URI)
db = client["fabric_app"]

print("\n🔍 Scanning for orphan orders...\n")

# ================= FIND ORPHANS =================

pipeline = [
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
            "as": "customer_match"
        }
    },
    {
        "$match": {
            "customer_match": {"$size": 0}
        }
    },
    {
        "$project": {
            "_id": 1,
            "customer_id": 1,
            "status": 1,
            "created_at": 1
        }
    }
]

orphans = list(db.orders.aggregate(pipeline))

if not orphans:
    print("✅ No orphan orders found.")
    sys.exit(0)

print(f"⚠️  Found {len(orphans)} orphan orders.\n")

for o in orphans[:10]:
    print(f"Order ID: {o['_id']} | Customer ID: {o.get('customer_id')}")

if len(orphans) > 10:
    print(f"... and {len(orphans) - 10} more")

# ================= CONFIRMATION =================

confirm = input("\nType DELETE to permanently remove these orders: ")

if confirm != "DELETE":
    print("❌ Aborted. No changes made.")
    sys.exit(0)

# ================= DELETE =================

ids_to_delete = [o["_id"] for o in orphans]

result = db.orders.delete_many({
    "_id": {"$in": ids_to_delete}
})

print("\n🗑  Cleanup Complete")
print(f"Deleted Orders: {result.deleted_count}")
print(f"Timestamp: {datetime.utcnow()} UTC")

print("\n✅ Database integrity restored.\n")