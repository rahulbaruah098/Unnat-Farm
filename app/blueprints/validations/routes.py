from datetime import datetime

from app.utils.helpers import now_utc
from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, abort, jsonify
from app.extensions import mongo
from app.utils.decorators import login_required, roles_required
from app.services.validation_service import list_validations_for_role, action_validation, can_act_on_validation

validation_bp = Blueprint('validation', __name__, url_prefix='/validations')

def json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, list):
        return [json_safe(item) for item in value]

    if isinstance(value, dict):
        return {
            key: json_safe(item)
            for key, item in value.items()
        }

    return value

def wants_json_response():
    return (
        request.headers.get("Accept") == "application/json"
        or request.is_json
        or request.args.get("format") == "json"
    )


def json_error(message, status=400):
    return jsonify({
        "ok": False,
        "message": message
    }), status


@validation_bp.route('/')
@login_required
@roles_required('super_admin', 'avpl_admin', 'ufc_mitra')
def queue():
    q = request.args.get("q", "").strip()

    items = list_validations_for_role(session.get('role'), session)

    if session.get("role") in ["super_admin", "avpl_admin"]:
        existing_ids = {str(item.get("_id")) for item in items}

        profile_update_items = list(
            mongo.db.validations.find({
                "entity_type": "profile_update_request",
                "status": "pending"
            }).sort("created_at", -1)
        )

        for update_item in profile_update_items:
            if str(update_item.get("_id")) not in existing_ids:
                items.append(update_item)

    for item in items:
        linked_user = None
        linked_user_id = item.get("entity_id")

        if item.get("entity_type") == "profile_update_request":
            update_request = None

            try:
                update_request = mongo.db.profile_update_requests.find_one({
                    "_id": ObjectId(str(item.get("entity_id")))
                })
            except Exception:
                update_request = None

            if update_request:
                linked_user_id = update_request.get("user_id")

        if linked_user_id:
            linked_user_id_str = str(linked_user_id)

            try:
                linked_user = mongo.db.users.find_one({
                    "_id": ObjectId(linked_user_id_str)
                })
            except Exception:
                linked_user = None

            if not linked_user:
                linked_user = mongo.db.users.find_one({
                    "_id": linked_user_id_str
                })

            if not linked_user:
                linked_user = mongo.db.users.find_one({
                    "linked_user_id": linked_user_id_str
                })

        item["display_username"] = (
            linked_user.get("username")
            or linked_user.get("name")
            or linked_user.get("phone")
            or str(linked_user_id or "-")
        ) if linked_user else str(linked_user_id or "-")

    if q:
        q_lower = q.lower()
        items = [
            item for item in items
            if q_lower in str(item.get("entity_type", "")).lower()
            or q_lower in str(item.get("status", "")).lower()
            or q_lower in str(item.get("created_at", "")).lower()
            or q_lower in str(item.get("display_username", "")).lower()
        ]

    if request.headers.get("Accept") == "application/json" or request.args.get("format") == "json":
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "q": q
        }))
    
    return render_template('validations/queue.html', items=items)

