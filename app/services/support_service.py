from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from bson import ObjectId

from app.extensions import mongo
from app.services.document_service import store_support_attachment, validate_support_attachment
from app.utils.helpers import now_utc


SUPPORT_EMAIL = "ites@sayanant.com"
SUPPORT_NUMBER = "9957367398"

SUPPORT_PROBLEM_TYPES = [
    "Login / Account Issue",
    "Profile / Validation Issue",
    "Dashboard / Navigation Issue",
    "Product / Marketplace Issue",
    "Order / Delivery Issue",
    "Stock / Inventory Issue",
    "POS / Billing Issue",
    "Payment / Settlement Issue",
    "Invoice / GST / Accounting Issue",
    "LMS / Learning Issue",
    "Report / Data Issue",
    "Document Upload Issue",
    "Mobile App Issue",
    "Other Technical Issue",
]

SUPPORT_PRIORITIES = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "urgent": "Urgent",
}

SUPPORT_STATUSES = {
    "open": "Open",
    "in_progress": "In Progress",
    "waiting_for_user": "Waiting for User",
    "resolved": "Resolved",
    "closed": "Closed",
}

PUBLIC_LOGIN_PROBLEM_TYPES = [
    "Unable to Login",
    "Forgot Password",
    "Forgot Username",
    "Account Inactive",
    "Wrong Login Type Selected",
    "Mobile Number Changed",
    "Email ID Changed",
    "Approval Pending",
    "Account Not Found",
    "Password Reset Required",
    "Other Login Issue",
]

PUBLIC_ACCOUNT_TYPES = [
    "Authority",
    "UnnatFarm Centre",
    "UnnatFarm Mitra",
    "Farmer",
    "Not Sure",
]

IST = ZoneInfo("Asia/Kolkata")


def _clean(value, limit=2000):
    return str(value or "").strip()[:limit]


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _phone_digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())[-10:]


def _display_datetime(value):
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        value = value.astimezone(IST)
    except Exception:
        pass
    return value.strftime("%d %b %Y, %I:%M %p")


def _get_actor(user_id):
    oid = _to_object_id(user_id)
    if not oid:
        raise PermissionError("Please login again to use Support.")
    user = mongo.db.users.find_one({"_id": oid})
    if not user:
        raise PermissionError("Your user account could not be found.")
    user = dict(user)
    user["resolved_role"] = _clean(user.get("role"), 60).lower()
    user["resolved_name"] = (
        _clean(user.get("name"), 160)
        or _clean(user.get("full_name"), 160)
        or _clean(user.get("username"), 160)
        or "User"
    )
    return user


def _new_ticket_ref(prefix="SUP"):
    stamp = now_utc().strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{uuid4().hex[:6].upper()}"


def _message(author_user_id, author_name, author_role, author_type, body, *, visibility="public", attachments=None):
    return {
        "id": uuid4().hex,
        "author_user_id": str(author_user_id or ""),
        "author_name": _clean(author_name, 160) or "User",
        "author_role": _clean(author_role, 60),
        "author_type": author_type,
        "body": _clean(body, 6000),
        "visibility": visibility,
        "attachments": list(attachments or []),
        "created_at": now_utc(),
    }


def _history(action, actor_name, actor_role, note=""):
    return {
        "id": uuid4().hex,
        "action": _clean(action, 80),
        "actor_name": _clean(actor_name, 160),
        "actor_role": _clean(actor_role, 60),
        "note": _clean(note, 1200),
        "created_at": now_utc(),
    }


def _notify_user(user_id, role, title, message):
    if not user_id:
        return
    mongo.db.notifications.insert_one({
        "to_user_id": str(user_id),
        "role": _clean(role, 60),
        "title": _clean(title, 160),
        "message": _clean(message, 700),
        "status": "unread",
        "created_at": now_utc(),
    })


def _notify_super_admins(title, message):
    for user in mongo.db.users.find({"role": "super_admin", "active": {"$ne": False}}, {"_id": 1}):
        _notify_user(user.get("_id"), "super_admin", title, message)


