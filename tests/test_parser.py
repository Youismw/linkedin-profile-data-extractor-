"""Tests for Phase 6: HTML profile parsing, rules, coverage, and fixtures."""

from pathlib import Path

from profile_extractor.models import Reason, Status
from profile_extractor.parser import parse_profile

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DEFAULT_IMPORTANT_FIELDS = [
    "name",
    "headline",
    "location",
    "about",
    "experience",
    "education",
    "skills",
]


class TestParser:
    """Test suite for DOM parsing, section routing, and missing fields detection."""

    def test_full_profile_fixture(self) -> None:
        """Verify complete extraction from full_profile.html including Unicode characters."""
        html = (FIXTURES_DIR / "full_profile.html").read_text(encoding="utf-8")
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/renee-bjork-muller/",
            slug="renee-bjork-muller",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        assert doc.meta.status == Status.OK
        assert doc.meta.reason is None
        assert doc.meta.missing_fields == []

        # Name with Unicode
        assert doc.profile.name == "Renée Björk-Müller"
        assert "Principal Software Architect" in (doc.profile.headline or "")
        assert "Munich" in (doc.profile.location or "")

        # About
        assert doc.profile.about is not None
        assert "distributed systems" in doc.profile.about.lower()

        # Experience
        assert doc.profile.experience is not None
        assert len(doc.profile.experience) == 2
        exp1 = doc.profile.experience[0]
        assert exp1.title == "Principal Architect"
        assert exp1.company == "Global Tech AG"
        assert exp1.raw != ""
        assert "Global Tech AG" in exp1.raw

        # Education
        assert doc.profile.education is not None
        assert len(doc.profile.education) == 1
        edu = doc.profile.education[0]
        assert edu.school == "Technical University of Munich"
        assert edu.degree == "M.S. in Computer Science"
        assert edu.raw != ""

        # Skills
        assert doc.profile.skills is not None
        assert "Distributed Systems" in doc.profile.skills
        assert "Python" in doc.profile.skills

        # Raw text dump
        assert doc.raw_text is not None
        assert "Renée Björk-Müller" in doc.raw_text
        assert "raw_text" in doc.to_serializable_dict()

    def test_sparse_profile_fixture(self) -> None:
        """Verify sparse profile accurately computes missing_fields and contract serialization."""
        html = (FIXTURES_DIR / "sparse_profile.html").read_text(encoding="utf-8")
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/john-minimal/",
            slug="john-minimal",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        assert doc.meta.status == Status.OK
        assert doc.profile.name == "John Minimal"
        assert doc.profile.headline == "Independent IT Consultant"

        # Missing fields check
        missing = set(doc.meta.missing_fields)
        expected_missing = {"location", "about", "experience", "education", "skills"}
        assert expected_missing.issubset(missing)

        # Contract assertion: missing fields are omitted from serialized profile dict
        serialized = doc.to_serializable_dict()
        for field in expected_missing:
            assert field not in serialized["profile"]

    def test_unusual_sections_land_in_other_sections(self) -> None:
        """Verify unmapped sections land cleanly in other_sections without data loss."""
        html = (FIXTURES_DIR / "unusual_section_profile.html").read_text(encoding="utf-8")
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/alice-wonderland/",
            slug="alice-wonderland",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        assert doc.meta.status == Status.OK
        assert doc.profile.name == "Alice Wonderland"
        assert len(doc.other_sections) >= 3

        headings = [sec.heading.lower() for sec in doc.other_sections]
        assert any("volunteer" in h for h in headings)
        assert any("patent" in h or "publication" in h for h in headings)
        assert any("languages" in h for h in headings)

        # Check content preserved
        volunteer_sec = next(s for s in doc.other_sections if "volunteer" in s.heading.lower())
        assert "Girls Who Code" in volunteer_sec.text

    def test_idempotent_parsing(self) -> None:
        """Verify identical HTML produces identical output apart from fetched_at timestamp."""
        html = (FIXTURES_DIR / "full_profile.html").read_text(encoding="utf-8")

        doc1 = parse_profile(
            source_url="https://www.linkedin.com/in/renee-bjork-muller/",
            slug="renee-bjork-muller",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )
        doc2 = parse_profile(
            source_url="https://www.linkedin.com/in/renee-bjork-muller/",
            slug="renee-bjork-muller",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        dict1 = doc1.to_serializable_dict()
        dict2 = doc2.to_serializable_dict()

        # Apart from fetched_at, all keys and values must match exactly
        dict1["meta"]["fetched_at"] = "FIXED"
        dict2["meta"]["fetched_at"] = "FIXED"
        assert dict1 == dict2

    def test_parse_failed_when_no_name_and_headline(self) -> None:
        """Verify parse_failed is returned when neither name nor headline can be found."""
        blank_html = "<html><body><div>Just some random text without headers</div></body></html>"
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/empty-user/",
            slug="empty-user",
            html=blank_html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        assert doc.meta.status == Status.UNREACHABLE
        assert doc.meta.reason == Reason.PARSE_FAILED
        assert doc.profile.name is None
        assert doc.to_serializable_dict()["profile"] == {}

    def test_title_fallback_rejects_generic_titles(self) -> None:
        """Verify generic titles like 'Sign Up | LinkedIn' or 'Log In' are rejected."""
        signup_html = (
            "<html><head><title>Sign Up | LinkedIn</title></head>"
            "<body><div>Form</div></body></html>"
        )
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/blocked-user/",
            slug="blocked-user",
            html=signup_html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )
        assert doc.meta.status == Status.UNREACHABLE
        assert doc.meta.reason == Reason.PARSE_FAILED
        assert doc.profile.name != "Sign Up"

    def test_headline_and_location_word_boundaries(self) -> None:
        """Verify substrings like 'connect' or 'uk' do not corrupt headline or location."""
        html = """
        <html>
        <head><title>John Mukherjee | LinkedIn</title></head>
        <body>
            <main>
                <section>
                    <h1>John Mukherjee</h1>
                    <div>Network Connectivity Engineer</div>
                    <div>San Francisco, California, United States</div>
                    <div>500+ connections</div>
                </section>
            </main>
        </body>
        </html>
        """
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/john-mukherjee/",
            slug="john-mukherjee",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )
        assert doc.meta.status == Status.OK
        assert doc.profile.name == "John Mukherjee"
        assert doc.profile.headline == "Network Connectivity Engineer"
        assert doc.profile.location == "San Francisco, California, United States"

    def test_aside_strip_prevents_sidebar_leakage(self) -> None:
        """Verify <aside> tags and 'People also viewed' sections do not pollute raw_text."""
        html = """
        <html>
        <body>
            <main>
                <section>
                    <h1>Target Candidate</h1>
                    <div>Senior Python Engineer</div>
                </section>
                <aside>
                    <h2>People also viewed</h2>
                    <div>Unrelated Person - Chief Clown</div>
                </aside>
            </main>
        </body>
        </html>
        """
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/target-candidate/",
            slug="target-candidate",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )
        assert doc.meta.status == Status.OK
        assert doc.profile.name == "Target Candidate"
        assert "Unrelated Person" not in (doc.raw_text or "")
        assert "Chief Clown" not in (doc.raw_text or "")

    def test_consecutive_duplicate_lines_deduped(self) -> None:
        """Verify LinkedIn's duplicated visually-hidden spans don't shift experience fields."""
        html = """
        <html>
        <body>
            <main>
                <section>
                    <h1>Jane Doe</h1>
                    <div>Software Engineer</div>
                </section>
                <section id="experience">
                    <h2>Experience</h2>
                    <ul>
                        <li>
                            <div>Staff Engineer</div>
                            <div>Staff Engineer</div>
                            <div>Acme Corp</div>
                            <div>Acme Corp</div>
                            <div>Jan 2020 - Present · 4 yrs</div>
                            <div>Jan 2020 - Present · 4 yrs</div>
                        </li>
                    </ul>
                </section>
            </main>
        </body>
        </html>
        """
        doc = parse_profile(
            source_url="https://www.linkedin.com/in/jane-doe/",
            slug="jane-doe",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )
        assert doc.meta.status == Status.OK
        assert doc.profile.experience is not None
        exp = doc.profile.experience[0]
        assert exp.title == "Staff Engineer"
        assert exp.company == "Acme Corp"
        assert "Jan 2020" in (exp.dates or "")

    def test_parse_contact_info_modal(self) -> None:
        """Verify contact overlay modal parsing with all attributes."""
        from profile_extractor.parser import parse_contact_info

        modal_html = """
        <div class="artdeco-modal" role="dialog" aria-labelledby="pv-contact-info">
          <div class="artdeco-modal__header">
            <h1 id="pv-contact-info">Rohit Chauhan</h1>
          </div>
          <div class="artdeco-modal__content">
            <section class="pv-contact-info__contact-type ci-vanity-url">
              <h3 class="pv-contact-info__header">Your Profile</h3>
              <div class="pv-contact-info__ci-container">
                <a class="pv-contact-info__contact-link" href="https://www.linkedin.com/in/rohit-chauhan-464b15262">
                  linkedin.com/in/rohit-chauhan-464b15262
                </a>
              </div>
            </section>
            <section class="pv-contact-info__contact-type ci-email">
              <h3 class="pv-contact-info__header">Email</h3>
              <div class="pv-contact-info__ci-container">
                <a class="pv-contact-info__contact-link" href="mailto:rohit@example.com">
                  rohit@example.com
                </a>
              </div>
            </section>
            <section class="pv-contact-info__contact-type ci-phone">
              <h3 class="pv-contact-info__header">Phone</h3>
              <div class="pv-contact-info__ci-container">
                <span class="t-14 t-black t-normal">+91 9876543210</span>
                <span class="t-14 t-black--light t-normal"> (Mobile)</span>
              </div>
            </section>
            <section class="pv-contact-info__contact-type ci-websites">
              <h3 class="pv-contact-info__header">Websites</h3>
              <div class="pv-contact-info__ci-container">
                <ul>
                  <li>
                    <a href="https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fgithub%2Ecom%2Frohit&amp;urlhash=1234">
                      github.com/rohit
                    </a>
                  </li>
                  <li>
                    <a href="https://myportfolio.dev">Portfolio</a>
                  </li>
                </ul>
              </div>
            </section>
            <section class="pv-contact-info__contact-type ci-address">
              <h3 class="pv-contact-info__header">Address</h3>
              <div class="pv-contact-info__ci-container">
                <span>Noida, Uttar Pradesh, India</span>
              </div>
            </section>
            <section class="pv-contact-info__contact-type ci-birthday">
              <h3 class="pv-contact-info__header">Birthday</h3>
              <div class="pv-contact-info__ci-container">
                <span>January 15</span>
              </div>
            </section>
            <section class="pv-contact-info__contact-type ci-connected">
              <h3 class="pv-contact-info__header">Connected</h3>
              <div class="pv-contact-info__ci-container">
                <span>November 4, 2026</span>
              </div>
            </section>
            <section class="pv-contact-info__contact-type ci-twitter">
              <h3 class="pv-contact-info__header">Twitter</h3>
              <div class="pv-contact-info__ci-container">
                <a href="https://twitter.com/rohit_dev">@rohit_dev</a>
              </div>
            </section>
          </div>
        </div>
        """
        ci = parse_contact_info(modal_html)
        assert ci is not None
        assert ci.email == "rohit@example.com"
        assert "+91 9876543210" in (ci.phone or "")
        assert ci.profile_url == "https://www.linkedin.com/in/rohit-chauhan-464b15262"
        assert ci.address == "Noida, Uttar Pradesh, India"
        assert ci.birthday == "January 15"
        assert ci.connected == "November 4, 2026"
        assert ci.twitter == "https://twitter.com/rohit_dev"
        assert ci.websites == ["https://github.com/rohit", "https://myportfolio.dev"]
        assert "Contact Info" in ci.raw or "Rohit Chauhan" in ci.raw

    def test_parse_contact_info_empty_and_fallback(self) -> None:
        """Verify empty HTML returns None, and raw fallback detects emails/phone."""
        from profile_extractor.parser import parse_contact_info

        assert parse_contact_info("") is None
        assert parse_contact_info("<html><body><div>Just some content</div></body></html>") is None

        fallback_html = """
        <div role="dialog">
            <h2>Contact Info</h2>
            <p>Reach me at contact@company.org or tel:+123456789</p>
            <a href="mailto:contact@company.org">Email</a>
            <a href="tel:+123456789">Call</a>
        </div>
        """
        ci = parse_contact_info(fallback_html)
        assert ci is not None
        assert ci.email == "contact@company.org"
        assert ci.phone == "+123456789"

    def test_parse_contact_info_sdui_modal(self) -> None:
        """Verify modern LinkedIn SDUI contact overlay modal parsing."""
        from profile_extractor.parser import parse_contact_info

        sdui_html = """
        <div class="f49ayy f49iun f49ivr f49l40">
            <header id="dialog-header">
                <h2>Contact info</h2>
            </header>
            <div data-testid="dialog-content">
                <div data-sdui-screen="com.linkedin.sdui.flagshipnav.profile.ProfileContactDetailsOverlay">
                    <div id="com.linkedin.sdui.profile.card.refACoAAFuiOaABNrD5PImIzCtko6mrvf_EuS1dK2MContactInfoDetailSection">
                        <h3>Gaurav's profile</h3>
                        <a href="https://www.linkedin.com/in/gaurav-akoliya-aa8a3636a/">
                            linkedin.com/in/gaurav-akoliya-aa8a3636a
                        </a>
                        <h3>Email</h3>
                        <a href="mailto:gaurav@example.com">gaurav@example.com</a>
                        <h3>Phone</h3>
                        <a href="tel:+919876543210">+91 9876543210</a>
                        <h3>Website</h3>
                        <a href="https://gauravakoliya.dev">Portfolio</a>
                        <a href="https://www.linkedin.com/premium/products/?upsell=1">Premium</a>
                    </div>
                </div>
            </div>
        </div>
        """
        ci = parse_contact_info(sdui_html)
        assert ci is not None
        assert ci.profile_url == "https://www.linkedin.com/in/gaurav-akoliya-aa8a3636a/"
        assert ci.email == "gaurav@example.com"
        assert ci.phone == "+919876543210"
        assert ci.websites == ["https://gauravakoliya.dev"]
        # Make sure internal linkedin.com upsell link is excluded
        assert not any("linkedin.com" in w for w in (ci.websites or []))

    def test_parse_contact_info_ignores_search_autocomplete(self) -> None:
        """Verify top-nav search autocomplete popups are strictly excluded."""
        from profile_extractor.parser import parse_contact_info

        autocomplete_html = """
        <div role="dialog" id="global-nav-typeahead">
            <div data-testid="typeahead-input">
                <input placeholder="Search" />
            </div>
            <div>
                <h3>Recent entity history</h3>
                <a href="https://www.linkedin.com/in/other-person/">Other Person</a>
            </div>
        </div>
        """
        ci = parse_contact_info(autocomplete_html)
        assert ci is None

    def test_parse_profile_merges_multiple_matching_sections_without_overwriting(self) -> None:
        """Duplicate matching sections in composed HTML merge additively without overwriting."""
        html = """
        <html>
          <body>
            <main>
              <section id="top-card">
                <h1>Jane Doe</h1>
                <p>Staff Software Engineer</p>
              </section>

              <section id="exp-card-1">
                <h2>Experience</h2>
                <ul>
                  <li>
                    <span>Senior Engineer</span>
                    <span>Acme Corp</span>
                    <span>2021 - Present</span>
                  </li>
                  <li>
                    <span>Software Engineer</span>
                    <span>Beta Systems</span>
                    <span>2019 - 2021</span>
                  </li>
                </ul>
              </section>
              <section id="exp-card-2">
                <h2>Experience</h2>
                <ul>
                  <li>
                    <span>Software Engineer</span>
                    <span>Beta Systems</span>
                    <span>2019 - 2021</span>
                    <span>Led migration of core billing services to event-driven workers.</span>
                  </li>
                  <li>
                    <span>Junior Developer</span>
                    <span>Gamma Labs</span>
                    <span>2017 - 2019</span>
                  </li>
                </ul>
              </section>

              <section id="edu-card-1">
                <h2>Education</h2>
                <ul>
                  <li>
                    <span>State University</span>
                    <span>M.S. Computer Science</span>
                    <span>2015 - 2017</span>
                  </li>
                </ul>
              </section>
              <section id="edu-card-2">
                <h2>Education</h2>
                <ul>
                  <li>
                    <span>State University</span>
                    <span>M.S. Computer Science</span>
                    <span>2015 - 2017</span>
                  </li>
                  <li>
                    <span>City College</span>
                    <span>B.S. Computer Science</span>
                    <span>2011 - 2015</span>
                  </li>
                </ul>
              </section>

              <section id="skills-card-1">
                <h2>Skills</h2>
                <ul>
                  <li><span>Python</span></li>
                  <li><span>Playwright</span></li>
                </ul>
              </section>
              <section id="skills-card-2">
                <h2>Skills</h2>
                <ul>
                  <li><span>playwright</span></li>
                  <li><span>TypeScript</span></li>
                </ul>
              </section>

              <section id="cert-card-1">
                <h2>Licenses &amp; certifications</h2>
                <ul>
                  <li>
                    <span>AWS Certified Solutions Architect</span>
                    <span>Amazon Web Services</span>
                    <span>Credential ID AWS-123</span>
                  </li>
                </ul>
              </section>
              <section id="cert-card-2">
                <h2>Licenses &amp; certifications</h2>
                <ul>
                  <li>
                    <span>AWS Certified Solutions Architect</span>
                    <span>Amazon Web Services</span>
                    <span>Credential ID AWS-123</span>
                  </li>
                  <li>
                    <span>CKAD</span>
                    <span>CNCF</span>
                    <span>Credential ID CNCF-456</span>
                  </li>
                </ul>
              </section>

              <section id="proj-card-1">
                <h2>Projects</h2>
                <ul>
                  <li>
                    <span>Open Scraper</span>
                    <span>2024 - 2025</span>
                  </li>
                </ul>
              </section>
              <section id="proj-card-2">
                <h2>Projects</h2>
                <ul>
                  <li>
                    <span>Open Scraper</span>
                    <span>2024 - 2025</span>
                    <span>Built resilient HTML parser.</span>
                  </li>
                  <li>
                    <span>Data Pipeline</span>
                    <span>2022 - 2023</span>
                  </li>
                </ul>
              </section>

              <section id="lang-card-1">
                <h2>Languages</h2>
                <ul>
                  <li>
                    <span>English</span>
                    <span>Native or bilingual proficiency</span>
                  </li>
                </ul>
              </section>
              <section id="lang-card-2">
                <h2>Languages</h2>
                <ul>
                  <li>
                    <span>English</span>
                    <span>Native or bilingual proficiency</span>
                  </li>
                  <li>
                    <span>Hindi</span>
                    <span>Full professional proficiency</span>
                  </li>
                </ul>
              </section>
            </main>
          </body>
        </html>
        """

        doc = parse_profile(
            source_url="https://www.linkedin.com/in/jane-doe/",
            slug="jane-doe",
            html=html,
            status=Status.OK,
            important_fields=DEFAULT_IMPORTANT_FIELDS,
        )

        p = doc.profile
        assert p.experience is not None
        assert [item.title for item in p.experience] == [
            "Senior Engineer",
            "Software Engineer",
            "Junior Developer",
        ]
        beta_item = p.experience[1]
        assert beta_item.description is not None and "billing services" in beta_item.description

        assert p.education is not None
        assert [item.school for item in p.education] == ["State University", "City College"]

        assert p.skills == ["Python", "Playwright", "TypeScript"]

        assert p.certifications is not None
        assert [item.name for item in p.certifications] == [
            "AWS Certified Solutions Architect",
            "CKAD",
        ]

        assert p.projects is not None
        assert [item.title for item in p.projects] == ["Open Scraper", "Data Pipeline"]
        assert p.projects[0].description is not None and "HTML parser" in p.projects[0].description

        assert p.languages is not None
        assert [item.name for item in p.languages] == ["English", "Hindi"]
