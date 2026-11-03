"""Data contracts and Pydantic models for LinkedIn Profile Extractor."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Status(StrEnum):
    """Profile retrieval / processing status."""

    OK = "ok"
    UNREACHABLE = "unreachable"


class Reason(StrEnum):
    """Reason for profile being unreachable."""

    INVALID_URL = "invalid_url"
    NOT_FOUND = "not_found"
    NOT_VISIBLE = "not_visible"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    PARSE_FAILED = "parse_failed"
    UNEXPECTED_ERROR = "unexpected_error"


class ExperienceItem(BaseModel):
    """Structured representation of a single work experience entry."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    company: str | None = None
    dates: str | None = None
    location: str | None = None
    description: str | None = None
    raw: str = ""


class EducationItem(BaseModel):
    """Structured representation of a single education entry."""

    model_config = ConfigDict(extra="ignore")

    school: str | None = None
    degree: str | None = None
    dates: str | None = None
    raw: str = ""


class OtherSection(BaseModel):
    """An unmapped or additional section captured from the page."""

    model_config = ConfigDict(extra="ignore")

    heading: str
    text: str


class Profile(BaseModel):
    """Structured important fields of a LinkedIn profile."""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    headline: str | None = None
    location: str | None = None
    about: str | None = None
    experience: list[ExperienceItem] | None = None
    education: list[EducationItem] | None = None
    skills: list[str] | None = None


class Meta(BaseModel):
    """Metadata describing the profile extraction."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = "1.0"
    source_url: str
    slug: str
    status: Status
    reason: Reason | None = None
    fetched_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
    missing_fields: list[str] = Field(default_factory=list)


class ProfileDocument(BaseModel):
    """Complete output document written to JSON for each profile."""

    model_config = ConfigDict(extra="ignore")

    meta: Meta
    profile: Profile = Field(default_factory=Profile)
    other_sections: list[OtherSection] = Field(default_factory=list)

    def to_serializable_dict(self) -> dict[str, Any]:
        """Convert document to a dictionary adhering strictly to the contract.

        Omits missing important fields (None values) from the profile object.
        For unreachable profiles, profile is an empty dict and other_sections is an empty list.
        """
        if self.meta.status == Status.UNREACHABLE:
            return {
                "meta": self.meta.model_dump(),
                "profile": {},
                "other_sections": [],
            }

        profile_data = self.profile.model_dump(exclude_none=True)
        return {
            "meta": self.meta.model_dump(),
            "profile": profile_data,
            "other_sections": [section.model_dump() for section in self.other_sections],
        }


class FetchResult(BaseModel):
    """Outcome and raw payload returned by a Fetcher implementation."""

    model_config = ConfigDict(extra="ignore")

    url: str
    status: Status
    reason: Reason | None = None
    html: str | None = None
    fetched_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
    detail: str | None = None
    extra_pages: dict[str, str] = Field(default_factory=dict)


class UnreachableRecord(BaseModel):
    """Record written to unreachable_profiles.xlsx."""

    input_row: int
    url_as_given: str
    slug: str | None = None
    reason: Reason
    detail: str | None = None
    timestamp_utc: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )


class RunSummary(BaseModel):
    """Summary statistics written to run_summary.json at the end of a run."""

    model_config = ConfigDict(extra="ignore")

    started_at: str
    finished_at: str
    duration_seconds: float
    rows_read: int
    rows_valid: int
    rows_invalid: int
    duplicates_skipped: int
    ok_count: int
    unreachable_count: int
    unreachable_by_reason: dict[str, int]
    not_processed_count: int
    output_dir: str
    tool_version: str = "0.1.0"