def _attachment_row(doc):
    return {
        "document_id": str(doc.get("_id") or ""),
        "filename": doc.get("filename") or "",
        "original_name": doc.get("original_name") or doc.get("filename") or "Attachment",
        "size_bytes": int(doc.get("size_bytes") or 0),
        "content_type": doc.get("content_type") or "",
    }


def _validate_attachments(files):
    for file_storage in list(files or [])[:5]:
        if not file_storage or not file_storage.filename:
            continue
        valid, message = validate_support_attachment(file_storage)
        if not valid:
            raise ValueError(message)


def _save_attachments(files, *, ticket_id, uploader_user_id, role):
    rows = []
    for file_storage in list(files or [])[:5]:
        if not file_storage or not file_storage.filename:
            continue
        doc = store_support_attachment(
            file_storage,
            linked_ticket_id=ticket_id,
            uploader_user_id=uploader_user_id,
            role=role,
        )
        if doc:
            rows.append(_attachment_row(doc))
    return rows


def ensure_support_indexes():
    try:
        mongo.db.support_tickets.create_index([("ticket_ref", 1)])
        mongo.db.support_tickets.create_index([("user_id", 1), ("created_at", -1)])
        mongo.db.support_tickets.create_index([("status", 1), ("updated_at", -1)])
        mongo.db.support_tickets.create_index([("priority", 1), ("updated_at", -1)])
    except Exception:
        # Index creation must never block Support in an existing deployment.
        pass


def _serialize_message(row):
    item = dict(row or {})
    item["created_at_display"] = _display_datetime(item.get("created_at"))
    item["attachments"] = list(item.get("attachments") or [])
    return item


def _datetime_sort_key(value):
    if not value:
        return 0.0
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return 0.0
    if not isinstance(value, datetime):
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return value.timestamp()
    except Exception:
        return 0.0


def _legacy_messages(ticket):
    rows = []
    original = _clean(ticket.get("message"), 6000)
    if original:
        rows.append({
            "id": "legacy-original",
            "author_user_id": str(ticket.get("user_id") or ""),
            "author_name": ticket.get("user_name") or ticket.get("requester_name") or "Requester",
            "author_role": ticket.get("role") or "",
            "author_type": "requester",
            "body": original,
            "visibility": "public",
            "attachments": list(ticket.get("attachments") or []),
            "created_at": ticket.get("created_at"),
        })
    resolution = _clean(ticket.get("resolution_note"), 6000)
    if resolution:
        rows.append({
            "id": "legacy-resolution",
            "author_user_id": "",
            "author_name": ticket.get("updated_by") or "Support Team",
            "author_role": "super_admin",
            "author_type": "support",
            "body": resolution,
            "visibility": "public",
            "attachments": [],
            "created_at": ticket.get("resolved_at") or ticket.get("updated_at"),
        })
    return rows


def serialize_ticket(ticket, *, include_messages=False, include_internal=False):
    row = dict(ticket or {})
    row["id"] = str(row.get("_id") or "")
    row["ticket_ref"] = row.get("ticket_ref") or f"TCK-{row['id']}"
    row["status"] = _clean(row.get("status") or "open", 40).lower()
    row["status_label"] = SUPPORT_STATUSES.get(row["status"], row["status"].replace("_", " ").title())
    row["priority"] = _clean(row.get("priority") or "medium", 40).lower()
    row["priority_label"] = SUPPORT_PRIORITIES.get(row["priority"], row["priority"].title())
    row["problem_type"] = row.get("problem_type") or "Other Technical Issue"
    row["created_at_display"] = _display_datetime(row.get("created_at"))
    row["updated_at_display"] = _display_datetime(row.get("updated_at") or row.get("created_at"))
    row["resolved_at_display"] = _display_datetime(row.get("resolved_at")) if row.get("resolved_at") else ""
    row["last_activity_at_display"] = _display_datetime(row.get("last_activity_at") or row.get("updated_at") or row.get("created_at"))
    row["source_label"] = row.get("source_label") or ("Login Page" if row.get("ticket_source") == "login_page" else "Signed-in Support")
    row["attachments"] = list(row.get("attachments") or [])
    row["reply_count"] = max(len(row.get("messages") or []) - 1, 0)

    if include_messages:
        messages = list(row.get("messages") or []) or _legacy_messages(row)
        if not include_internal:
            messages = [item for item in messages if item.get("visibility") != "internal"]
        messages.sort(key=lambda item: _datetime_sort_key(item.get("created_at")))
        row["messages"] = [_serialize_message(item) for item in messages]
    else:
        row.pop("messages", None)

    history = list(row.get("history") or [])
    for item in history:
        item["created_at_display"] = _display_datetime(item.get("created_at"))
    row["history"] = history if include_internal else []
    return row


