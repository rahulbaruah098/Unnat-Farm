from app.extensions import mongo
from app.utils.helpers import now_utc
from app.utils.security import save_file


def store_document(file_storage, linked_user_id, linked_master_id, uploader_user_id, role, document_type):
    filename = save_file(file_storage, prefix=document_type.lower().replace(" ", "_"))
    if not filename:
        return None
    doc = {
        "filename": filename,
        "linked_user_id": linked_user_id,
        "linked_master_id": linked_master_id,
        "uploader_user_id": uploader_user_id,
        "role": role,
        "document_type": document_type,
        "status": "active",
        "created_at": now_utc(),
    }
    result = mongo.db.documents.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc
