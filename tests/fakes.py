"""Test doubles: an in-memory MongoDB collection and a fake Anthropic client.

The collection implements only the operators this program uses ($set,
$setOnInsert, $addToSet, equality/array-membership/$ne/$exists/$or/$expr filters,
projection, sort, search-index creation and a cosine $vectorSearch) — enough to assert real
semantics such as "$setOnInsert must not overwrite an existing field".

mongomock is not used: as of mongomock 4.3.0 / pymongo 4.17.0 its
bulk_write path breaks on pymongo's newer add_update() signature.
"""

from typing import Any


class FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs

    def sort(self, key: str, direction: int = 1) -> "FakeCursor":
        self._docs.sort(key=lambda d: d.get(key), reverse=direction < 0)
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeBulkResult:
    def __init__(self, matched: int, modified: int, upserted_ids: dict[int, Any]):
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_ids = upserted_ids


class FakeUpdateResult:
    def __init__(self, matched: int, modified: int):
        self.matched_count = matched
        self.modified_count = modified


class FakeDeleteResult:
    def __init__(self, deleted: int):
        self.deleted_count = deleted


class FakeCollection:
    """Minimal in-memory stand-in for a pymongo Collection."""

    def __init__(self, docs: list[dict[str, Any]] | None = None):
        self.docs: list[dict[str, Any]] = [dict(d) for d in (docs or [])]
        self.indexes: list[dict[str, Any]] = []
        self.search_indexes: list[dict[str, Any]] = []
        self._next_id = 1

    # --- search indexes ---
    def list_search_indexes(self):
        return list(self.search_indexes)

    def create_search_index(self, model) -> str:
        name = getattr(model, "document", {}).get("name", "unnamed")
        self.search_indexes.append(
            {"name": name, "definition": getattr(model, "document", {})}
        )
        return name

    def aggregate(self, pipeline):
        """Support only the $vectorSearch + $project shape this program uses.

        The real index is configured with autoEmbed, so the query is text and Atlas
        does the embedding. Similarity is approximated here by word overlap, which
        is enough to order neighbours and exercise the score threshold.
        """
        stage = pipeline[0]["$vectorSearch"]
        limit = stage["limit"]
        query = stage["query"]
        scored = []
        for doc in self.docs:
            text = doc.get(stage["path"])
            if not text:
                continue
            scored.append((_word_overlap(query, text), doc))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        out = []
        for score, doc in scored[:limit]:
            item = {k: doc.get(k) for k in ("slug", "name", "description", "status")}
            item["score"] = score
            out.append(item)
        return out

    # --- indexes ---
    def create_index(self, keys, **kwargs) -> str:
        name = kwargs.get("name") or "_".join(k for k, _ in keys)
        self.indexes.append({"keys": list(keys), "name": name, **kwargs})
        return name

    # --- reads ---
    def _matches(self, doc: dict, query: dict) -> bool:
        for key, expected in query.items():
            if key == "$or":
                if not any(self._matches(doc, clause) for clause in expected):
                    return False
                continue
            if key == "$expr":
                # Only the {"$ne": ["$a", "$b"]} field comparison is supported.
                left, right = expected["$ne"]
                if doc.get(left.lstrip("$")) == doc.get(right.lstrip("$")):
                    return False
                continue
            actual = doc.get(key)
            if isinstance(expected, dict) and "$ne" in expected:
                if actual == expected["$ne"]:
                    return False
            elif isinstance(expected, dict) and "$exists" in expected:
                if (key in doc) != expected["$exists"]:
                    return False
            elif isinstance(actual, list) and not isinstance(expected, list):
                # MongoDB matches a scalar against any element of an array field,
                # which is how questions are filtered by badge and by category.
                if expected not in actual:
                    return False
            elif actual != expected:
                return False
        return True

    def _project(self, doc: dict, projection: dict | None) -> dict:
        out = dict(doc)
        for field, keep in (projection or {}).items():
            if not keep:
                out.pop(field, None)
        return out

    def find(self, query: dict | None = None, projection: dict | None = None):
        matched = [d for d in self.docs if self._matches(d, query or {})]
        return FakeCursor([self._project(d, projection) for d in matched])

    def find_one(self, query: dict, projection: dict | None = None):
        for doc in self.docs:
            if self._matches(doc, query):
                return self._project(doc, projection)
        return None

    def count_documents(self, query: dict | None = None) -> int:
        return sum(1 for d in self.docs if self._matches(d, query or {}))

    # --- writes ---
    def _apply_update(self, doc: dict, update: dict, *, inserting: bool) -> bool:
        before = {k: list(v) if isinstance(v, list) else v for k, v in doc.items()}
        doc.update(update.get("$set", {}))
        if inserting:
            doc.update(update.get("$setOnInsert", {}))
        for field, value in update.get("$addToSet", {}).items():
            values = doc.setdefault(field, [])
            incoming = value["$each"] if isinstance(value, dict) and "$each" in value else [value]
            for item in incoming:
                if item not in values:
                    values.append(item)
        return doc != before

    def update_one(self, query: dict, update: dict, upsert: bool = False):
        for doc in self.docs:
            if self._matches(doc, query):
                changed = self._apply_update(doc, update, inserting=False)
                return FakeUpdateResult(1, int(changed))
        if not upsert:
            return FakeUpdateResult(0, 0)
        self._insert(query, update)
        return FakeUpdateResult(0, 0)

    def _insert(self, query: dict, update: dict) -> Any:
        # Operator expressions in the filter (e.g. {"$ne": True}) are match
        # conditions, not values to store — mirror MongoDB and drop them.
        seed = {k: v for k, v in query.items() if not isinstance(v, dict)}
        doc = {"_id": self._next_id, **seed}
        self._next_id += 1
        self._apply_update(doc, update, inserting=True)
        self.docs.append(doc)
        return doc["_id"]

    def insert_many(self, docs: list[dict]) -> list[Any]:
        ids = []
        for doc in docs:
            stored = {"_id": self._next_id, **doc}
            self._next_id += 1
            self.docs.append(stored)
            ids.append(stored["_id"])
        return ids

    def delete_one(self, query: dict) -> "FakeDeleteResult":
        for index, doc in enumerate(self.docs):
            if self._matches(doc, query):
                del self.docs[index]
                return FakeDeleteResult(1)
        return FakeDeleteResult(0)

    def bulk_write(self, operations, ordered: bool = True) -> FakeBulkResult:
        matched = modified = 0
        upserted: dict[int, Any] = {}
        for index, operation in enumerate(operations):
            query, update, upsert = _unpack_update_one(operation)
            existing = next((d for d in self.docs if self._matches(d, query)), None)
            if existing is not None:
                matched += 1
                modified += int(self._apply_update(existing, update, inserting=False))
            elif upsert:
                upserted[index] = self._insert(query, update)
        return FakeBulkResult(matched, modified, upserted)