def create_ticket(actor_user_id, *, subject, problem_type, priority, message, files=None):
    ensure_support_indexes()
    actor = _get_actor(actor_user_id)
    role = actor.get("resolved_role")
    if role == "super_admin":
        raise PermissionError("Super Admin manages Support tickets and cannot raise a ticket from this desk.")

    subject = _clean(subject, 180)
    problem_type = _clean(problem_type, 120)
    priority = _clean(priority, 20).lower()
    message = _clean(message, 6000)
    if not subject or not problem_type or not priority or not message:
        raise ValueError("Please fill all required support ticket fields.")
    if problem_type not in SUPPORT_PROBLEM_TYPES:
        raise ValueError("Please select a valid problem type.")
    if priority not in SUPPORT_PRIORITIES:
        raise ValueError("Please select a valid priority.")

    _validate_attachments(files)
    ticket_ref = _new_ticket_ref("SUP")
    timestamp = now_utc()
    ticket = {
        "ticket_ref": ticket_ref,
        "ticket_source": "authenticated",
        "source_label": "Signed-in Support",
        "submitted_from": "Support Desk",
        "user_id": str(actor.get("_id") or ""),
        "user_name": actor.get("resolved_name") or "User",
        "username": actor.get("username") or "",
        "role": role,
        "phone": actor.get("phone") or actor.get("contact_no") or "",
        "email": actor.get("email") or "",
        "subject": subject,
        "problem_type": problem_type,
        "priority": priority,
        "message": message,
        "support_email": SUPPORT_EMAIL,
        "support_number": SUPPORT_NUMBER,
        "status": "open",
        "progress": "Ticket received",
        "resolution_note": "",
        "attachments": [],
        "messages": [],
        "history": [_history("ticket_created", actor.get("resolved_name"), role, "Ticket submitted to Support.")],
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_activity_at": timestamp,
        "resolved_at": None,
        "closed_at": None,
        "updated_by": None,
    }
    result = mongo.db.support_tickets.insert_one(ticket)
    ticket["_id"] = result.inserted_id

    attachments = _save_attachments(
        files,
        ticket_id=result.inserted_id,
        uploader_user_id=actor.get("_id"),
        role=role,
    )
    first_message = _message(
        actor.get("_id"),
        actor.get("resolved_name"),
        role,
        "requester",
        message,
        attachments=attachments,
    )
    mongo.db.support_tickets.update_one(
        {"_id": result.inserted_id},
        {"$set": {"attachments": attachments, "messages": [first_message], "updated_at": now_utc(), "last_activity_at": now_utc()}},
    )
    ticket = mongo.db.support_tickets.find_one({"_id": result.inserted_id}) or ticket
    _notify_super_admins("New Support Ticket", f"{ticket_ref} · {actor.get('resolved_name')} · {subject}")
    return {"ticket": serialize_ticket(ticket, include_messages=True), "message": f"Ticket {ticket_ref} raised successfully."}


def _actor_ticket_query(actor):
    if actor.get("resolved_role") == "super_admin":
        return {}
    return {"user_id": str(actor.get("_id") or "")}