#changes by atlanta
@validation_bp.route('/<validation_id>')
@login_required
@roles_required('super_admin', 'avpl_admin', 'ufc_mitra')
def detail(validation_id):
    try:
        item = mongo.db.validations.find_one({'_id': ObjectId(validation_id)})
    except Exception:
        item = None

    if not item:
        if wants_json_response():
            return json_error("Validation request not found.", 404)

        abort(404)

    entity = None
    master = None
    linked_docs = []
    profile_update_request = None

    # ─────────────────────────────────────────────
    # Profile update request validation
    # ─────────────────────────────────────────────
    if item.get("entity_type") == "profile_update_request":
        if session.get("role") not in ["super_admin", "avpl_admin"]:
            if wants_json_response():
                return json_error("You are not allowed to view this validation.", 403)

            abort(403)

        try:
            profile_update_request = mongo.db.profile_update_requests.find_one({
                "_id": ObjectId(str(item.get("entity_id")))
            })
        except Exception:
            profile_update_request = None

        if not profile_update_request:
            if wants_json_response():
                return json_error("Profile update request not found.", 404)

            abort(404)

        user_id = profile_update_request.get("user_id")

        try:
            entity = mongo.db.users.find_one({'_id': ObjectId(str(user_id))})
        except Exception:
            entity = mongo.db.users.find_one({'_id': user_id})

        role = profile_update_request.get("role")

        if role == "ufc_admin":
            master = (
                mongo.db.ufc_admin_master.find_one({'linked_user_id': user_id})
                or mongo.db.ufc_admin_master.find_one({'linked_user_id': ObjectId(str(user_id))})
            )
        elif role == "ufc_mitra":
            master = (
                mongo.db.ufc_mitra_master.find_one({'linked_user_id': user_id})
                or mongo.db.ufc_mitra_master.find_one({'linked_user_id': ObjectId(str(user_id))})
            )
        elif role == "farmer":
            master = (
                mongo.db.farmer_master.find_one({'linked_user_id': user_id})
                or mongo.db.farmer_master.find_one({'linked_user_id': ObjectId(str(user_id))})
            )

        for doc in profile_update_request.get("uploaded_docs", []):
            linked_docs.append({
                "document_type": doc.get("label") or doc.get("document_type"),
                "filename": doc.get("filename"),
                "created_at": doc.get("uploaded_at")
            })

        if wants_json_response():
            return jsonify(json_safe({
                "ok": True,
                "item": item,
                "entity": entity or {},
                "master": master or {},
                "linked_docs": linked_docs,
                "profile_update_request": profile_update_request or {}
            }))

        return render_template(
            'validations/detail.html',
            item=item,
            entity=entity,
            master=master,
            linked_docs=linked_docs,
            profile_update_request=profile_update_request
        )

    # ─────────────────────────────────────────────
    # Normal farmer / centre / mitra validation
    # ─────────────────────────────────────────────
    if not can_act_on_validation(item, session.get("role"), session):
        if wants_json_response():
            return json_error("You are not allowed to view this validation.", 403)

        abort(403)

    entity_id = item.get('entity_id')

    try:
        entity = mongo.db.users.find_one({'_id': ObjectId(str(entity_id))})
    except Exception:
        entity = mongo.db.users.find_one({'_id': entity_id})

    if item.get('entity_type') == 'ufc_admin_profile':
        master = (
            mongo.db.ufc_admin_master.find_one({'linked_user_id': entity_id})
            or mongo.db.ufc_admin_master.find_one({'linked_user_id': str(entity_id)})
        )

    elif item.get('entity_type') == 'ufc_mitra_profile':
        master = (
            mongo.db.ufc_mitra_master.find_one({'linked_user_id': entity_id})
            or mongo.db.ufc_mitra_master.find_one({'linked_user_id': str(entity_id)})
        )

    elif item.get('entity_type') == 'farmer_registration':
        master = (
            mongo.db.farmer_master.find_one({'linked_user_id': entity_id})
            or mongo.db.farmer_master.find_one({'linked_user_id': str(entity_id)})
        )

    master_id = str(master.get('_id')) if master else item.get('metadata', {}).get('master_id')

    doc_or_query = [
        {'linked_user_id': entity_id},
        {'linked_user_id': str(entity_id)}
    ]

    if master and master.get('_id'):
        doc_or_query.append({'linked_master_id': master['_id']})
        doc_or_query.append({'linked_master_id': str(master['_id'])})

    if master_id:
        doc_or_query.append({'linked_master_id': master_id})

    all_docs = list(
        mongo.db.documents.find({
            "$and": [
                {"$or": doc_or_query},
                {"status": "active"}
            ]
        }).sort('created_at', -1)
    )

    seen_document_types = set()

    for doc in all_docs:
        doc_type = (doc.get("document_type") or "").strip().lower()

        if not doc_type:
            continue

        if doc_type in seen_document_types:
            continue

        seen_document_types.add(doc_type)
        linked_docs.append(doc)

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "item": item,
            "entity": entity or {},
            "master": master or {},
            "linked_docs": linked_docs,
            "profile_update_request": {}
        }))

    return render_template(
        'validations/detail.html',
        item=item,
        entity=entity,
        master=master,
        linked_docs=linked_docs
    )

