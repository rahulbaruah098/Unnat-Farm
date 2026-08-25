
from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from urllib.parse import urlparse

from app.extensions import mongo
from app.utils.helpers import now_utc


COURSE_COLLECTION = "lms_courses"
MODULE_COLLECTION = "lms_course_modules"
LESSON_COLLECTION = "lms_lessons"
RESOURCE_COLLECTION = "lms_resources"
PROGRESS_COLLECTION = "lms_progress"
LEGACY_COLLECTION = "lms_materials"

ACTIVITY_CHOICES = ["Pig", "Goat", "Poultry", "Cattle", "Fishery", "Agri"]
AGRI_SUB_CATEGORY_CHOICES = [
    "Maize",
    "Joha Rice",
    "Mustard Seed",
    "Black Rice",
    "Ginger",
    "Turmeric",
    "Black Pepper",
]
AUDIENCE_CHOICES = {
    "farmer": "Farmer",
    "ufc_mitra": "UFC Mitra",
    "ufc_admin": "UFC Admin",
    "all": "All",
}
LEVEL_CHOICES = {
    "beginner": "Beginner",
    "intermediate": "Intermediate",
    "advanced": "Advanced",
}
RESOURCE_TYPES = {
    "pdf": "PDF",
    "video": "Video",
    "image": "Image",
    "document": "Document",
    "link": "External Link",
}