def get_support_overview(actor_user_id, *, search="", status="all", priority="all", problem_type="all"):
    ensure_support_indexes()
    actor = _get_actor(actor_user_id)
    base_query = _actor_ticket_query(actor)
    query_parts = [base_query] if base_query else []

    search = _clean(search, 160)
    if search:
        escaped = __import__("re").escape(search)
        query_parts.append({"$or": [
            {"ticket_ref": {"$regex": escaped, "$options": "i"}},
            {"user_name": {"$regex": escaped, "$options": "i"}},
            {"username": {"$regex": escaped, "$options": "i"}},
            {"subject": {"$regex": escaped, "$options": "i"}},
            {"problem_type": {"$regex": escaped, "$options": "i"}},
            {"message": {"$regex": escaped, "$options": "i"}},
            {"progress": {"$regex": escaped, "$options": "i"}},
        ]})

    status = _clean(status, 40).lower()
    if status and status != "all" and status in SUPPORT_STATUSES:
        query_parts.append({"status": status})
    priority = _clean(priority, 40).lower()
    if priority and priority != "all" and priority in SUPPORT_PRIORITIES:
        query_parts.append({"priority": priority})
    problem_type = _clean(problem_type, 120)
    if problem_type and problem_type != "all":
        query_parts.append({"problem_type": problem_type})

    if not query_parts:
        query = {}
    elif len(query_parts) == 1:
        query = query_parts[0]
    else:
        query = {"$and": query_parts}

    rows = [serialize_ticket(item) for item in mongo.db.support_tickets.find(query).sort("updated_at", -1).limit(500)]

    scope = base_query or {}
    existing_problem_types = [
        _clean(value, 120)
        for value in mongo.db.support_tickets.distinct("problem_type", scope)
        if _clean(value, 120)
    ]
    problem_types = list(SUPPORT_PROBLEM_TYPES)
    for value in sorted(set(existing_problem_types), key=lambda item: item.lower()):
        if value not in problem_types:
            problem_types.append(value)

    summary = {
        "total": mongo.db.support_tickets.count_documents(scope),
        "open": mongo.db.support_tickets.count_documents({**scope, "status": "open"}),
        "in_progress": mongo.db.support_tickets.count_documents({**scope, "status": "in_progress"}),
        "waiting_for_user": mongo.db.support_tickets.count_documents({**scope, "status": "waiting_for_user"}),
        "resolved": mongo.db.support_tickets.count_documents({**scope, "status": "resolved"}),
        "closed": mongo.db.support_tickets.count_documents({**scope, "status": "closed"}),
    }
    return {
        "actor": {"role": actor.get("resolved_role"), "name": actor.get("resolved_name")},
        "tickets": rows,
        "summary": summary,
        "search": search,
        "selected_status": status or "all",
        "selected_priority": priority or "all",
        "selected_problem_type": problem_type or "all",
        "problem_types": problem_types,
        "priorities": SUPPORT_PRIORITIES,
        "statuses": SUPPORT_STATUSES,
        "support_email": SUPPORT_EMAIL,
        "support_number": SUPPORT_NUMBER,
        "can_create": actor.get("resolved_role") != "super_admin",
        "is_support_admin": actor.get("resolved_role") == "super_admin",
    }


def _load_ticket_for_actor(actor, ticket_id):
    oid = _to_object_id(ticket_id)
    if not oid:
        raise ValueError("Invalid support ticket reference.")
    ticket = mongo.db.support_tickets.find_one({"_id": oid})
    if not ticket:
        raise ValueError("Support ticket was not found.")
    if actor.get("resolved_role") != "super_admin" and str(ticket.get("user_id") or "") != str(actor.get("_id") or ""):
        raise PermissionError("You cannot view another user's support ticket.")
    return ticket


