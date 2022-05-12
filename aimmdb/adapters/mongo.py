import collections.abc
import os
import uuid
from pathlib import Path

import pymongo
from tiled.adapters.utils import IndexersMixin, tree_repr
from tiled.query_registration import QueryTranslationRegistry, register
from tiled.structures.core import StructureFamily
from tiled.structures.dataframe import serialize_arrow
from tiled.utils import (APACHE_ARROW_FILE_MIME_TYPE, UNCHANGED, DictView,
                         ListView)

from aimmdb.adapters.array import WritingArrayAdapter
from aimmdb.adapters.dataframe import WritingDataFrameAdapter
from aimmdb.models import Document

_mime_structure_association = {
    StructureFamily.array: "application/x-hdf5",
    StructureFamily.dataframe: APACHE_ARROW_FILE_MIME_TYPE,
}


# Generic mongo adapter adapted from databroker
class MongoAdapterBase:
    structure_family = "node"

    query_registry = QueryTranslationRegistry()
    register_query = query_registry.register
    register_query_lazy = query_registry.register_lazy

    # TODO remove when writing routes are upstreamed to tiled
    from aimmdb.router_tiled import router

    include_routers = [router]

    def __init__(
        self,
        *,
        metadata_collection,
        data_directory,
        queries=None,
        sorting=None,
        metadata=None,
        principal=None,
        access_policy=None,
    ):
        self.metadata_collection = metadata_collection
        self.data_directory = Path(data_directory).resolve()
        if not self.data_directory.exists():
            raise ValueError(f"Directory {self.data_directory} does not exist.")
        if not self.data_directory.is_dir():
            raise ValueError(
                f"The given directory path {self.data_directory} is not a directory."
            )
        if not os.access(self.data_directory, os.W_OK):
            raise ValueError("Directory {self.directory} is not writeable.")
        self.queries = queries or []
        self.sorting = sorting or []
        self.metadata = metadata or {}
        self.principal = principal
        self.access_policy = access_policy
        super().__init__()

    @classmethod
    def from_uri(
        cls,
        uri,
        metadata_collection,
        data_directory,
        *,
        metadata=None,
        access_policy=None,
    ):
        if not pymongo.uri_parser.parse_uri(uri)["database"]:
            raise ValueError(
                f"Invalid URI: {uri!r} " f"Did you forget to include a database?"
            )
        client = pymongo.MongoClient(uri)
        database = client.get_database()
        return cls(
            metadata_collection=database[metadata_collection],
            data_directory=data_directory,
            metadata=metadata,
            access_policy=access_policy,
        )

    @classmethod
    def from_mongomock(cls, data_directory, *, metadata=None):
        import mongomock

        mongo_client = mongomock.MongoClient()
        db = mongo_client["test"]
        metadata_collection = db["test"]

        return cls(
            metadata_collection=metadata_collection,
            data_directory=data_directory,
            metadata=metadata,
        )

    def authenticated_as(self, principal):
        if self.principal is not None:
            raise RuntimeError(f"Already authenticated as {self.principal}")
        if self.access_policy is not None:
            tree = self.access_policy.filter_results(self, principal)
        else:
            tree = self.new_variation(principal=principal)
        return tree

    def new_variation(
        self,
        metadata=UNCHANGED,
        queries=UNCHANGED,
        sorting=UNCHANGED,
        principal=UNCHANGED,
        **kwargs,
    ):
        if metadata is UNCHANGED:
            metadata = self.metadata
        if queries is UNCHANGED:
            queries = self.queries
        if sorting is UNCHANGED:
            sorting = self.sorting
        if principal is UNCHANGED:
            principal = self.principal
        return type(self)(
            metadata_collection=self.metadata_collection,
            data_directory=self.data_directory,
            metadata=metadata,
            queries=queries,
            sorting=sorting,
            access_policy=self.access_policy,
            principal=principal,
            **kwargs,
        )

    def search(self, query):
        """
        Return a MongoAdapter with a subset of the mapping.
        """
        return self.query_registry(query, self)

    def sort(self, sorting):
        return self.new_variation(sorting=sorting)

    def uid(self):
        return str(uuid.uuid4())

    def post_metadata(self, metadata, structure_family, structure, specs):

        uid = self.uid()

        validated_document = Document(
            uid=uid,
            structure_family=structure_family,
            structure=structure,
            metadata=metadata,
            specs=specs,
            mimetype=_mime_structure_association[structure_family],
        )

        # After validating the document must be encoded to bytes again to make it compatible with MongoDB
        if validated_document.structure_family == StructureFamily.dataframe:
            validated_document.structure.micro.meta = bytes(
                serialize_arrow(validated_document.structure.micro.meta, {})
            )

        self.metadata_collection.insert_one(validated_document.dict(by_alias=True))
        return uid

    def _build_mongo_query(self, *queries):
        combined = self.queries + list(queries)
        if combined:
            return {"$and": combined}
        else:
            return {}


