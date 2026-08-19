"""Test doubles: an in-memory MongoDB collection and a fake Anthropic client.

The collection implements only the operators this program uses ($set,
$setOnInsert, $addToSet, equality/array-membership/$ne/$exists/$or/$expr filters,
projection, sort, search-index creation and a cosine $vectorSearch) — enough to assert real
semantics such as "$setOnInsert must not overwrite an existing field".

mongomock is not used: as of mongomock 4.3.0 / pymongo 4.17.0 its
bulk_write path breaks on pymongo's newer add_update() signature.
"""

import re
from typing import Any

from bson import ObjectId


class FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs

    def sort(self, key, direction: int = 1) -> "FakeCursor":
        # pymongo accepts either a field name or a list of (field, direction) pairs;
        # a text search sorts by {"$meta": "textScore"}, which is descending.
        if isinstance(key, list):
            field, spec = key[0]
            descending = isinstance(spec, dict) or spec < 0
            self._docs.sort(
                key=lambda d: (d.get(field) is None, d.get(field)), reverse=descending
            )
            return self
        self._docs.sort(key=lambda d: d.get(key), reverse=direction < 0)
        return self

    def limit(self, count: int) -> "FakeCursor":
        self._docs = self._docs[:count]
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
        """Support the aggregation shapes this program uses.

        A pipeline starting with $vectorSearch is a similarity search (below); one
        starting with $group is a summary over the collection, used by the
        documentation corpus screen to count pages per source; one starting with
        $unwind fans an array field out before grouping, which is how questions are
        counted per badge — a question filed under three badges counts for each.
        """
        if pipeline and "$unwind" in pipeline[0]:
            docs = self._unwound(pipeline[0]["$unwind"], self.docs)
            return self._aggregate_group(pipeline[1:], docs=docs)
        if pipeline and "$group" in pipeline[0]:
            return self._aggregate_group(pipeline)
        return self._aggregate_vector_search(pipeline)

    @staticmethod
    def _unwound(path: str, docs: list[dict]) -> list[dict]:
        """One document per array element, as $unwind produces.

        A document whose array is empty or absent contributes nothing, which is what
        Mongo does without preserveNullAndEmptyArrays — a question filed under no badge
        should not appear in a per-badge count.
        """
        field = str(path).lstrip("$")
        out = []
        for doc in docs:
            for value in doc.get(field) or []:
                out.append({**doc, field: value})
        return out

    def _aggregate_group(self, pipeline, docs=None):
        """$group with $sum/$max/$min accumulators, then an optional $sort.

        The group key may be a field path or a document of them: counting questions per
        badge groups on badge *and* status together, and a fake that only understood a
        single path would silently collapse the statuses into one number.
        """
        spec = pipeline[0]["$group"]
        key = spec["_id"]
        groups: dict[Any, list[dict]] = {}
        for doc in docs if docs is not None else self.docs:
            if isinstance(key, str):
                group_key: Any = doc.get(key.lstrip("$"))
            elif isinstance(key, dict):
                # Hashable so it can key the dict, and turned back into a document
                # below — the caller reads row["_id"]["slug"], as it would in Mongo.
                group_key = tuple(
                    (name, doc.get(str(path).lstrip("$"))) for name, path in key.items()
                )
            else:
                group_key = None
            groups.setdefault(group_key, []).append(doc)

        def field_values(docs, expression):
            path = list(expression.values())[0]
            if path == 1:
                return [1] * len(docs)
            name = path.lstrip("$")
            values = [_dotted(d, name) for d in docs]
            return [v for v in values if v is not None]

        rows = []
        for group_key, docs in groups.items():
            row: dict[str, Any] = {
                "_id": dict(group_key) if isinstance(group_key, tuple) else group_key
            }
            for field, expression in spec.items():
                if field == "_id":
                    continue
                operator = next(iter(expression))
                values = field_values(docs, expression)
                if operator == "$sum":
                    row[field] = sum(values) if values else 0
                elif operator == "$max":
                    row[field] = max(values) if values else None
                elif operator == "$min":
                    row[field] = min(values) if values else None
                elif operator == "$addToSet":
                    # Distinct values, order not guaranteed by MongoDB either. Used to
                    # count how many pages a set of chunks spans.
                    row[field] = list(dict.fromkeys(values))
                else:
                    raise NotImplementedError(f"accumulator {operator} not faked")
            rows.append(row)

        for stage in pipeline[1:]:
            if "$sort" in stage:
                field, direction = next(iter(stage["$sort"].items()))
                rows.sort(key=lambda r: (r.get(field) is None, r.get(field)),
                          reverse=direction < 0)
        return rows

    def _aggregate_vector_search(self, pipeline):
        """Support only the $vectorSearch + $project shape this program uses.

        The real index is configured with autoEmbed, so the query is text and Atlas
        does the embedding. Similarity is approximated here by word overlap, which
        is enough to order neighbours and exercise the score threshold.

        The $project stage is honoured rather than assumed: badges and questions
        project different fields, and a fake that returned one shape for both would
        let a caller pass while asking for fields it never receives in production.
        """
        stage = pipeline[0]["$vectorSearch"]
        limit = stage["limit"]
        query = stage["query"]
        scored = []
        for doc in self.docs:
            text = doc.get(stage["path"])
            if not text:
                continue
            # A stored documentation page is Markdown beginning with its title heading,
            # so the embedding of its text sees the title too. Fixtures here set the
            # title as a separate field, so it is scored alongside the body rather than
            # being invisible to the stand-in.
            text = f"{doc.get('title') or ''} {text}"
            similarity = _word_overlap(query, text)
            if similarity <= 0:
                # A document sharing nothing with the query is not a neighbour. Atlas
                # returns the nearest `limit` documents whatever their score, but its
                # scores are never zero — approximating that here would make every
                # search in a small fake corpus return the whole corpus.
                continue
            scored.append((similarity, doc))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        projection = next(
            (s["$project"] for s in pipeline[1:] if "$project" in s), None
        )
        out = []
        for score, doc in scored[:limit]:
            out.append(_project_search_result(doc, projection, score))
        return out

    # --- indexes ---
    def create_index(self, keys, **kwargs) -> str:
        name = kwargs.get("name") or "_".join(k for k, _ in keys)
        self.indexes.append({"keys": list(keys), "name": name, **kwargs})
        return name

    # --- reads ---
    @staticmethod
    def _text_score(doc: dict, search: str) -> float:
        """Stand in for MongoDB's textScore, with the same title weighting.

        Real scoring is term-frequency based and weighted per field; this counts term
        occurrences and weights the title ten times, which is enough to order results
        and to exercise "the title match comes first".
        """
        terms = [t.strip('"').casefold() for t in search.split() if t.strip('"')]
        title = (doc.get("title") or "").casefold()
        body = (doc.get("text") or "").casefold()
        return sum(title.count(t) * 10 + body.count(t) for t in terms)

    def _matches(self, doc: dict, query: dict) -> bool:
        for key, expected in query.items():
            if key == "$text":
                if not self._text_score(doc, expected["$search"]):
                    return False
                continue
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
            if isinstance(expected, dict) and "$in" in expected:
                if actual not in expected["$in"]:
                    return False
            elif isinstance(expected, dict) and expected.keys() & {"$lt", "$lte", "$gt", "$gte"}:
                # Range comparisons, used to select pages by size.
                if actual is None:
                    return False
                for operator, bound in expected.items():
                    if operator == "$lt" and not actual < bound:
                        return False
                    if operator == "$lte" and not actual <= bound:
                        return False
                    if operator == "$gt" and not actual > bound:
                        return False
                    if operator == "$gte" and not actual >= bound:
                        return False
            elif isinstance(expected, dict) and "$ne" in expected:
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
        """Apply MongoDB's projection semantics, which are all-or-nothing.

        A projection naming any field for inclusion returns *only* those fields; one
        naming only exclusions returns everything else. Mixing the two (beyond _id) is
        an error in MongoDB. Modelling this matters: an earlier version of this fake
        returned the whole document whichever form was used, which hid a real bug where
        a search projection returned neither title nor url.
        """
        if not projection:
            return dict(doc)

        included = [
            field
            for field, spec in projection.items()
            if field != "_id" and spec is True
        ]
        meta = [field for field, spec in projection.items() if isinstance(spec, dict)]

        if included:
            out = {field: doc.get(field) for field in included if field in doc}
            if projection.get("_id", True) and "_id" in doc:
                out["_id"] = doc["_id"]
            return out

        out = dict(doc)
        for field, spec in projection.items():
            if field in meta:
                continue
            if not spec:
                out.pop(field, None)
        return out

    def find(self, query: dict | None = None, projection: dict | None = None):
        query = query or {}
        matched = [d for d in self.docs if self._matches(d, query)]
        projected = []
        for doc in matched:
            out = self._project(doc, projection)
            # A projection may ask for the text score as a meta field; only a text
            # query can supply one.
            for field, spec in (projection or {}).items():
                if isinstance(spec, dict) and "$meta" in spec and "$text" in query:
                    out[field] = self._text_score(doc, query["$text"]["$search"])
            projected.append(out)
        return FakeCursor(projected)

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
        for field in update.get("$unset", {}):
            doc.pop(field, None)
        return doc != before

    def _bulk_op_kind(self, op) -> str:
        """Which write a bulk operation is, by class name.

        pymongo's operation objects expose no common interface worth matching on, and
        the alternative — importing each class to isinstance against — couples the fake
        to pymongo's module layout for no gain.
        """
        return type(op).__name__

    def replace_one(self, query: dict, document: dict, upsert: bool = False):
        """Whole-document replacement, as a run record uses.

        The stored `_id` is preserved: MongoDB keeps it across a replace, and a fake
        that dropped it would let a caller pass while identity changed under it.
        """
        for index, doc in enumerate(self.docs):
            if self._matches(doc, query):
                self.docs[index] = {"_id": doc.get("_id"), **document}
                return FakeUpdateResult(1, 1)
        if not upsert:
            return FakeUpdateResult(0, 0)
        self.docs.append({"_id": ObjectId(), **document})
        return FakeUpdateResult(0, 0)

    def update_many(self, query: dict, update: dict) -> "FakeUpdateResult":
        """Every match updated, with the count of those that actually changed.

        `modified_count` is what a caller reports to an operator, and MongoDB counts
        only documents the update altered — a cleanup run twice must say it changed
        nothing the second time rather than re-reporting every document it matched.
        """
        matched = modified = 0
        for doc in self.docs:
            if self._matches(doc, query):
                matched += 1
                modified += int(self._apply_update(doc, update, inserting=False))
        return FakeUpdateResult(matched, modified)

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
        doc = {"_id": ObjectId(), **seed}
        self._apply_update(doc, update, inserting=True)
        self.docs.append(doc)
        return doc["_id"]

    # `_id` is an ObjectId, as MongoDB assigns — not a counter. A fake handing out
    # integers let lookup-by-ObjectId pass its tests while failing against a real
    # collection, because str(1) is not a parseable ObjectId.
    def insert_many(self, docs: list[dict]) -> list[Any]:
        ids = []
        for doc in docs:
            stored = {"_id": ObjectId(), **doc}
            self.docs.append(stored)
            ids.append(stored["_id"])
        return ids

    def delete_many(self, query: dict) -> "FakeDeleteResult":
        keep = [d for d in self.docs if not self._matches(d, query)]
        removed = len(self.docs) - len(keep)
        self.docs = keep
        return FakeDeleteResult(removed)

    def delete_one(self, query: dict) -> "FakeDeleteResult":
        for index, doc in enumerate(self.docs):
            if self._matches(doc, query):
                del self.docs[index]
                return FakeDeleteResult(1)
        return FakeDeleteResult(0)

    def bulk_write(self, operations, ordered: bool = True) -> FakeBulkResult:
        """UpdateOne, DeleteMany and InsertOne, which is what this program mixes.

        Replacing a page's chunks is a DeleteMany followed by InsertOnes in one write,
        so a page is never briefly chunkless; a fake that only understood UpdateOne
        would let that pass while storing nothing.
        """
        matched = modified = 0
        upserted: dict[int, Any] = {}
        for index, operation in enumerate(operations):
            kind = self._bulk_op_kind(operation)
            if kind == "DeleteMany":
                self.delete_many(_bulk_filter(operation))
                continue
            if kind == "DeleteOne":
                self.delete_one(_bulk_filter(operation))
                continue
            if kind == "InsertOne":
                self.insert_many([_bulk_document(operation)])
                continue
            query, update, upsert = _unpack_update_one(operation)
            existing = next((d for d in self.docs if self._matches(d, query)), None)
            if existing is not None:
                matched += 1
                modified += int(self._apply_update(existing, update, inserting=False))
            elif upsert:
                upserted[index] = self._insert(query, update)
        return FakeBulkResult(matched, modified, upserted)


