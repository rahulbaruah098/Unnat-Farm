import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from app.extensions import mongo
from app.utils.db import init_indexes
from app.services.location_service import seed_locations
from app.services.user_service import create_user

app = create_app()

with app.app_context():
    db_name = app.config["MONGO_DB_NAME"]
    mongo.db.client.drop_database(db_name)
    db = mongo.db
    init_indexes(db)
    seed_locations(force=True)

    superadmin = create_user(
        "Super Admin",
        "super_admin",
        username="superadmin",
        password="admin123",
        created_by="system",
    )
    create_user(
        "AVPL Admin",
        "avpl_admin",
        username="avpladmin",
        password="admin123",
        created_by=str(superadmin["_id"]),
    )

    print("Seed completed successfully.")
    print("Login IDs created:")
    print("superadmin / admin123")
    print("avpladmin / admin123")
    print("Create all other users from the MIS after login.")
