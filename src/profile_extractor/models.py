"""Data contracts and Pydantic models for LinkedIn Profile Extractor.

This module acts as the single source of truth for all data structures across
the application. By defining explicit Pydantic models, we guarantee strict type
validation, automated JSON serialization, and immutable contracts between the
input reader, DOM parser, storage writers, and external consumers.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ==============================================================================
# Status and Error Classifications
# ==============================================================================


class Status(StrEnum):
    """Binary profile retrieval outcome according to project requirements.

    Attributes:
        OK: Profile was successfully retrieved and parsed.
        UNREACHABLE: Profile could not be retrieved, viewed, or parsed.
    """

    OK = "ok"
    UNREACHABLE = "unreachable"


class Reason(StrEnum):
    """Categorized root causes when a profile is unreachable.

    Matches the strict error taxonomy mandated in the project specification:
        INVALID_URL: Malformed link, wrong domain, or syntax error in spreadsheet.
        NOT_FOUND: LinkedIn returned HTTP 404 / 'Profile Not Found'.
        NOT_VISIBLE: Authwall, private profile, or network redirect to login.
        BLOCKED: Bot detection challenge, CAPTCHA, or HTTP 429 / 999 response.
        TIMEOUT: Page load or element rendering exceeded configured deadline.
        PARSE_FAILED: Page loaded but candidate name/headline could not be found.
        UNEXPECTED_ERROR: Unhandled system exception or runtime failure.
    """

    INVALID_URL = "invalid_url"
    NOT_FOUND = "not_found"
    NOT_VISIBLE = "not_visible"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    PARSE_FAILED = "parse_failed"
    UNEXPECTED_ERROR = "unexpected_error"


# ==============================================================================
# Structured Profile Entities
# ==============================================================================


class ExperienceItem(BaseModel):
    """Structured representation of a single professional job position.

    Attributes:
        title: Extracted job title (e.g., 'Senior Software Engineer').
        company: Company name or organization.
        dates: Employment timeframe string (e.g., 'Sep 2023 - Present · 1 yr').
        location: Work location and modality (e.g., 'Noida, India · Hybrid').
        description: Genuine role description, narrative, or achievements.
        skills_preview: Associated skills chip (e.g. 'Node.js, MongoDB and +2 skills').
        raw: Full unparsed text string preserved for audit integrity.
    """

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    company: str | None = None
    dates: str | None = None
    location: str | None = None
    description: str | None = None
    skills_preview: str | None = None
    raw: str = ""


class EducationItem(BaseModel):
    """Structured representation of a single educational qualification.

    Attributes:
        school: College, university, or institution name.
        degree: Degree, diploma, or field of study.
        dates: Enrollment period or graduation year.
        grade: Academic grade, CGPA, or honors (e.g., 'CGPA: 7.18').
        activities: Clubs, societies, and extracurricular activities.
        raw: Full unparsed text string preserved for audit integrity.
    """

    model_config = ConfigDict(extra="ignore")

    school: str | None = None
    degree: str | None = None
    dates: str | None = None
    grade: str | None = None
    activities: str | None = None
    raw: str = ""


class CertificationItem(BaseModel):
    """Structured representation of a professional license or certification.

    Attributes:
        name: Name of the certificate or credential (e.g. 'AWS Solutions Architect').
        issuer: Issuing organization or authority (e.g. 'Amazon Web Services').
        issue_date: Date issued (e.g. 'Issued Sep 2023').
        expiration_date: Expiration date or 'No Expiration Date'.
        credential_id: License / Credential identification code.
        credential_url: Link to verify the certificate online.
        raw: Full unparsed text string preserved for audit integrity.
    """

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    issuer: str | None = None
    issue_date: str | None = None
    expiration_date: str | None = None
    credential_id: str | None = None
    credential_url: str | None = None
    raw: str = ""


class ProjectItem(BaseModel):
    """Structured representation of an academic, personal, or professional project.

    Attributes:
        title: Project title or name.
        dates: Project timeline or completion date.
        description: Narrative description of the project and implementation.
        url: Link to project repository or live deployment.
        skills: Associated skills and technologies used.
        raw: Full unparsed text string preserved for audit integrity.
    """

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    dates: str | None = None
    description: str | None = None
    url: str | None = None
    skills: list[str] | None = None
    raw: str = ""


class LanguageItem(BaseModel):
    """Structured representation of a spoken/written language proficiency.

    Attributes:
        name: Language name (e.g. 'English', 'German').
        proficiency: Proficiency level (e.g. 'Full professional proficiency').
        raw: Full unparsed text string preserved for audit integrity.
    """

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    proficiency: str | None = None
    raw: str = ""


class OtherSection(BaseModel):
    """Unmapped or miscellaneous profile section captured from the DOM.

    Captures auxiliary sections like 'Volunteering', 'Patents', 'Publications',
    or 'Activity' without data loss.
    """

    model_config = ConfigDict(extra="ignore")

    heading: str
    text: str


class Profile(BaseModel):
    """Core professional attributes extracted from a candidate's profile.

    Fields are Optional to support candidates with sparse or partial profiles.
    Missing fields are tracked separately in the Meta block.
    """

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    headline: str | None = None
    location: str | None = None
    about: str | None = None
    experience: list[ExperienceItem] | None = None
    education: list[EducationItem] | None = None
    skills: list[str] | None = None
    certifications: list[CertificationItem] | None = None
    projects: list[ProjectItem] | None = None
    languages: list[LanguageItem] | None = None


# ==============================================================================
# Document Metadata & Root Document Schema
# ==============================================================================


class Meta(BaseModel):
    """Metadata block recording extraction provenance and quality metrics.

    Attributes:
        schema_version: Contract specification version ('1.0').
        source_url: Canonical LinkedIn URL that was requested.
        slug: Normalized unique handle extracted from the profile URL.
        status: Execution status ('ok' or 'unreachable').
        reason: Specific reason enum if unreachable, else None.
        fetched_at: ISO 8601 UTC timestamp of when data was pulled.
        missing_fields: List of expected core fields that had no data in DOM.
    """

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
    """Root JSON document persisted to disk for every visited profile.

    Adheres strictly to the JSON schema contract:
    - Meta block contains provenance and missing fields array.
    - Profile block contains non-null structured core fields.
    - Other sections contains all remaining content.
    - Raw text contains the unedited text dump of the entire profile.
    """

    model_config = ConfigDict(extra="ignore")

    meta: Meta
    profile: Profile = Field(default_factory=Profile)
    other_sections: list[OtherSection] = Field(default_factory=list)
    raw_text: str | None = None

    def to_serializable_dict(self) -> dict[str, Any]:
        """Convert document to a dictionary adhering strictly to the contract.

        Rule 1: If unreachable, 'profile' is {} and 'other_sections' is [].
        Rule 2: If ok, missing fields (None values) are stripped from 'profile'.
        Rule 3: 'raw_text' is appended at the very end of the JSON object.
        """
        # Unreachable profile serialization
        if self.meta.status == Status.UNREACHABLE:
            res: dict[str, Any] = {
                "meta": self.meta.model_dump(),
                "profile": {},
                "other_sections": [],
            }
            if self.raw_text is not None:
                res["raw_text"] = self.raw_text
            return res

        # Normal profile serialization
        profile_data = self.profile.model_dump(exclude_none=True)
        res = {
            "meta": self.meta.model_dump(),
            "profile": profile_data,
            "other_sections": [section.model_dump() for section in self.other_sections],
        }
        if self.raw_text is not None:
            res["raw_text"] = self.raw_text
        return res


# ==============================================================================
# Fetcher, Failure Report, and Run Summary Contracts
# ==============================================================================


class FetchResult(BaseModel):
    """Payload returned by any Fetcher implementation (Playwright or Fake).

    Decouples raw HTML retrieval from parsing logic, enabling headless browser
    swapping without touching business extraction code.
    """

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
    """Row record formatted for unreachable_profiles.xlsx.

    Allows recruiters to immediately identify which candidates failed,
    on which Excel row number, and the exact diagnosis why.
    """

    input_row: int
    url_as_given: str
    slug: str | None = None
    reason: Reason
    detail: str | None = None
    timestamp_utc: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )


class RunSummary(BaseModel):
    """Mathematical audit reconciliation written to run_summary.json.

    Guarantees full accounting: rows_valid == ok + unreachable + not_processed.
    """

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