def _bulk_filter(operation) -> dict:
    """The filter of a bulk delete, whatever pymongo calls the attribute this release."""
    for name in ("_filter", "filter"):
        found = getattr(operation, name, None)
        if found is not None:
            return found
    raise NotImplementedError(f"cannot read the filter of {operation!r}")


def _bulk_document(operation) -> dict:
    """The document of a bulk insert, whatever pymongo calls the attribute."""
    for name in ("_doc", "document"):
        found = getattr(operation, name, None)
        if found is not None:
            return found
    raise NotImplementedError(f"cannot read the document of {operation!r}")


def _project_search_result(doc: dict, projection: dict | None, score: float) -> dict:
    """Apply a $vectorSearch pipeline's $project stage, including the score meta field."""
    if not projection:
        return {**doc, "score": score}
    item: dict[str, Any] = {}
    for field, spec in projection.items():
        if isinstance(spec, dict) and "$meta" in spec:
            item[field] = score
        elif spec:
            item[field] = doc.get(field)
    return item


def _dotted(doc: dict, path: str) -> Any:
    """Resolve a dotted field path, as MongoDB does in an aggregation expression.

    Summing `$cost.dollars` over run records needs this. A fake that only looked up
    top-level keys would total zero and the caller would pass while reporting every
    run as free.
    """
    current: Any = doc
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _tokens(text: str) -> set[str]:
    """Words, without punctuation: "$lookup" and "lookup" are the same term.

    An embedding model does not see the punctuation as a difference, so a stand-in
    that did would fail searches the real index answers.
    """
    return {word for word in re.split(r"[^a-z0-9$]+", text.lower()) if word.strip("$")}