def get_ticket_detail(actor_user_id, ticket_id):
    actor = _get_actor(actor_user_id)
    ticket = _load_ticket_for_actor(actor, ticket_id)
    is_admin = actor.get("resolved_role") == "super_admin"
    mongo.db.support_tickets.update_one(
        {"_id": ticket["_id"]},
        {"$set": {"last_viewed_by_support_at" if is_admin else "last_viewed_by_user_at": now_utc()}},
    )
    row = serialize_ticket(ticket, include_messages=True, include_internal=is_admin)
    row["is_owner"] = not is_admin
    row["can_reply"] = row.get("status") != "closed"
    row["can_close"] = not is_admin and row.get("status") not in {"closed"}
    row["can_reopen"] = not is_admin and row.get("status") in {"resolved", "closed"}
    return {
        "ticket": row,
        "is_support_admin": is_admin,
        "statuses": SUPPORT_STATUSES,
        "priorities": SUPPORT_PRIORITIES,
        "support_email": SUPPORT_EMAIL,
        "support_number": SUPPORT_NUMBER,
    }


def add_ticket_reply(actor_user_id, ticket_id, *, message, files=None, internal=False):
    actor = _get_actor(actor_user_id)
    ticket = _load_ticket_for_actor(actor, ticket_id)
    is_admin = actor.get("resolved_role") == "super_admin"
    if internal and not is_admin:
        raise PermissionError("Only Support can add internal notes.")
    if ticket.get("status") == "closed" and not is_admin:
        raise ValueError("This ticket is closed. Reopen it before sending another reply.")

    message = _clean(message, 6000)
    if not message:
        raise ValueError("Please enter a reply.")

    attachments = _save_attachments(
        files,
        ticket_id=ticket.get("_id"),
        uploader_user_id=actor.get("_id"),
        role=actor.get("resolved_role"),
    )
    visibility = "internal" if internal else "public"
    reply = _message(
        actor.get("_id"),
        actor.get("resolved_name"),
        actor.get("resolved_role"),
        "support" if is_admin else "requester",
        message,
        visibility=visibility,
        attachments=attachments,
    )

    set_fields = {"updated_at": now_utc(), "last_activity_at": now_utc()}
    if not is_admin and ticket.get("status") in {"waiting_for_user", "resolved"}:
        set_fields.update({"status": "open", "progress": "User replied — awaiting Support review", "resolved_at": None})
    elif is_admin and not internal and ticket.get("status") == "open":
        set_fields.update({"status": "in_progress", "progress": "Support is working on this ticket"})

    mongo.db.support_tickets.update_one(
        {"_id": ticket.get("_id")},
        {
            "$push": {
                "messages": reply,
                "history": _history("internal_note" if internal else "reply_added", actor.get("resolved_name"), actor.get("resolved_role"), "Support note added." if internal else "Reply added."),
            },
            "$set": set_fields,
        },
    )

    if is_admin and not internal and ticket.get("user_id"):
        _notify_user(ticket.get("user_id"), ticket.get("role"), "Support replied", f"{ticket.get('ticket_ref')} · {ticket.get('subject')}")
    elif not is_admin:
        _notify_super_admins("Support ticket reply", f"{ticket.get('ticket_ref')} · {actor.get('resolved_name')} replied.")
    return {"message": "Reply sent successfully." if not internal else "Internal note saved."}


