"""Skill badge shapes.

`DiscoveredBadge` is what Claude returns; `SkillBadgeDoc` is what we store.
Stored documents keep `categories` as an array so questions can be joined and
filtered on the same field name.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DiscoveredBadge(BaseModel):
    slug: str = Field(description="Stable kebab-case identifier, e.g. 'atlas-search'")
    name: str = Field(description="Official badge name as MongoDB publishes it")
    description: str = Field(description="One or two sentences on what the badge covers")
    categories: list[str] = Field(
        default_factory=list,
        description="Topic areas the badge exercises, e.g. ['aggregation', 'indexing']",
    )
    source_urls: list[str] = Field(
        default_factory=list, description="URLs supporting this badge's existence"
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="How well sourced this badge is. Low means treat as a candidate."
    )
    credly_url: str | None = Field(
        default=None, description="Canonical Credly badge page for this badge"
    )
    mongodb_url: str | None = Field(
        default=None,
        description="Canonical learn.mongodb.com page the badge is earned from",
    )
    image_url: str | None = Field(
        default=None, description="Badge artwork, whose printed title is authoritative"
    )
    image_title: str | None = Field(
        default=None,
        description="Title read from the badge artwork. Authoritative over the text "
        "titles on Credly and learn.mongodb.com, which disagree with each other.",
    )
    text_title: str | None = Field(
        default=None, description="Title as written in the source catalog listing"
    )


class DiscoveredBadges(BaseModel):
    badges: list[DiscoveredBadge]


class SkillBadgeDoc(DiscoveredBadge):
    discovered_at: datetime
    discovery_run_id: str
    status: Literal["candidate", "approved", "retired"] = "candidate"