class MongoAdapter(MongoAdapterBase, collections.abc.Mapping, IndexersMixin):
    def __len__(self):
        return self.metadata_collection.count_documents(
            # self._build_mongo_query({"active": True})
            self._build_mongo_query({"data_url": {"$ne": None}})
        )

    def __length_hint__(self):
        # https://www.python.org/dev/peps/pep-0424/
        return self.metadata_collection.estimated_document_count(
            # self._build_mongo_query({"active": True}),
            self._build_mongo_query({"data_url": {"$ne": None}}),
        )

    def __repr__(self):
        # Display up to the first N keys to avoid making a giant service
        # request. Use _keys_slicer because it is unauthenticated.
        N = 10
        return tree_repr(self, self._keys_slice(0, N, direction=1))

    def __getitem__(self, key):
        query = {"_id": key}
        doc = self.metadata_collection.find_one(self._build_mongo_query(query))
        if doc is None:
            raise KeyError(key)

        if doc["structure_family"] == StructureFamily.array:
            return WritingArrayAdapter(
                self.metadata_collection, self.data_directory, doc
            )
        elif doc["structure_family"] == StructureFamily.dataframe:
            return WritingDataFrameAdapter(
                self.metadata_collection, self.data_directory, doc
            )
        else:
            raise ValueError("Unsupported Structure Family value in the databse")

    def __iter__(self):
        # TODO Apply pagination, as we do in Databroker.
        for doc in list(
            self.metadata_collection.find(
                # self._build_mongo_query({"active": True}), {"uid": True}
                self._build_mongo_query({"data_url": {"$ne": None}}),
                {"_id": True},
            )
        ):
            yield doc["_id"]

    def _keys_slice(self, start, stop, direction):
        assert direction == 1, "direction=-1 should be handled by the client"
        skip = start or 0
        if stop is not None:
            limit = stop - skip
        else:
            limit = None
        for doc in self.metadata_collection.find(
            # self._build_mongo_query({"active": True}),
            self._build_mongo_query({"data_url": {"$ne": None}}),
            skip=skip,
            limit=limit,
        ):
            yield doc["_id"]

    def _items_slice(self, start, stop, direction):
        assert direction == 1, "direction=-1 should be handled by the client"
        skip = start or 0
        if stop is not None:
            limit = stop - skip
        else:
            limit = None

        for doc in self.metadata_collection.find(
            # self._build_mongo_query({"active": True}),
            self._build_mongo_query({"data_url": {"$ne": None}}),
            skip=skip,
            limit=limit,
        ):
            if doc["structure_family"] == StructureFamily.array:
                yield (
                    doc["_id"],
                    WritingArrayAdapter(
                        self.metadata_collection, self.data_directory, doc
                    ),
                )
            elif doc["structure_family"] == StructureFamily.dataframe:
                yield (
                    doc["_id"],
                    WritingDataFrameAdapter(
                        self.metadata_collection, self.data_directory, doc
                    ),
                )
            else:
                raise ValueError("Unsupported Structure Family value in the databse")

    def _item_by_index(self, index, direction):
        assert direction == 1, "direction=-1 should be handled by the client"
        return self._items_slice(index, index + 1, 1)