@validation_bp.route('/<validation_id>/action', methods=['POST'])
@login_required
@roles_required('super_admin', 'avpl_admin', 'ufc_mitra')
def take_action(validation_id):
    action = request.form.get('action')
    remarks = request.form.get('remarks', '')
    item = mongo.db.validations.find_one({"_id": ObjectId(validation_id)})

    if not item:
        if wants_json_response():
            return json_error("Validation request not found.", 404)

        abort(404)

    if item.get("entity_type") == "profile_update_request":
        if session.get("role") not in ["super_admin", "avpl_admin"]:
            abort(403)

        update_request = mongo.db.profile_update_requests.find_one({
            "_id": ObjectId(item["entity_id"])
        })

        if not update_request or update_request.get("status") != "pending":
            if wants_json_response():
                return json_error(
                    "Profile update request not found or already reviewed.",
                    400
                )

            flash("Profile update request not found or already reviewed.", "danger")
            return redirect(url_for('validation.queue'))

        user_id = update_request.get("user_id")
        role = update_request.get("role")
        uploaded_docs = update_request.get("uploaded_docs", [])

        if action == "approve":
            update_fields = {
                "updated_at": now_utc()
            }

            for doc in uploaded_docs:
                field = doc.get("field")
                filename = doc.get("filename")

                if not filename:
                    continue

                if field == "profile_photo":
                    update_fields["profile_photo"] = filename
                elif field == "government_id_file":
                    update_fields["government_id_file"] = filename
                elif field == "supporting_document":
                    update_fields["supporting_document"] = filename

                document_type = doc.get("label") or doc.get("document_type")

                same_type_names = [document_type]

                if field == "profile_photo":
                    same_type_names = [
                    "Passport Size Photo",
                    "Profile Photo",
                    "Farmer Profile Photo"
                ]
                elif field == "government_id_file":
                    same_type_names = [
                    "Government ID / Identity Document",
                    "Government-Issued Identity Card",
                    "Government-issued Identity Card",
                    "Government ID",
                    "Identity Document"
                ]
                elif field == "supporting_document":
                    same_type_names = [
                        "Supporting Document",
                        "Support Document"
                ]

                mongo.db.documents.update_many(
    {
        "linked_user_id": str(user_id),
        "document_type": {"$in": same_type_names},
        "status": "active"
    },
    {
        "$set": {
            "status": "replaced",
            "replaced_at": now_utc(),
            "updated_at": now_utc()
        }
    }
)

                mongo.db.documents.insert_one({
                    "linked_user_id": str(user_id),
                    "linked_master_id": str(update_request.get("master_id")) if update_request.get("master_id") else None,
                    "filename": filename,
                    "document_type": document_type,
                    "uploaded_by": user_id,
                    "uploaded_role": role,
                    "status": "active",
                    "created_at": now_utc(),
                    "updated_at": now_utc(),
                    "approved_from_update_request": str(update_request["_id"])
        })

            master_collection = None

            if role == "farmer":
                master_collection = mongo.db.farmer_master
            elif role == "ufc_mitra":
                master_collection = mongo.db.ufc_mitra_master
            elif role == "ufc_admin":
                master_collection = mongo.db.ufc_admin_master

            if master_collection is not None:
                master_collection.update_one(
                    {
                        "$or": [
                            {"linked_user_id": user_id},
                            {"linked_user_id": ObjectId(user_id)}
                        ]
                    },
                    {"$set": update_fields}
                )

            mongo.db.users.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": update_fields}
            )

            mongo.db.profile_update_requests.update_one(
    {"_id": ObjectId(item["entity_id"])},
    {
        "$set": {
            "status": "approved",
            "reviewed_by": session.get("user_id"),
            "reviewed_at": now_utc(),
            "updated_at": now_utc(),
            "rejection_reason": ""
        }
    }
)

            mongo.db.profile_update_requests.update_many(
            {
            "user_id": str(user_id),
            "status": "pending",
            "_id": {"$ne": ObjectId(item["entity_id"])}
        },
        {
            "$set": {
            "status": "cancelled",
            "reviewed_by": session.get("user_id"),
            "reviewed_at": now_utc(),
            "updated_at": now_utc(),
            "rejection_reason": "Cancelled because a newer/related profile update request was already approved."
        }
    }
)

            mongo.db.validations.update_one(
                {"_id": ObjectId(validation_id)},
                {
                    "$set": {
                        "status": "approved",
                        "updated_at": now_utc(),
                        "action_by": session.get("user_id"),
                        "action_remarks": remarks
                    }
                }
            )

            if wants_json_response():
                return jsonify({
                    "ok": True,
                    "message": "Profile update request approved successfully.",
                    "status": "approved"
                })

            flash("Profile update request approved successfully.", "success")
            return redirect(url_for('validation.queue'))

        if action == "reject":
            mongo.db.profile_update_requests.update_one(
                {"_id": ObjectId(item["entity_id"])},
                {
                "$set": {
                "status": "rejected",
                "reviewed_by": session.get("user_id"),
                "reviewed_at": now_utc(),
                "updated_at": now_utc(),
                "rejection_reason": remarks
            }
        }
    )

            mongo.db.validations.update_one(
                {"_id": ObjectId(validation_id)},
                {
                    "$set": {
                        "status": "rejected",
                        "updated_at": now_utc(),
                        "action_by": session.get("user_id"),
                        "action_remarks": remarks,
                        "rejection_reason": remarks
                    }
                }
            )

            if wants_json_response():
                return jsonify({
                    "ok": True,
                    "message": "Profile update request rejected.",
                    "status": "rejected"
                })

            flash("Profile update request rejected.", "success")
            return redirect(url_for('validation.queue'))

        if wants_json_response():
            return json_error("Invalid action selected.", 400)

        flash("Invalid action selected.", "danger")
        return redirect(url_for('validation.queue'))

    if not can_act_on_validation(item, session.get("role"), session):
        abort(403)

    ok, message = action_validation(
        validation_id,
        session['user_id'],
        action,
        remarks
    )

    if wants_json_response():
        return jsonify({
            "ok": ok,
            "message": message,
            "status": action if ok else "failed"
        }), 200 if ok else 400

    flash(message, 'success' if ok else 'danger')
    return redirect(url_for('validation.queue'))
