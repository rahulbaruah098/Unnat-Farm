from app.extensions import mongo
from app.utils.helpers import now_utc


def log_action(actor_user_id, action, entity_type, entity_id=None, remarks=None, metadata=None):
    mongo.db.audit_logs.insert_one({
        "actor_user_id": actor_user_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "remarks": remarks,
        "metadata": metadata or {},
        "created_at": now_utc(),
    })