def update_ticket(actor_user_id, ticket_id, *, status, priority, progress="", resolution_note=""):
    actor = _get_actor(actor_user_id)
    if actor.get("resolved_role") != "super_admin":
        raise PermissionError("Only Super Admin can manage Support tickets.")
    ticket = _load_ticket_for_actor(actor, ticket_id)

    status = _clean(status, 40).lower()
    priority = _clean(priority, 40).lower()
    progress = _clean(progress, 1000)
    resolution_note = _clean(resolution_note, 4000)
    if status not in SUPPORT_STATUSES:
        raise ValueError("Invalid ticket status selected.")
    if priority not in SUPPORT_PRIORITIES:
        raise ValueError("Invalid ticket priority selected.")
    if status == "resolved" and not resolution_note:
        raise ValueError("Add a short resolution note before marking the ticket resolved.")

    timestamp = now_utc()
    set_fields = {
        "status": status,
        "priority": priority,
        "progress": progress or SUPPORT_STATUSES[status],
        "resolution_note": resolution_note,
        "updated_at": timestamp,
        "last_activity_at": timestamp,
        "updated_by": actor.get("resolved_name"),
    }
    if status in {"resolved", "closed"}:
        set_fields["resolved_at"] = ticket.get("resolved_at") or timestamp
    else:
        set_fields["resolved_at"] = None
    if status == "closed":
        set_fields["closed_at"] = timestamp
    elif ticket.get("status") == "closed":
        set_fields["closed_at"] = None

    history_note = f"Status: {SUPPORT_STATUSES[status]}; Priority: {SUPPORT_PRIORITIES[priority]}"
    mongo.db.support_tickets.update_one(
        {"_id": ticket.get("_id")},
        {"$set": set_fields, "$push": {"history": _history("ticket_updated", actor.get("resolved_name"), "super_admin", history_note)}},
    )

    if ticket.get("user_id"):
        _notify_user(ticket.get("user_id"), ticket.get("role"), "Support ticket updated", f"{ticket.get('ticket_ref')} is now {SUPPORT_STATUSES[status]}.")
    return {"message": "Support ticket updated successfully."}


def user_ticket_action(actor_user_id, ticket_id, action):
    actor = _get_actor(actor_user_id)
    if actor.get("resolved_role") == "super_admin":
        raise PermissionError("Use the Support management controls for this ticket.")
    ticket = _load_ticket_for_actor(actor, ticket_id)
    action = _clean(action, 20).lower()
    timestamp = now_utc()
    if action == "close":
        if ticket.get("status") == "closed":
            return {"message": "This ticket is already closed."}
        set_fields = {
            "status": "closed",
            "progress": "Closed by requester",
            "closed_at": timestamp,
            "resolved_at": ticket.get("resolved_at") or timestamp,
            "updated_at": timestamp,
            "last_activity_at": timestamp,
        }
        note = "Requester closed the ticket."
    elif action == "reopen":
        if ticket.get("status") not in {"resolved", "closed"}:
            raise ValueError("Only resolved or closed tickets can be reopened.")
        set_fields = {
            "status": "open",
            "progress": "Reopened by requester — awaiting Support review",
            "closed_at": None,
            "resolved_at": None,
            "updated_at": timestamp,
            "last_activity_at": timestamp,
        }
        note = "Requester reopened the ticket."
    else:
        raise ValueError("Invalid ticket action.")

    mongo.db.support_tickets.update_one(
        {"_id": ticket.get("_id")},
        {"$set": set_fields, "$push": {"history": _history(action, actor.get("resolved_name"), actor.get("resolved_role"), note)}},
    )
    _notify_super_admins("Support ticket updated", f"{ticket.get('ticket_ref')} · {note}")
    return {"message": note}


def create_public_login_ticket(*, requester_name, mobile, email, account_type, identifier, problem_type, subject, message, files=None):
    ensure_support_indexes()
    requester_name = _clean(requester_name, 160)
    mobile = _clean(mobile, 30)
    email = _clean(email, 160)
    account_type = _clean(account_type, 80)
    identifier = _clean(identifier, 160)
    problem_type = _clean(problem_type, 120)
    subject = _clean(subject, 180)
    message = _clean(message, 6000)
    if not requester_name or not mobile or not account_type or not problem_type or not subject or not message:
        raise ValueError("Please fill all required fields.")
    if account_type not in PUBLIC_ACCOUNT_TYPES:
        raise ValueError("Please select a valid account type.")
    if problem_type not in PUBLIC_LOGIN_PROBLEM_TYPES:
        raise ValueError("Please select a valid login problem type.")

    _validate_attachments(files)
    priority = "high" if problem_type in {"Unable to Login", "Account Inactive", "Password Reset Required"} else "medium"
    ticket_ref = _new_ticket_ref("LOGIN")
    timestamp = now_utc()
    ticket = {
        "ticket_ref": ticket_ref,
        "ticket_source": "login_page",
        "source_label": "Login Page",
        "submitted_from": "Login Page Support & Help",
        "user_id": "",
        "user_name": requester_name,
        "requester_name": requester_name,
        "username": identifier,
        "role": "public_login_user",
        "phone": mobile,
        "phone_normalized": _phone_digits(mobile),
        "email": email,
        "account_type": account_type,
        "login_identifier": identifier,
        "subject": subject,
        "problem_type": problem_type,
        "priority": priority,
        "message": message,
        "support_email": SUPPORT_EMAIL,
        "support_number": SUPPORT_NUMBER,
        "status": "open",
        "progress": "Ticket received from login page",
        "resolution_note": "",
        "attachments": [],
        "messages": [],
        "history": [_history("ticket_created", requester_name, "public_login_user", "Login Support request submitted.")],
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_activity_at": timestamp,
        "resolved_at": None,
        "closed_at": None,
        "updated_by": None,
    }
    result = mongo.db.support_tickets.insert_one(ticket)
    attachments = _save_attachments(files, ticket_id=result.inserted_id, uploader_user_id=None, role="public_login_user")
    first_message = _message("", requester_name, "public_login_user", "requester", message, attachments=attachments)
    mongo.db.support_tickets.update_one(
        {"_id": result.inserted_id},
        {"$set": {"attachments": attachments, "messages": [first_message], "updated_at": now_utc(), "last_activity_at": now_utc()}},
    )
    _notify_super_admins("New Login Support Ticket", f"{ticket_ref} · {requester_name} · {subject}")
    return {"ticket_ref": ticket_ref, "message": f"Support request submitted successfully. Your ticket reference is {ticket_ref}."}


def _public_ticket_match(ticket, mobile):
    if not ticket:
        return False
    requested = _phone_digits(mobile)
    stored = ticket.get("phone_normalized") or _phone_digits(ticket.get("phone"))
    return bool(requested and stored and requested == stored)


def get_public_login_ticket(ticket_ref, mobile):
    ticket_ref = _clean(ticket_ref, 60).upper()
    mobile = _clean(mobile, 30)
    if not ticket_ref or not mobile:
        raise ValueError("Enter both ticket reference and registered mobile number.")
    ticket = mongo.db.support_tickets.find_one({"ticket_ref": ticket_ref, "ticket_source": "login_page"})
    if not ticket or not _public_ticket_match(ticket, mobile):
        raise ValueError("No matching login Support ticket was found. Check the ticket reference and mobile number.")
    return serialize_ticket(ticket, include_messages=True, include_internal=False)


def reply_public_login_ticket(*, ticket_ref, mobile, message, files=None):
    ticket = mongo.db.support_tickets.find_one({"ticket_ref": _clean(ticket_ref, 60).upper(), "ticket_source": "login_page"})
    if not ticket or not _public_ticket_match(ticket, mobile):
        raise ValueError("No matching login Support ticket was found.")
    if ticket.get("status") == "closed":
        raise ValueError("This ticket is closed. Please raise a new Login Support request if you still need help.")
    message = _clean(message, 6000)
    if not message:
        raise ValueError("Please enter your reply.")
    attachments = _save_attachments(files, ticket_id=ticket.get("_id"), uploader_user_id=None, role="public_login_user")
    reply = _message("", ticket.get("requester_name") or ticket.get("user_name"), "public_login_user", "requester", message, attachments=attachments)
    set_fields = {"updated_at": now_utc(), "last_activity_at": now_utc()}
    if ticket.get("status") in {"waiting_for_user", "resolved"}:
        set_fields.update({"status": "open", "progress": "Requester replied — awaiting Support review", "resolved_at": None})
    mongo.db.support_tickets.update_one(
        {"_id": ticket.get("_id")},
        {"$push": {"messages": reply, "history": _history("public_reply", ticket.get("requester_name") or ticket.get("user_name"), "public_login_user", "Login Support reply added.")}, "$set": set_fields},
    )
    _notify_super_admins("Login Support reply", f"{ticket.get('ticket_ref')} · Requester replied.")
    return {"message": "Reply sent to Support."}
