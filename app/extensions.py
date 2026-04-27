from flask import current_app, g
from pymongo import MongoClient


class MongoExtension:
    def init_app(self, app):
        app.teardown_appcontext(self.teardown)

    @property
    def db(self):
        if "mongo_db" not in g:
            client = MongoClient(current_app.config["MONGO_URI"])
            g.mongo_client = client
            g.mongo_db = client[current_app.config["MONGO_DB_NAME"]]
        return g.mongo_db

    def teardown(self, exception=None):
        client = g.pop("mongo_client", None)
        g.pop("mongo_db", None)
        if client:
            client.close()


mongo = MongoExtension()