def _word_overlap(left: str, right: str) -> float:
    """Jaccard overlap, standing in for cosine similarity over embeddings."""
    a, b = set(left.lower().split()), set(right.lower().split())
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _unpack_update_one(operation) -> tuple[dict, dict, bool]:
    """Read filter/update/upsert off a pymongo UpdateOne without public getters."""
    return (
        getattr(operation, "_filter"),
        getattr(operation, "_doc"),
        bool(getattr(operation, "_upsert")),
    )


# --- Anthropic doubles ---


class FakeBlock:
    def __init__(self, type: str, text: str = ""):
        self.type = type
        self.text = text


class FakeMessage:
    def __init__(
        self,
        text: str = "",
        stop_reason: str = "end_turn",
        stop_details: Any = None,
        content: list[FakeBlock] | None = None,
    ):
        self.content = content if content is not None else [FakeBlock("text", text)]
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class FakeStream:
    def __init__(self, message: FakeMessage):
        self._message = message

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def get_final_message(self) -> FakeMessage:
        return self._message


class FakeParsedResponse:
    def __init__(self, parsed_output, stop_reason: str = "end_turn", stop_details=None):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class FakeMessages:
    def __init__(self, stream_messages: list[FakeMessage], parsed=None, parsed_by_format=None):
        self._stream_messages = list(stream_messages)
        self._parsed = parsed
        # A single run can make several parse() calls against different schemas
        # (extraction, then badge attribution). Scripting by output_format lets a
        # test answer each one without depending on call order.
        self._parsed_by_format = parsed_by_format or {}
        self.stream_calls: list[dict] = []
        self.parse_calls: list[dict] = []

    def stream(self, **kwargs) -> FakeStream:
        self.stream_calls.append(kwargs)
        if not self._stream_messages:
            raise AssertionError("stream() called more times than the test scripted")
        return FakeStream(self._stream_messages.pop(0))

    def parse(self, **kwargs) -> FakeParsedResponse:
        self.parse_calls.append(kwargs)
        scripted = self._parsed_by_format.get(kwargs.get("output_format"), self._parsed)
        return (
            scripted
            if isinstance(scripted, FakeParsedResponse)
            else FakeParsedResponse(scripted)
        )


class FakeAnthropic:
    def __init__(
        self,
        stream_messages: list[FakeMessage] | None = None,
        parsed=None,
        parsed_by_format=None,
    ):
        self.messages = FakeMessages(stream_messages or [], parsed, parsed_by_format)