def _to_object_id(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _clean(value, maximum=2000):
    return str(value or "").strip()[:maximum]


def _bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked"}


def _int(value, default=0, minimum=0, maximum=100000):
    try:
        number = int(str(value or default).strip())
    except Exception:
        number = default
    return max(minimum, min(number, maximum))


def _canonical_list(values, allowed):
    allowed_map = {str(item).casefold(): item for item in allowed}
    clean = []
    seen = set()
    for raw in values or []:
        key = str(raw or "").strip().casefold()
        item = allowed_map.get(key)
        if item and item not in seen:
            seen.add(item)
            clean.append(item)
    return clean


def _active_user(user):
    return bool(
        user
        and user.get("active", True) is not False
        and user.get("is_active", True) is not False
        and str(user.get("status") or "").lower() != "inactive"
    )


def _ensure_indexes():
    mongo.db[COURSE_COLLECTION].create_index([("status", ASCENDING), ("audience", ASCENDING), ("updated_at", DESCENDING)])
    mongo.db[COURSE_COLLECTION].create_index([("title", ASCENDING)])
    mongo.db[MODULE_COLLECTION].create_index([("course_id", ASCENDING), ("order", ASCENDING)])
    mongo.db[LESSON_COLLECTION].create_index([("course_id", ASCENDING), ("module_id", ASCENDING), ("order", ASCENDING)])
    mongo.db[RESOURCE_COLLECTION].create_index([("course_id", ASCENDING), ("lesson_id", ASCENDING), ("order", ASCENDING)])
    mongo.db[PROGRESS_COLLECTION].create_index([("user_id", ASCENDING), ("course_id", ASCENDING)], unique=True)
    mongo.db[PROGRESS_COLLECTION].create_index([("course_id", ASCENDING), ("status", ASCENDING)])


def _get_user(user_id):
    oid = _to_object_id(user_id)
    if not oid:
        raise ValueError("Please login again.")
    user = mongo.db.users.find_one({"_id": oid}) or {}
    if not _active_user(user):
        raise PermissionError("This user account is not active.")
    row = dict(user)
    row["resolved_role"] = str(user.get("role") or "").strip().lower()
    row["resolved_name"] = user.get("name") or user.get("username") or "User"
    return row


def _farmer_profile(user):
    return (
        mongo.db.farmer_master.find_one({"linked_user_id": str(user.get("_id"))})
        or mongo.db.farmer_master.find_one({"linked_user_id": user.get("_id")})
        or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")})
        or {}
    )


def _profile_target_snapshot(user):
    role = user.get("resolved_role") or ""
    if role != "farmer":
        return {
            "role": role,
            "activities": [],
            "agri_sub_categories": [],
            "centre_uid": user.get("mapped_centre_uid") or user.get("centre_uid") or "",
        }
    profile = _farmer_profile(user)
    activities = _canonical_list(profile.get("activities") or [], ACTIVITY_CHOICES)
    agri_sub_categories = _canonical_list(
        profile.get("agri_sub_categories") or [],
        AGRI_SUB_CATEGORY_CHOICES,
    )
    return {
        "role": role,
        "activities": activities,
        "agri_sub_categories": agri_sub_categories,
        "centre_uid": profile.get("centre_uid") or user.get("mapped_centre_uid") or user.get("centre_uid") or "",
        "farmer_name": profile.get("name") or user.get("resolved_name") or "Farmer",
    }


def _course_matches_profile(course, user, profile=None):
    role = user.get("resolved_role") or ""
    audience = str(course.get("audience") or "farmer").strip().lower()
    if audience not in {"all", role}:
        return False
    if role != "farmer":
        return True

    target_mode = str(course.get("target_mode") or "all").strip().lower()
    if target_mode == "all":
        return True

    profile = profile or _profile_target_snapshot(user)
    farmer_activities = set(profile.get("activities") or [])
    farmer_subcats = set(profile.get("agri_sub_categories") or [])
    target_activities = set(_canonical_list(course.get("target_activities") or [], ACTIVITY_CHOICES))
    target_subcats = set(_canonical_list(course.get("target_agri_sub_categories") or [], AGRI_SUB_CATEGORY_CHOICES))

    # No explicit targeting means general Farmer content.
    if not target_activities and not target_subcats:
        return True

    non_agri_targets = target_activities - {"Agri"}
    if farmer_activities.intersection(non_agri_targets):
        return True

    if "Agri" in target_activities:
        if "Agri" in farmer_activities and (not target_subcats or farmer_subcats.intersection(target_subcats)):
            return True

    if not target_activities and target_subcats and farmer_subcats.intersection(target_subcats):
        return True

    return False


def _serialize_course(course):
    if not course:
        return None
    row = dict(course)
    row["id"] = str(row.get("_id") or "")
    row["audience_label"] = AUDIENCE_CHOICES.get(row.get("audience"), str(row.get("audience") or "").title())
    row["level_label"] = LEVEL_CHOICES.get(row.get("level"), str(row.get("level") or "beginner").title())
    row["mandatory"] = row.get("mandatory") is True
    row["target_activities"] = _canonical_list(row.get("target_activities") or [], ACTIVITY_CHOICES)
    row["target_agri_sub_categories"] = _canonical_list(
        row.get("target_agri_sub_categories") or [],
        AGRI_SUB_CATEGORY_CHOICES,
    )
    if str(row.get("target_mode") or "all") == "all":
        row["target_label"] = "All eligible learners"
    else:
        target_bits = list(row["target_activities"]) + list(row["target_agri_sub_categories"])
        row["target_label"] = ", ".join(target_bits) if target_bits else "All Farmers"
    return row


def _serialize_module(module):
    row = dict(module or {})
    row["id"] = str(row.get("_id") or "")
    row["course_id_str"] = str(row.get("course_id") or "")
    return row


def _serialize_lesson(lesson):
    row = dict(lesson or {})
    row["id"] = str(row.get("_id") or "")
    row["module_id_str"] = str(row.get("module_id") or "")
    row["course_id_str"] = str(row.get("course_id") or "")
    row["required"] = row.get("required") is not False
    return row


def _serialize_resource(resource):
    row = dict(resource or {})
    row["id"] = str(row.get("_id") or "")
    row["lesson_id_str"] = str(row.get("lesson_id") or "")
    row["course_id_str"] = str(row.get("course_id") or "")
    row["resource_type_label"] = RESOURCE_TYPES.get(row.get("resource_type"), str(row.get("resource_type") or "").title())
    return row


def _progress_doc(user_id, course_id):
    user_oid = _to_object_id(user_id)
    course_oid = _to_object_id(course_id)
    if not user_oid or not course_oid:
        return {}
    return mongo.db[PROGRESS_COLLECTION].find_one({"user_id": user_oid, "course_id": course_oid}) or {}


def _course_lessons(course_id):
    oid = _to_object_id(course_id)
    if not oid:
        return []
    return list(
        mongo.db[LESSON_COLLECTION]
        .find({"course_id": oid, "status": {"$ne": "archived"}})
        .sort([("module_order", ASCENDING), ("order", ASCENDING), ("created_at", ASCENDING)])
    )


def _progress_summary(course_id, progress=None):
    lessons = _course_lessons(course_id)
    required_ids = [str(row["_id"]) for row in lessons if row.get("required") is not False]
    all_ids = [str(row["_id"]) for row in lessons]
    progress = dict(progress or {})
    completed_ids = {str(value) for value in progress.get("completed_lesson_ids") or []}
    denominator_ids = required_ids or all_ids
    completed_required = len([lesson_id for lesson_id in denominator_ids if lesson_id in completed_ids])
    total = len(denominator_ids)
    percent = int(round((completed_required / total) * 100)) if total else 0
    if total and completed_required >= total:
        status = "completed"
    elif completed_ids or progress.get("started_at"):
        status = "in_progress"
    else:
        status = "not_started"
    return {
        "total_lessons": len(all_ids),
        "required_lessons": len(required_ids),
        "completed_lessons": len(completed_ids.intersection(set(all_ids))),
        "completed_required": completed_required,
        "progress_percent": percent,
        "progress_status": status,
        "started_at": progress.get("started_at"),
        "completed_at": progress.get("completed_at"),
    }


def _touch_started(user, course):
    timestamp = now_utc()
    mongo.db[PROGRESS_COLLECTION].update_one(
        {"user_id": user["_id"], "course_id": course["_id"]},
        {
            "$setOnInsert": {
                "user_id": user["_id"],
                "user_id_str": str(user["_id"]),
                "user_name": user.get("resolved_name") or "",
                "course_id": course["_id"],
                "course_id_str": str(course["_id"]),
                "course_title": course.get("title") or "Course",
                "started_at": timestamp,
                "completed_lesson_ids": [],
                "viewed_resource_ids": [],
                "created_at": timestamp,
            },
            "$set": {"updated_at": timestamp},
        },
        upsert=True,
    )


def create_course(actor_user_id, data):
    _ensure_indexes()
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") not in {"avpl_admin", "sales_unnatfarm", "sales_nelocals"}:
        raise PermissionError("You are not allowed to manage LMS courses.")

    title = _clean(data.get("title"), 180)
    if len(title) < 3:
        raise ValueError("Course title must contain at least 3 characters.")
    audience = str(data.get("audience") or "farmer").strip().lower()
    if audience not in AUDIENCE_CHOICES:
        raise ValueError("Choose a valid audience.")
    level = str(data.get("level") or "beginner").strip().lower()
    if level not in LEVEL_CHOICES:
        raise ValueError("Choose a valid course level.")
    target_mode = str(data.get("target_mode") or "all").strip().lower()
    if target_mode not in {"all", "profile"}:
        target_mode = "all"

    activities = _canonical_list(data.get("target_activities") or [], ACTIVITY_CHOICES)
    subcats = _canonical_list(data.get("target_agri_sub_categories") or [], AGRI_SUB_CATEGORY_CHOICES)
    if subcats and "Agri" not in activities:
        activities.append("Agri")

    timestamp = now_utc()
    doc = {
        "title": title,
        "short_description": _clean(data.get("short_description"), 600),
        "description": _clean(data.get("description"), 6000),
        "audience": audience,
        "level": level,
        "mandatory": _bool(data.get("mandatory")),
        "certificate_enabled": _bool(data.get("certificate_enabled")),
        "estimated_minutes": _int(data.get("estimated_minutes"), 0, 0, 100000),
        "target_mode": target_mode,
        "target_activities": activities if target_mode == "profile" else [],
        "target_agri_sub_categories": subcats if target_mode == "profile" else [],
        "status": "draft",
        "created_by": actor["_id"],
        "created_by_name": actor.get("resolved_name") or "",
        "created_role": actor.get("resolved_role") or "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = mongo.db[COURSE_COLLECTION].insert_one(doc)
    doc["_id"] = result.inserted_id
    return {"course": _serialize_course(doc), "message": "LMS course created as Draft."}


def update_course(actor_user_id, course_id, data):
    _ensure_indexes()
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") not in {"avpl_admin", "sales_unnatfarm", "sales_nelocals"}:
        raise PermissionError("You are not allowed to manage LMS courses.")
    oid = _to_object_id(course_id)
    course = mongo.db[COURSE_COLLECTION].find_one({"_id": oid}) if oid else None
    if not course:
        raise ValueError("LMS course was not found.")

    title = _clean(data.get("title"), 180)
    if len(title) < 3:
        raise ValueError("Course title must contain at least 3 characters.")
    audience = str(data.get("audience") or course.get("audience") or "farmer").strip().lower()
    if audience not in AUDIENCE_CHOICES:
        raise ValueError("Choose a valid audience.")
    level = str(data.get("level") or course.get("level") or "beginner").strip().lower()
    if level not in LEVEL_CHOICES:
        raise ValueError("Choose a valid course level.")
    target_mode = str(data.get("target_mode") or "all").strip().lower()
    activities = _canonical_list(data.get("target_activities") or [], ACTIVITY_CHOICES)
    subcats = _canonical_list(data.get("target_agri_sub_categories") or [], AGRI_SUB_CATEGORY_CHOICES)
    if subcats and "Agri" not in activities:
        activities.append("Agri")

    patch = {
        "title": title,
        "short_description": _clean(data.get("short_description"), 600),
        "description": _clean(data.get("description"), 6000),
        "audience": audience,
        "level": level,
        "mandatory": _bool(data.get("mandatory")),
        "certificate_enabled": _bool(data.get("certificate_enabled")),
        "estimated_minutes": _int(data.get("estimated_minutes"), 0, 0, 100000),
        "target_mode": target_mode if target_mode in {"all", "profile"} else "all",
        "target_activities": activities if target_mode == "profile" else [],
        "target_agri_sub_categories": subcats if target_mode == "profile" else [],
        "updated_at": now_utc(),
    }
    mongo.db[COURSE_COLLECTION].update_one({"_id": oid}, {"$set": patch})
    return {"course": _serialize_course(mongo.db[COURSE_COLLECTION].find_one({"_id": oid})), "message": "Course settings updated."}


def set_course_status(actor_user_id, course_id, status):
    _ensure_indexes()
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") not in {"avpl_admin", "sales_unnatfarm", "sales_nelocals"}:
        raise PermissionError("You are not allowed to manage LMS courses.")
    oid = _to_object_id(course_id)
    course = mongo.db[COURSE_COLLECTION].find_one({"_id": oid}) if oid else None
    if not course:
        raise ValueError("LMS course was not found.")
    status = str(status or "").strip().lower()
    if status not in {"draft", "published", "archived"}:
        raise ValueError("Invalid course status.")
    if status == "published":
        lesson_count = mongo.db[LESSON_COLLECTION].count_documents({"course_id": oid, "status": {"$ne": "archived"}})
        if lesson_count < 1:
            raise ValueError("Add at least one lesson before publishing this course.")
    patch = {"status": status, "updated_at": now_utc()}
    if status == "published":
        patch["published_at"] = now_utc()
        patch["published_by"] = actor["_id"]
    mongo.db[COURSE_COLLECTION].update_one({"_id": oid}, {"$set": patch})
    return {"course": _serialize_course(mongo.db[COURSE_COLLECTION].find_one({"_id": oid})), "message": f"Course marked {status.title()}."}


def create_module(actor_user_id, course_id, title, description="", order=0):
    _ensure_indexes()
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") not in {"avpl_admin", "sales_unnatfarm", "sales_nelocals"}:
        raise PermissionError("You are not allowed to manage LMS modules.")
    course_oid = _to_object_id(course_id)
    course = mongo.db[COURSE_COLLECTION].find_one({"_id": course_oid}) if course_oid else None
    if not course:
        raise ValueError("LMS course was not found.")
    clean_title = _clean(title, 180)
    if len(clean_title) < 2:
        raise ValueError("Module title is required.")
    timestamp = now_utc()
    doc = {
        "course_id": course_oid,
        "course_id_str": str(course_oid),
        "title": clean_title,
        "description": _clean(description, 1000),
        "order": _int(order, 0, 0, 10000),
        "status": "active",
        "created_by": actor["_id"],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = mongo.db[MODULE_COLLECTION].insert_one(doc)
    doc["_id"] = result.inserted_id
    return {"module": _serialize_module(doc), "message": "Module added."}


def create_lesson(actor_user_id, module_id, data):
    _ensure_indexes()
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") not in {"avpl_admin", "sales_unnatfarm", "sales_nelocals"}:
        raise PermissionError("You are not allowed to manage LMS lessons.")
    module_oid = _to_object_id(module_id)
    module = mongo.db[MODULE_COLLECTION].find_one({"_id": module_oid, "status": {"$ne": "archived"}}) if module_oid else None
    if not module:
        raise ValueError("LMS module was not found.")
    title = _clean(data.get("title"), 180)
    if len(title) < 2:
        raise ValueError("Lesson title is required.")
    timestamp = now_utc()
    doc = {
        "course_id": module["course_id"],
        "course_id_str": str(module["course_id"]),
        "module_id": module_oid,
        "module_id_str": str(module_oid),
        "module_order": _int(module.get("order"), 0),
        "title": title,
        "summary": _clean(data.get("summary"), 1000),
        "content_text": _clean(data.get("content_text"), 12000),
        "estimated_minutes": _int(data.get("estimated_minutes"), 0, 0, 10000),
        "order": _int(data.get("order"), 0, 0, 10000),
        "required": _bool(data.get("required", True)),
        "status": "active",
        "created_by": actor["_id"],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = mongo.db[LESSON_COLLECTION].insert_one(doc)
    doc["_id"] = result.inserted_id
    return {"lesson": _serialize_lesson(doc), "message": "Lesson added."}


def create_resource(actor_user_id, lesson_id, data):
    _ensure_indexes()
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") not in {"avpl_admin", "sales_unnatfarm", "sales_nelocals"}:
        raise PermissionError("You are not allowed to manage LMS resources.")
    lesson_oid = _to_object_id(lesson_id)
    lesson = mongo.db[LESSON_COLLECTION].find_one({"_id": lesson_oid, "status": {"$ne": "archived"}}) if lesson_oid else None
    if not lesson:
        raise ValueError("LMS lesson was not found.")
    resource_type = str(data.get("resource_type") or "").strip().lower()
    if resource_type not in RESOURCE_TYPES:
        raise ValueError("Choose a valid resource type.")
    title = _clean(data.get("title"), 180)
    if len(title) < 2:
        raise ValueError("Resource title is required.")

    file_name = _clean(data.get("file_name"), 500)
    external_url = _clean(data.get("external_url"), 1000)
    if resource_type == "link":
        parsed = urlparse(external_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Enter a valid http/https resource link.")
        file_name = ""
    elif not file_name:
        raise ValueError("Upload a file for this resource.")

    timestamp = now_utc()
    doc = {
        "course_id": lesson["course_id"],
        "course_id_str": str(lesson["course_id"]),
        "module_id": lesson["module_id"],
        "module_id_str": str(lesson["module_id"]),
        "lesson_id": lesson_oid,
        "lesson_id_str": str(lesson_oid),
        "title": title,
        "description": _clean(data.get("description"), 1000),
        "resource_type": resource_type,
        "file_name": file_name,
        "external_url": external_url if resource_type == "link" else "",
        "order": _int(data.get("order"), 0, 0, 10000),
        "downloadable": _bool(data.get("downloadable")),
        "status": "active",
        "created_by": actor["_id"],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = mongo.db[RESOURCE_COLLECTION].insert_one(doc)
    doc["_id"] = result.inserted_id
    return {"resource": _serialize_resource(doc), "message": "Learning resource added."}


def archive_resource(actor_user_id, resource_id):
    _ensure_indexes()
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") not in {"avpl_admin", "sales_unnatfarm", "sales_nelocals"}:
        raise PermissionError("You are not allowed to manage LMS resources.")
    oid = _to_object_id(resource_id)
    if not oid:
        raise ValueError("Invalid resource.")
    result = mongo.db[RESOURCE_COLLECTION].update_one(
        {"_id": oid, "status": {"$ne": "archived"}},
        {"$set": {"status": "archived", "archived_at": now_utc(), "archived_by": actor["_id"], "updated_at": now_utc()}},
    )
    if result.matched_count < 1:
        raise ValueError("Resource was not found.")
    return {"message": "Resource archived."}


def get_admin_overview(search="", status=""):
    _ensure_indexes()
    q = _clean(search, 120)
    status = str(status or "").strip().lower()
    query = {}
    if status in {"draft", "published", "archived"}:
        query["status"] = status
    if q:
        query["$or"] = [
            {"title": {"$regex": q, "$options": "i"}},
            {"short_description": {"$regex": q, "$options": "i"}},
            {"description": {"$regex": q, "$options": "i"}},
            {"target_activities": {"$regex": q, "$options": "i"}},
            {"target_agri_sub_categories": {"$regex": q, "$options": "i"}},
        ]

    rows = []
    for course in mongo.db[COURSE_COLLECTION].find(query).sort("updated_at", DESCENDING):
        row = _serialize_course(course)
        row["module_count"] = mongo.db[MODULE_COLLECTION].count_documents({"course_id": course["_id"], "status": {"$ne": "archived"}})
        row["lesson_count"] = mongo.db[LESSON_COLLECTION].count_documents({"course_id": course["_id"], "status": {"$ne": "archived"}})
        row["resource_count"] = mongo.db[RESOURCE_COLLECTION].count_documents({"course_id": course["_id"], "status": {"$ne": "archived"}})
        progress_rows = list(mongo.db[PROGRESS_COLLECTION].find({"course_id": course["_id"]}, {"status": 1, "completed_at": 1}))
        row["started_count"] = len(progress_rows)
        row["completed_count"] = len([p for p in progress_rows if p.get("completed_at")])
        rows.append(row)

    legacy = []
    for item in mongo.db[LEGACY_COLLECTION].find({}).sort("created_at", DESCENDING).limit(30):
        legacy.append({
            "id": str(item.get("_id") or ""),
            "title": item.get("title") or "Legacy Material",
            "audience": item.get("audience") or "all",
            "lms_type": item.get("lms_type") or "",
            "activity_category": item.get("activity_category") or "all",
            "description": item.get("description") or "",
            "file_name": item.get("file_name") or "",
        })

    summary = {
        "course_count": mongo.db[COURSE_COLLECTION].count_documents({"status": {"$ne": "archived"}}),
        "published_count": mongo.db[COURSE_COLLECTION].count_documents({"status": "published"}),
        "draft_count": mongo.db[COURSE_COLLECTION].count_documents({"status": "draft"}),
        "started_learners": len(mongo.db[PROGRESS_COLLECTION].distinct("user_id")),
        "completed_records": mongo.db[PROGRESS_COLLECTION].count_documents({"completed_at": {"$exists": True, "$ne": None}}),
    }
    return {
        "courses": rows,
        "legacy_items": legacy,
        "summary": summary,
        "query": q,
        "status_filter": status,
        "activity_choices": ACTIVITY_CHOICES,
        "agri_sub_category_choices": AGRI_SUB_CATEGORY_CHOICES,
        "audience_choices": AUDIENCE_CHOICES,
        "level_choices": LEVEL_CHOICES,
    }


def get_admin_course_context(course_id):
    _ensure_indexes()
    oid = _to_object_id(course_id)
    course = mongo.db[COURSE_COLLECTION].find_one({"_id": oid}) if oid else None
    if not course:
        raise ValueError("LMS course was not found.")

    modules = []
    module_docs = list(
        mongo.db[MODULE_COLLECTION]
        .find({"course_id": oid, "status": {"$ne": "archived"}})
        .sort([("order", ASCENDING), ("created_at", ASCENDING)])
    )
    for module in module_docs:
        module_row = _serialize_module(module)
        lessons = []
        for lesson in mongo.db[LESSON_COLLECTION].find(
            {"module_id": module["_id"], "status": {"$ne": "archived"}}
        ).sort([("order", ASCENDING), ("created_at", ASCENDING)]):
            lesson_row = _serialize_lesson(lesson)
            resources = [
                _serialize_resource(resource)
                for resource in mongo.db[RESOURCE_COLLECTION].find(
                    {"lesson_id": lesson["_id"], "status": {"$ne": "archived"}}
                ).sort([("order", ASCENDING), ("created_at", ASCENDING)])
            ]
            lesson_row["resources"] = resources
            lessons.append(lesson_row)
        module_row["lessons"] = lessons
        modules.append(module_row)

    progress_rows = list(mongo.db[PROGRESS_COLLECTION].find({"course_id": oid}).sort("updated_at", DESCENDING).limit(200))
    learners = []
    for p in progress_rows:
        summary = _progress_summary(oid, p)
        learners.append({
            "user_id": str(p.get("user_id") or ""),
            "user_name": p.get("user_name") or "Learner",
            **summary,
        })
    return {
        "course": _serialize_course(course),
        "modules": modules,
        "learners": learners,
        "activity_choices": ACTIVITY_CHOICES,
        "agri_sub_category_choices": AGRI_SUB_CATEGORY_CHOICES,
        "audience_choices": AUDIENCE_CHOICES,
        "level_choices": LEVEL_CHOICES,
        "resource_types": RESOURCE_TYPES,
    }


def _legacy_matches(item, user, profile):
    audience = str(item.get("audience") or "all").strip().lower()
    role = user.get("resolved_role") or ""
    if audience not in {"all", role}:
        return False
    if role != "farmer":
        return True
    category = str(item.get("activity_category") or "all").strip()
    if category.casefold() == "all":
        return True
    allowed = set(profile.get("activities") or []) | set(profile.get("agri_sub_categories") or [])
    return category in allowed


def get_learner_overview(actor_user_id, search="", activity_filter=""):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    role = user.get("resolved_role")
    if role not in {"farmer", "ufc_mitra", "ufc_admin"}:
        raise PermissionError("LMS is available to Farmers, UFC Mitras and UFC Admins.")
    profile = _profile_target_snapshot(user)
    q = _clean(search, 120)
    activity_filter = _clean(activity_filter, 80)

    query = {"status": "published", "audience": {"$in": [role, "all"]}}
    if q:
        query["$or"] = [
            {"title": {"$regex": q, "$options": "i"}},
            {"short_description": {"$regex": q, "$options": "i"}},
            {"description": {"$regex": q, "$options": "i"}},
            {"target_activities": {"$regex": q, "$options": "i"}},
            {"target_agri_sub_categories": {"$regex": q, "$options": "i"}},
        ]

    rows = []
    for course in mongo.db[COURSE_COLLECTION].find(query).sort([("mandatory", DESCENDING), ("published_at", DESCENDING), ("updated_at", DESCENDING)]):
        if not _course_matches_profile(course, user, profile):
            continue
        row = _serialize_course(course)
        if activity_filter and activity_filter != "all":
            course_tags = set(row.get("target_activities") or []) | set(row.get("target_agri_sub_categories") or [])
            if activity_filter not in course_tags and str(row.get("target_mode") or "all") != "all":
                continue
        progress = _progress_doc(user["_id"], course["_id"])
        row.update(_progress_summary(course["_id"], progress))
        row["module_count"] = mongo.db[MODULE_COLLECTION].count_documents({"course_id": course["_id"], "status": {"$ne": "archived"}})
        row["lesson_count"] = mongo.db[LESSON_COLLECTION].count_documents({"course_id": course["_id"], "status": {"$ne": "archived"}})
        rows.append(row)

    legacy = []
    for item in mongo.db[LEGACY_COLLECTION].find({"audience": {"$in": [role, "all"]}}).sort("created_at", DESCENDING):
        if not _legacy_matches(item, user, profile):
            continue
        if q:
            haystack = " ".join(
                str(item.get(k) or "")
                for k in ["title", "description", "lms_type", "activity_category", "file_name"]
            ).casefold()
            if q.casefold() not in haystack:
                continue
        legacy.append({
            "id": str(item.get("_id") or ""),
            "title": item.get("title") or "Learning Material",
            "description": item.get("description") or "",
            "lms_type": item.get("lms_type") or "",
            "activity_category": item.get("activity_category") or "all",
            "file_name": item.get("file_name") or "",
        })

    completed = len([row for row in rows if row.get("progress_status") == "completed"])
    in_progress = len([row for row in rows if row.get("progress_status") == "in_progress"])
    mandatory_pending = len([
        row for row in rows
        if row.get("mandatory") and row.get("progress_status") != "completed"
    ])
    filters = ["all"] + list(profile.get("activities") or []) + list(profile.get("agri_sub_categories") or [])
    return {
        "courses": rows,
        "legacy_items": legacy,
        "profile": profile,
        "query": q,
        "activity_filter": activity_filter,
        "filter_choices": filters,
        "summary": {
            "assigned": len(rows),
            "in_progress": in_progress,
            "completed": completed,
            "mandatory_pending": mandatory_pending,
        },
    }


def get_learner_course_context(actor_user_id, course_id):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    oid = _to_object_id(course_id)
    course = mongo.db[COURSE_COLLECTION].find_one({"_id": oid, "status": "published"}) if oid else None
    if not course:
        raise ValueError("This LMS course is not available.")
    profile = _profile_target_snapshot(user)
    if not _course_matches_profile(course, user, profile):
        raise PermissionError("This course is not assigned to your registered profile.")

    _touch_started(user, course)
    progress = _progress_doc(user["_id"], oid)
    completed_ids = {str(value) for value in progress.get("completed_lesson_ids") or []}
    viewed_resource_ids = {str(value) for value in progress.get("viewed_resource_ids") or []}

    modules = []
    for module in mongo.db[MODULE_COLLECTION].find(
        {"course_id": oid, "status": {"$ne": "archived"}}
    ).sort([("order", ASCENDING), ("created_at", ASCENDING)]):
        module_row = _serialize_module(module)
        lessons = []
        for lesson in mongo.db[LESSON_COLLECTION].find(
            {"module_id": module["_id"], "status": {"$ne": "archived"}}
        ).sort([("order", ASCENDING), ("created_at", ASCENDING)]):
            lesson_row = _serialize_lesson(lesson)
            lesson_row["completed"] = lesson_row["id"] in completed_ids
            resources = []
            for resource in mongo.db[RESOURCE_COLLECTION].find(
                {"lesson_id": lesson["_id"], "status": {"$ne": "archived"}}
            ).sort([("order", ASCENDING), ("created_at", ASCENDING)]):
                resource_row = _serialize_resource(resource)
                resource_row["viewed"] = resource_row["id"] in viewed_resource_ids
                resources.append(resource_row)
            lesson_row["resources"] = resources
            lessons.append(lesson_row)
        module_row["lessons"] = lessons
        modules.append(module_row)

    return {
        "course": _serialize_course(course),
        "modules": modules,
        "profile": profile,
        "progress": _progress_summary(oid, progress),
    }


def mark_lesson_complete(actor_user_id, lesson_id, completed=True):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    lesson_oid = _to_object_id(lesson_id)
    lesson = mongo.db[LESSON_COLLECTION].find_one({"_id": lesson_oid, "status": {"$ne": "archived"}}) if lesson_oid else None
    if not lesson:
        raise ValueError("Lesson was not found.")
    course = mongo.db[COURSE_COLLECTION].find_one({"_id": lesson.get("course_id"), "status": "published"}) or {}
    if not course or not _course_matches_profile(course, user, _profile_target_snapshot(user)):
        raise PermissionError("This lesson is not assigned to you.")

    _touch_started(user, course)
    timestamp = now_utc()
    if completed:
        mongo.db[PROGRESS_COLLECTION].update_one(
            {"user_id": user["_id"], "course_id": course["_id"]},
            {"$addToSet": {"completed_lesson_ids": str(lesson_oid)}, "$set": {"updated_at": timestamp}},
        )
    else:
        mongo.db[PROGRESS_COLLECTION].update_one(
            {"user_id": user["_id"], "course_id": course["_id"]},
            {"$pull": {"completed_lesson_ids": str(lesson_oid)}, "$set": {"updated_at": timestamp}},
        )

    progress = _progress_doc(user["_id"], course["_id"])
    summary = _progress_summary(course["_id"], progress)
    patch = {"status": summary["progress_status"], "updated_at": timestamp}
    if summary["progress_status"] == "completed":
        patch["completed_at"] = progress.get("completed_at") or timestamp
    else:
        patch["completed_at"] = None
    mongo.db[PROGRESS_COLLECTION].update_one(
        {"user_id": user["_id"], "course_id": course["_id"]},
        {"$set": patch},
    )
    return {
        "message": "Lesson marked complete." if completed else "Lesson marked incomplete.",
        "course_id": str(course["_id"]),
        "progress": _progress_summary(course["_id"], mongo.db[PROGRESS_COLLECTION].find_one({"user_id": user["_id"], "course_id": course["_id"]}) or {}),
    }


def get_resource_for_learner(actor_user_id, resource_id):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    oid = _to_object_id(resource_id)
    resource = mongo.db[RESOURCE_COLLECTION].find_one({"_id": oid, "status": "active"}) if oid else None
    if not resource:
        raise ValueError("Learning resource was not found.")
    course = mongo.db[COURSE_COLLECTION].find_one({"_id": resource.get("course_id"), "status": "published"}) or {}
    if not course or not _course_matches_profile(course, user, _profile_target_snapshot(user)):
        raise PermissionError("This resource is not assigned to you.")

    _touch_started(user, course)
    mongo.db[PROGRESS_COLLECTION].update_one(
        {"user_id": user["_id"], "course_id": course["_id"]},
        {
            "$addToSet": {"viewed_resource_ids": str(resource["_id"])},
            "$set": {"last_resource_viewed_at": now_utc(), "updated_at": now_utc()},
        },
    )
    return _serialize_resource(resource)
