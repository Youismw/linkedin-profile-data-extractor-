"""Parser turning raw HTML fetch results into structured ProfileDocument contracts."""

from collections.abc import Sequence

from profile_extractor.models import (
    Meta,
    OtherSection,
    Profile,
    ProfileDocument,
    Status,
)


def parse_profile(
    source_url: str,
    slug: str,
    html: str | None,
    status: Status,
    important_fields: Sequence[str],
    reason: str | None = None,
) -> ProfileDocument:
    """Parse profile HTML into a validated ProfileDocument.

    In Phase 4, acts as a valid contract stub. In Phase 6, full DOM extraction is plugged in.
    """
    if status == Status.UNREACHABLE:
        from profile_extractor.models import Reason

        reason_enum = Reason(reason) if reason else None
        return ProfileDocument(
            meta=Meta(
                source_url=source_url,
                slug=slug,
                status=Status.UNREACHABLE,
                reason=reason_enum,
                missing_fields=[],
            ),
            profile=Profile(),
            other_sections=[],
        )

    # Minimal stub population for Status.OK
    # Fills standard fields from simple heuristic or fallback for walking skeleton
    name = slug.replace("-", " ").title()
    headline = "Professional on LinkedIn"

    profile = Profile(
        name=name,
        headline=headline,
        location="Earth",
        about="Professional profile summary.",
        experience=[],
        education=[],
        skills=[],
    )

    # Determine missing fields based on important_fields configuration
    missing_fields: list[str] = []
    profile_dict = profile.model_dump(exclude_none=True)
    for field in important_fields:
        val = profile_dict.get(field)
        if val is None or (isinstance(val, list) and len(val) == 0 and field in ("skills",)):
            missing_fields.append(field)

    return ProfileDocument(
        meta=Meta(
            source_url=source_url,
            slug=slug,
            status=Status.OK,
            reason=None,
            missing_fields=missing_fields,
        ),
        profile=profile,
        other_sections=[OtherSection(heading="Interests", text="Technology")],
    )

