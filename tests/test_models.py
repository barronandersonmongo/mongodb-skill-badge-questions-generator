"""Tests for app/models/skill_badge.py — badge schemas.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models.skill_badge import DiscoveredBadge, DiscoveredBadges, SkillBadgeDoc

MINIMAL = {
    "slug": "atlas-search",
    "name": "Atlas Search",
    "description": "Covers Atlas Search indexes and queries.",
    "confidence": "high",
}


def test_minimal_badge_defaults_both_arrays_to_empty():
    """
    Intent: A badge Claude found with no categories and no cited sources must still
        be storable, with empty arrays rather than null so downstream array queries
        never have to special-case a missing field.
    Success: categories == [] and source_urls == [].
    Feature: Badge schema — safe defaults.
    """
    badge = DiscoveredBadge(**MINIMAL)
    assert badge.categories == []
    assert badge.source_urls == []


@pytest.mark.parametrize("missing", ["slug", "name", "description", "confidence"])
def test_required_fields_are_required(missing):
    """
    Intent: A badge is useless to a quiz author without an identifier, a name, a
        description of scope, and a confidence signal — none may be omitted.
    Success: Omitting any one of those four fields raises ValidationError.
    Feature: Badge schema — required fields.
    """
    payload = {k: v for k, v in MINIMAL.items() if k != missing}
    with pytest.raises(ValidationError):
        DiscoveredBadge(**payload)


def test_confidence_is_constrained():
    """
    Intent: Confidence drives whether a human trusts a badge, so it must be one of
        a known set — free text would make the admin view unfilterable.
    Success: An unrecognized confidence value raises ValidationError.
    Feature: Badge schema — constrained confidence.
    """
    with pytest.raises(ValidationError):
        DiscoveredBadge(**{**MINIMAL, "confidence": "very-sure"})


def test_schema_describes_every_field_for_the_model():
    """
    Intent: In a structured-output call the field descriptions ARE the instructions
        Claude follows. An undescribed field is a silently under-specified
        instruction, so every field must carry a description.
    Success: No property in the generated JSON schema is missing a description.
    Feature: Badge discovery — prompt quality of the extraction schema.
    """
    properties = DiscoveredBadges.model_json_schema()["$defs"]["DiscoveredBadge"][
        "properties"
    ]
    undescribed = [
        name for name, spec in properties.items() if not spec.get("description")
    ]
    assert undescribed == []


def test_stored_doc_requires_provenance_fields():
    """
    Intent: A stored badge must be traceable to the run that produced it, so the
        stored shape cannot be constructed without discovery provenance, and it
        defaults to candidate rather than approved.
    Success: Omitting discovered_at/discovery_run_id raises; a complete doc
        defaults status to "candidate".
    Feature: Badge lifecycle — provenance and default status.
    """
    with pytest.raises(ValidationError):
        SkillBadgeDoc(**MINIMAL)

    doc = SkillBadgeDoc(
        **MINIMAL,
        discovered_at=datetime.now(timezone.utc),
        discovery_run_id="abc123",
    )
    assert doc.status == "candidate"


def test_stored_doc_status_is_constrained():
    """
    Intent: Status is a human review decision that the admin UI switches on, so it
        must be restricted to candidate/approved/retired.
    Success: An unrecognized status raises ValidationError.
    Feature: Badge lifecycle — constrained status.
    """
    with pytest.raises(ValidationError):
        SkillBadgeDoc(
            **MINIMAL,
            discovered_at=datetime.now(timezone.utc),
            discovery_run_id="abc123",
            status="published",
        )
