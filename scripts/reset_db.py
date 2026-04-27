import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from app.extensions import mongo

app = create_app()
with app.app_context():
    db_name = app.config["MONGO_DB_NAME"]
    mongo.db.client.drop_database(db_name)
    print(f"Dropped database: {db_name}")