def _word_overlap(left: str, right: str) -> float:
    """Jaccard overlap, standing in for cosine similarity over embeddings."""
    a, b = {t.strip("$") for t in _tokens(left)}, {t.strip("$") for t in _tokens(right)}
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
    def __init__(
        self, parsed_output, stop_reason: str = "end_turn", stop_details=None, usage=None
    ):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        # Token counts, as every real response carries. Left None unless a test sets
        # FakeMessages.usage, so a test that does not care about cost stays unaffected
        # — and a run priced at nothing is then visibly nothing, not a silent zero.
        self.usage = usage


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
        # Set by a test that asserts on run cost; attached to every parse() response.
        self.usage = None

    def stream(self, **kwargs) -> FakeStream:
        self.stream_calls.append(kwargs)
        if not self._stream_messages:
            raise AssertionError("stream() called more times than the test scripted")
        return FakeStream(self._stream_messages.pop(0))

    def parse(self, **kwargs) -> FakeParsedResponse:
        self.parse_calls.append(kwargs)
        scripted = self._parsed_by_format.get(kwargs.get("output_format"), self._parsed)
        if isinstance(scripted, FakeParsedResponse):
            if scripted.usage is None:
                scripted.usage = self.usage
            return scripted
        return FakeParsedResponse(scripted, usage=self.usage)


class FakeAnthropic:
    def __init__(
        self,
        stream_messages: list[FakeMessage] | None = None,
        parsed=None,
        parsed_by_format=None,
    ):
        self.messages = FakeMessages(stream_messages or [], parsed, parsed_by_format)
