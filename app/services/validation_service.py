from bson import ObjectId
from app.extensions import mongo
from app.utils.helpers import now_utc
from app.services.audit_service import log_action


def create_validation(entity_type, entity_id, target_role, created_by_user_id, approver_role, remarks=None, metadata=None):
    entity_id = str(entity_id)
    existing = mongo.db.validations.find_one({
        "entity_type": entity_type,
        "entity_id": entity_id,
        "status": "pending",
    })
    if existing:
        return existing["_id"]
    return mongo.db.validations.insert_one({
        "entity_type": entity_type,
        "entity_id": entity_id,
        "target_role": target_role,
        "created_by_user_id": created_by_user_id,
        "approver_role": approver_role,
        "status": "pending",
        "remarks": remarks or "",
        "action_remarks": "",
        "metadata": metadata or {},
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }).inserted_id


def list_validations_for_role(role, session_ctx):
    db = mongo.db
    if role in {"super_admin", "avpl_admin"}:
        query = {} if role == "super_admin" else {"approver_role": {"$in": ["avpl_admin", "super_admin"]}}
        return list(db.validations.find(query).sort("created_at", -1))
    if role == "ufc_mitra":
        return list(db.validations.find({
            "approver_role": "ufc_mitra",
            "metadata.mapped_mitra_uid": session_ctx.get("mitra_uid"),
        }).sort("created_at", -1))
    return []


def can_act_on_validation(validation, role, session_ctx):
    if not validation:
        return False
    if role == "super_admin":
        return True
    if role == "avpl_admin":
        return validation.get("approver_role") in ["avpl_admin", "super_admin"]
    if role == "ufc_mitra":
        return validation.get("approver_role") == "ufc_mitra" and validation.get("metadata", {}).get("mapped_mitra_uid") == session_ctx.get("mitra_uid")
    return False


def action_validation(validation_id, actor_user_id, action, remarks):
    db = mongo.db
    validation = db.validations.find_one({"_id": ObjectId(validation_id)})
    if not validation:
        return False, "Validation not found."
    if validation.get("status") != "pending":
        return False, "This validation has already been processed."
    if action not in {"approve", "reject"}:
        return False, "Invalid validation action."

    status = "approved" if action == "approve" else "rejected"
    db.validations.update_one(
        {"_id": validation["_id"]},
        {"$set": {
            "status": status,
            "action_remarks": remarks,
            "rejection_reason": remarks if status == "rejected" else "",
            "action_by": actor_user_id,
            "updated_at": now_utc(),
        }}
    )

    entity_type = validation["entity_type"]
    entity_id = validation["entity_id"]

    if entity_type == "ufc_admin_profile":
        update_payload = {"approval_status": status, "updated_at": now_utc(), "latest_rejection_reason": remarks if status == "rejected" else ""}
        db.users.update_one({"_id": ObjectId(entity_id)}, {"$set": update_payload})
        db.ufc_admin_master.update_one({"linked_user_id": entity_id}, {"$set": update_payload})
    elif entity_type == "ufc_mitra_profile":
        update_payload = {"approval_status": status, "updated_at": now_utc(), "latest_rejection_reason": remarks if status == "rejected" else ""}
        db.users.update_one({"_id": ObjectId(entity_id)}, {"$set": update_payload})
        db.ufc_mitra_master.update_one({"linked_user_id": entity_id}, {"$set": update_payload})
    elif entity_type == "farmer_registration":
        update_payload = {"approval_status": status, "updated_at": now_utc(), "latest_rejection_reason": remarks if status == "rejected" else ""}
        db.users.update_one({"_id": ObjectId(entity_id)}, {"$set": update_payload})
        db.farmer_master.update_one({"linked_user_id": entity_id}, {"$set": update_payload})

    log_action(actor_user_id, f"validation_{status}", entity_type, entity_id, remarks)
    return True, f"Validation {status}."
