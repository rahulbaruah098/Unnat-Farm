import os

from flask import current_app
from werkzeug.utils import safe_join

from app.extensions import mongo
from app.utils.helpers import now_utc
from app.utils.security import save_file


def store_document(file_storage, linked_user_id, linked_master_id, uploader_user_id, role, document_type):
    """
    Store a document and replace only old active documents of the same type
    for the same user and role.
    """

    if not file_storage or not file_storage.filename:
        return None

    linked_user_id_str = str(linked_user_id) if linked_user_id else None
    linked_master_id_str = str(linked_master_id) if linked_master_id else None
    uploader_user_id_str = str(uploader_user_id) if uploader_user_id else None

    filename = save_file(file_storage, prefix=document_type.lower().replace(" ", "_"))

    if not filename:
        return None

    mongo.db.documents.update_many(
        {
            "linked_user_id": linked_user_id_str,
            "role": role,
            "document_type": document_type,
            "status": "active",
        },
        {
            "$set": {
                "status": "replaced",
                "replaced_at": now_utc(),
                "replaced_by": uploader_user_id_str,
                "updated_at": now_utc(),
            }
        }
    )

    doc = {
        "filename": filename,
        "linked_user_id": linked_user_id_str,
        "linked_master_id": linked_master_id_str,
        "uploader_user_id": uploader_user_id_str,
        "role": role,
        "document_type": document_type,
        "status": "active",
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }

    result = mongo.db.documents.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


def replace_document(file_storage, linked_user_id, linked_master_id, uploader_user_id, role, document_type):
    """
    Backward-compatible wrapper.
    Existing imports will not break.
    """
    return store_document(
        file_storage,
        linked_user_id,
        linked_master_id,
        uploader_user_id,
        role,
        document_type
    )

def candidate_upload_dirs():
    """Return safe upload directories used by current and legacy builds."""
    dirs = []
    configured = current_app.config.get("UPLOAD_FOLDER") or "uploads"
    if configured:
        dirs.append(configured)
        if not os.path.isabs(configured):
            dirs.append(os.path.abspath(configured))
            dirs.append(os.path.abspath(os.path.join(current_app.root_path, "..", configured)))
            dirs.append(os.path.abspath(os.path.join(current_app.root_path, configured)))

    dirs.append(os.path.abspath(os.path.join(current_app.root_path, "..", "uploads")))
    dirs.append(os.path.abspath(os.path.join(current_app.root_path, "uploads")))

    seen = set()
    clean = []
    for directory in dirs:
        if not directory:
            continue
        absolute = os.path.abspath(directory)
        if absolute not in seen:
            seen.add(absolute)
            clean.append(absolute)
    return clean


def find_document_path(filename):
    """Resolve a stored document reference only when the real file exists."""
    safe_name = os.path.basename(str(filename or ""))
    if not safe_name or safe_name in {".", ".."}:
        return None

    for directory in candidate_upload_dirs():
        candidate = safe_join(directory, safe_name)
        if candidate and os.path.isfile(candidate):
            return candidate

        if not os.path.isdir(directory):
            continue

        for root, _dirs, files in os.walk(directory):
            if safe_name in files:
                return os.path.join(root, safe_name)
    return None


def document_file_exists(filename):
    return bool(find_document_path(filename))
