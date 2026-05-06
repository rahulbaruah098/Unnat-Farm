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