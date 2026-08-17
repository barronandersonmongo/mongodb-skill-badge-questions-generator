"""MongoDB client and collection accessors."""

from functools import lru_cache

from pymongo import MongoClient
from pymongo.database import Database

from app.config import get_settings


@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    return MongoClient(get_settings().mongodb_uri)


def get_database() -> Database:
    return get_client()[get_settings().database]
