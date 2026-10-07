"""A2 v2: the private route, the strict codec, the service and the router.

0.6A-1F. Dependency-pure — `FirestoreRepositories` over `FakeDocumentStore`, an
injected identity verifier, a fake rung source, and no SDK anywhere. The REAL
frozen rung table, the real Parent engine and the Firestore emulator are driven
in `pilot_runtime/tests/`.

What is under test is the TRUST BOUNDARY, not the baseline. The Pilot never
recomputes a baseline: it accepts a validated payload from an authenticated
Parent service, resolves every skill's identity against its own canonical
source, verifies Parent's denominators against that same source, resolves the
child itself, and writes once.

## The fake rung source is a CONTRACT stand-in, not a shortcut

It implements exactly the two methods the boundary calls — `canonical_identity`
and `declared_band_roster` — with the same normalisation and the same
fail-closed behaviour on zero or multiple matches. Using it here keeps this file
dependency-pure; `pilot_runtime/tests/test_a2_v2_product_path.py` runs the same
boundary against the real artifact, so neither file can pass on an assumption the
other would contradict.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import pathlib

import pytest

from pilot_backend.domain.canonical_rung import normalize_milestone_text
from pilot_backend.domain.entities import Child
from pilot_backend.domain.parent_baseline_projection import (
    ProjectionIntegrityError,
    ProjectionValidationError,
)
from pilot_backend.domain.parent_baseline_projection_v2 import (
    PROJECTION_SCHEMA_V2,
    ParentBaselineProjectionV2,
    ProjectionV2Error,
    projection_v2_id_for,
)
from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
from pilot_backend.integration.baseline_projection_v2_service import (
    BaselineProjectionV2Service,
    ProjectionV2ChildUnresolved,
    ProjectionV2LinkAmbiguous,
)
from pilot_backend.integration.baseline_skill_projection import (
    SkillCanonicalisationError,
)
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories
from pilot_backend.transport.projection_v2_request import (
    BODY_KEYS,
    MAX_BODY_BYTES,
    MAX_MILESTONE_CHARS,
    MAX_SKILLS,
    ProjectionV2RequestError,
    ProjectionV2RequestTooLarge,
    decode_body,
    read_request,
)
from pilot_backend.transport.projection_v2_wsgi import (
    PROJECTION_V2_ROUTE,
    InternalProjectionRouter,
    ProjectionV2App,
)
from pilot_backend.transport.projection_wsgi import (
    PROJECTION_ROUTE,
    ProjectionApp,
)

PILOT_ROOT = pathlib.Path(__file__).resolve().parents[1]

DOMAIN = "talking_and_communicating"
SESSION = "sess-fictional-v2-a1b2"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64

#: A distinctive fictional milestone used by the logging audit. Unmistakable in
#: a log, and nothing like real taxonomy wording.
PLANTED = "ZZQQ-FICTIONAL-MILESTONE-pelican-waltzes-on-the-abacus"

SUMMARY = {
    "domain": DOMAIN,
    "area_id": "talking",
    "entry_choice_id": "two_three_words",
    "routing_anchor_months": 24,
    "not_demonstrated_months": 30,
    "status": "BOUNDED",
    "baseline_version": "parent-2.4-functional-baseline-v2",
}

#: Four declared 30-month skills plus one at 24, mirroring the real band shape.
#: `pronouns` stands for the canonicalisable-but-not-activity-mappable case.
SKILL_ROWS = [
    {"domain": DOMAIN, "subdomain": "expressive_language", "months": 24,
     "milestone": "says at least two words together", "state": "demonstrated"},
    {"domain": DOMAIN, "subdomain": "expressive_language", "months": 30,
     "milestone": "name things in a book", "state": "not_demonstrated"},
    {"domain": DOMAIN, "subdomain": "expressive_language", "months": 30,
     "milestone": "say two or more words together with one action",
     "state": "demonstrated"},
    {"domain": DOMAIN, "subdomain": "expressive_language", "months": 30,
     "milestone": "says about 50 words", "state": "demonstrated"},
    {"domain": DOMAIN, "subdomain": "expressive_language", "months": 30,
     "milestone": "says words like I me or we", "state": "unknown"},
]

BAND_TOTALS = [{"months": 24, "total_skills": 1},
               {"months": 30, "total_skills": 4}]


def body(**overrides):
    out = {
        "source_session_id": SESSION,
        "source_record_digest": DIGEST_A,
        "summary": dict(SUMMARY),
        "skills": [dict(s) for s in SKILL_ROWS],
        "band_totals": [dict(b) for b in BAND_TOTALS],
    }
    out.update(overrides)
    return out


class FakeRungSource:
    """The two methods the canonicalisation boundary calls, and nothing else.

    Refs are deterministic per milestone so a test can predict them, and carry
    the `rung1:` prefix the domain object requires — a Parent-side identity
    cannot be smuggled through as a pilot ref.
    """

    def __init__(self, *, rungs=None, domain=DOMAIN):
        self._domain = domain
        self._rungs = {}
        for months, milestone, subdomain in (rungs or self._default()):
            key = (months, normalize_milestone_text(milestone))
            ref = "rung1:" + f"{abs(hash(key)):016x}"[:16]
            self._rungs.setdefault(key, (ref, subdomain))

    @staticmethod
    def _default():
        rows = [(24, "says at least two words together", "expressive_language")]
        for row in SKILL_ROWS:
            if row["months"] == 30:
                rows.append((30, row["milestone"], row["subdomain"]))
        # A 36-month rung that is NOT in the 30-month band, for the
        # cross-band-membership attack.
        rows.append((36, "ask who or what or where or why",
                     "expressive_language"))
        return rows

    def canonical_identity(self, domain_key, months, milestone_text):
        if (domain_key or "").strip() != self._domain:
            raise ValueError("this source does not cover the requested domain")
        if isinstance(months, bool) or not isinstance(months, int):
            raise ValueError("months must be an integer")
        wanted = normalize_milestone_text(milestone_text)
        matches = [v for (m, t), v in self._rungs.items()
                   if m == months and t == wanted]
        if not matches:
            raise ValueError("no canonical rung matches this source skill")
        if len(matches) > 1:  # pragma: no cover - defended, not reachable here
            raise ValueError("more than one canonical rung matches")
        return matches[0]

    def declared_band_roster(self, domain_key, months):
        if (domain_key or "").strip() != self._domain:
            raise ValueError("this source does not cover the requested domain")
        return tuple(sorted(ref for (m, _), (ref, _s) in self._rungs.items()
                            if m == months))

    def ref_for(self, months, milestone):
        return self._rungs[(months, normalize_milestone_text(milestone))][0]


class Stack:
    """Repos over a fake store, one linked child, and the v2 service."""

    def __init__(self, *, link=True, rung_source=None):
        self.repos = FirestoreRepositories(FakeDocumentStore())
        self.rungs = rung_source or FakeRungSource()
        self.child = self.repos.children.create(Child.create(actor_id="seed"))
        if link:
            self.repos.source_links.create(SourceSystemLink.create(
                self.child.child_id, SourceSystem.PARENT, SESSION,
                actor_id="seed"))

    def service(self):
        return BaselineProjectionV2Service(repos=self.repos,
                                          rung_source=self.rungs)

    def accept(self, payload=None, *, digest=DIGEST_A, session=SESSION):
        data = payload or body()
        return self.service().accept(
            source_session_id=session, source_record_digest=digest,
            summary=data["summary"], skills=data["skills"],
            band_totals=data["band_totals"])


# ===========================================================================
# ITEM 7 — the strict request codec
# ===========================================================================


def test_a_well_formed_body_decodes_to_exactly_what_was_sent():
    decoded = decode_body(body())
    assert set(decoded) == set(BODY_KEYS)
    assert len(decoded["skills"]) == 5
    assert decoded["band_totals"][1] == {"months": 30, "total_skills": 4}


@pytest.mark.parametrize("missing", list(BODY_KEYS))
def test_a_missing_top_level_key_is_refused(missing):
    data = body()
    del data[missing]
    with pytest.raises(ProjectionV2RequestError):
        decode_body(data)


@pytest.mark.parametrize("extra", [
    # Everything the Pilot DERIVES. Sending one is refused, never ignored.
    "child_id", "projection_id", "source_system", "projected_at",
    "projection_schema",
    # Everything that must never cross at all.
    "asked", "raw_answer", "question_id", "skill_key", "parent_uid",
    "child_name", "diagnosis", "concern", "qna", "chronological_months",
    "entry_choice_label", "dev_age",
])
def test_an_unknown_top_level_key_is_refused_not_ignored(extra):
    """"Ignore extra keys" is the behaviour that makes a boundary rot.

    An ignored key leaves the sender believing it transmitted something and the
    receiver believing it received a complete payload. If Parent ever starts
    sending `raw_answer`, this must FAIL rather than quietly discard it.
    """
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(**{extra: "whatever"}))


@pytest.mark.parametrize("field", sorted(SUMMARY))
def test_a_missing_summary_field_is_refused(field):
    summary = dict(SUMMARY)
    del summary[field]
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(summary=summary))


def test_an_unknown_summary_field_is_refused():
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(summary=dict(SUMMARY, demonstrated_months=24)))
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(summary=dict(SUMMARY, asked=[])))


def test_a_null_anchor_is_accepted_and_not_coerced_to_zero():
    """An UNRESOLVED baseline genuinely has no anchor.

    Zero would be a measurement nobody made, and month 0 is a real band.
    """
    decoded = decode_body(body(summary=dict(
        SUMMARY, routing_anchor_months=None, not_demonstrated_months=None)))
    assert decoded["summary"]["routing_anchor_months"] is None
    assert decoded["summary"]["not_demonstrated_months"] is None


@pytest.mark.parametrize("field", ["domain", "subdomain", "months",
                                   "milestone", "state"])
def test_a_missing_or_unknown_skill_field_is_refused(field):
    rows = [dict(r) for r in SKILL_ROWS]
    del rows[0][field]
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills=rows))

    rows = [dict(r) for r in SKILL_ROWS]
    rows[0]["rung_ref"] = "rung1:deadbeef"
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills=rows))


@pytest.mark.parametrize("state", [
    "unassessed",       # the ABSENCE of a record, never a transmitted value
    "DEMONSTRATED", "demonstrated ", "mastered", "yes", "", None, 1, True,
])
def test_the_state_enum_is_strict(state):
    rows = [dict(r) for r in SKILL_ROWS]
    rows[0]["state"] = state
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills=rows))


def test_duplicate_skill_rows_are_refused():
    """Otherwise one state silently overwrites the other and the band is short."""
    rows = [dict(r) for r in SKILL_ROWS] + [dict(SKILL_ROWS[1])]
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills=rows))

    # Whitespace-only variation is the same identity, and is still caught.
    sneaky = dict(SKILL_ROWS[1])
    sneaky["milestone"] = "  name   things  in a book  "
    sneaky["state"] = "demonstrated"
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills=[dict(r) for r in SKILL_ROWS] + [sneaky]))


def test_duplicate_band_totals_are_refused():
    totals = [dict(b) for b in BAND_TOTALS] + [{"months": 30,
                                                "total_skills": 3}]
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(band_totals=totals))


def test_empty_and_oversized_lists_are_refused():
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills=[]))
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(band_totals=[]))

    too_many = [dict(SKILL_ROWS[0], milestone=f"milestone number {i}")
                for i in range(MAX_SKILLS + 1)]
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills=too_many))

    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills=[dict(SKILL_ROWS[0],
                                      milestone="x" * (MAX_MILESTONE_CHARS + 1))]))


@pytest.mark.parametrize("months", [-1, 241, True, 1.5, "30", None])
def test_out_of_range_or_wrongly_typed_months_are_refused(months):
    """`True` matters: `bool` is an `int`, and would silently become month 1."""
    rows = [dict(r) for r in SKILL_ROWS]
    rows[0]["months"] = months
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills=rows))


@pytest.mark.parametrize("total", [0, -1, True, "4", None, MAX_SKILLS + 1])
def test_an_invalid_band_total_is_refused(total):
    totals = [dict(b) for b in BAND_TOTALS]
    totals[1]["total_skills"] = total
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(band_totals=totals))


@pytest.mark.parametrize("digest", [
    "", "z" * 64, "A" * 64, "a" * 63, "a" * 65, 12345, None,
])
def test_the_digest_must_be_lowercase_sha256_hex_exactly(digest):
    """Not normalised: `hexdigest()` is already lowercase, so anything else did
    not come from the canonical digest function."""
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(source_record_digest=digest))


def test_a_non_object_body_and_non_list_members_are_refused():
    for bad in ([], "string", 42, None):
        with pytest.raises(ProjectionV2RequestError):
            decode_body(bad)
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills={"a": 1}))
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(band_totals="30"))
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(skills=["not a mapping"]))


def test_the_size_cap_is_applied_BEFORE_the_body_is_parsed():
    """`json.loads` on a hostile body is the attackable step.

    Proved by a stream that RAISES if read: the refusal has to happen on the
    declared length alone.
    """
    class Exploding:
        def read(self, _n):  # pragma: no cover - must never be called
            raise AssertionError("the body was read despite the size cap")

    with pytest.raises(ProjectionV2RequestTooLarge):
        read_request({"CONTENT_LENGTH": str(MAX_BODY_BYTES + 1),
                      "wsgi.input": Exploding()})


def test_a_missing_or_unparseable_body_is_refused_without_quoting_it():
    with pytest.raises(ProjectionV2RequestError):
        read_request({"CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b"")})
    with pytest.raises(ProjectionV2RequestError):
        read_request({"CONTENT_LENGTH": "nope", "wsgi.input": io.BytesIO(b"")})

    broken = json.dumps(body())[:-20].encode()
    try:
        read_request({"CONTENT_LENGTH": str(len(broken)),
                      "wsgi.input": io.BytesIO(broken)})
    except ProjectionV2RequestError as exc:
        # `json`'s own message quotes the offending document fragment, which
        # here would be milestone prose. It must not be propagated.
        assert "milestone" not in str(exc)
        assert "says about 50 words" not in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a truncated body was accepted")


def test_a_real_payload_fits_the_cap_with_room_to_spare():
    """The cap must not be so tight that a genuine assessment is refused."""
    raw = json.dumps(body()).encode()
    assert len(raw) < MAX_BODY_BYTES / 4


# ===========================================================================
# ITEM 8 — canonicalisation happens here, against the Pilot's own source
# ===========================================================================


def test_every_skill_resolves_to_exactly_one_canonical_rung():
    stack = Stack()
    result = stack.accept()
    assert result.created is True

    projection = result.projection
    assert projection.projection_schema == PROJECTION_SCHEMA_V2
    assert projection.projection_id.startswith("pbp2_")
    assert projection.child_id == stack.child.child_id
    refs = [e.rung_ref for e in projection.skill_evidence]
    assert len(refs) == len(set(refs)) == 5
    assert all(r.startswith("rung1:") for r in refs)

    # The payload never named a ref, and could not have.
    assert all("rung_ref" not in row for row in SKILL_ROWS)


def test_a_skill_with_no_canonical_match_fails_the_whole_projection():
    """Not dropped. A projection smaller than the assessment is the worst
    outcome available: it would look complete while being quietly short, and the
    Pilot would compute completeness against evidence it never received."""
    stack = Stack()
    rows = [dict(r) for r in SKILL_ROWS]
    rows.append(dict(SKILL_ROWS[0], months=30,
                     milestone="a milestone nobody has ever declared"))
    with pytest.raises(SkillCanonicalisationError):
        stack.accept(body(skills=rows))
    assert _stored_count(stack) == 0


def test_a_subdomain_disagreement_fails_closed():
    """The canonical ref excludes subdomain, so a relabel would otherwise map
    silently to the right rung with the wrong clinical grouping."""
    stack = Stack()
    rows = [dict(r) for r in SKILL_ROWS]
    rows[1]["subdomain"] = "receptive_language"
    with pytest.raises(SkillCanonicalisationError):
        stack.accept(body(skills=rows))
    assert _stored_count(stack) == 0


def test_a_wrong_domain_fails_closed():
    stack = Stack()
    rows = [dict(r) for r in SKILL_ROWS]
    rows[0]["domain"] = "moving_and_coordination"
    with pytest.raises(SkillCanonicalisationError):
        stack.accept(body(skills=rows))


def test_a_v1_baseline_version_cannot_produce_a_v2_projection():
    stack = Stack()
    summary = dict(SUMMARY,
                   baseline_version="parent-2.4-functional-baseline-v1")
    with pytest.raises(SkillCanonicalisationError):
        stack.accept(body(summary=summary))


# ===========================================================================
# ITEM 9 — the denominator attack, through the real service
# ===========================================================================


def test_understating_a_band_total_cannot_manufacture_completeness():
    """The founder's attack. `total_skills=3` with three valid 30m rows.

    If this were accepted, `assessment_complete(30)` would be TRUE while a
    fourth declared skill had never been asked — missing evidence becoming
    mastery, the one failure this whole repair exists to prevent, reintroduced
    through the denominator instead of the questioning.
    """
    stack = Stack()
    rows = [r for r in SKILL_ROWS
            if r["months"] == 24 or "I me or we" not in r["milestone"]]
    totals = [{"months": 24, "total_skills": 1},
              {"months": 30, "total_skills": 3}]
    with pytest.raises(SkillCanonicalisationError):
        stack.accept(body(skills=[dict(r) for r in rows], band_totals=totals))
    assert _stored_count(stack) == 0


def test_overstating_a_band_total_cannot_hide_a_target():
    """The other direction: a complete band made to look incomplete."""
    stack = Stack()
    totals = [{"months": 24, "total_skills": 1},
              {"months": 30, "total_skills": 5}]
    with pytest.raises(SkillCanonicalisationError):
        stack.accept(body(band_totals=totals))
    assert _stored_count(stack) == 0


def test_a_row_from_another_band_cannot_pad_a_bands_count():
    """A band of the right SIZE made of the wrong skills is still wrong.

    Two distinct refusals, and it is worth being exact about WHICH fires,
    because they sit at different depths:

    1. A row that DECLARES the wrong band fails at CANONICALISATION. The
       resolver matches on `(months, milestone)`, so a 36-month milestone
       claiming `months=30` resolves to nothing — "no canonical rung matches".

    2. A row that declares its own real band but whose band has no declared
       denominator fails the band-total check.

    Note what this means for the MEMBERSHIP check in `verify_band_denominators`:
    it is structurally UNREACHABLE through this service path, because the
    resolver can only ever return a ref that belongs to the band the evidence's
    `months` names. It is deliberate defence-in-depth for a future caller that
    builds `ProjectedSkillEvidence` without going through the resolver, and it is
    unit-tested directly in `test_baseline_skill_projection_v2.py` rather than
    pretended to be exercised here.
    """
    stack = Stack()
    kept = [dict(r) for r in SKILL_ROWS if "I me or we" not in r["milestone"]]
    totals = [{"months": 24, "total_skills": 1},
              {"months": 30, "total_skills": 4}]

    # 1. the 36-month milestone, mislabelled as a 30-month skill.
    mislabelled = kept + [{"domain": DOMAIN,
                           "subdomain": "expressive_language", "months": 30,
                           "milestone": "ask who or what or where or why",
                           "state": "demonstrated"}]
    with pytest.raises(SkillCanonicalisationError) as caught:
        stack.accept(body(skills=mislabelled, band_totals=totals))
    assert "exactly one canonical rung" in str(caught.value)

    # 2. the same rung at its OWN band, which has no declared denominator.
    honest_band = kept + [{"domain": DOMAIN,
                           "subdomain": "expressive_language", "months": 36,
                           "milestone": "ask who or what or where or why",
                           "state": "demonstrated"}]
    with pytest.raises(SkillCanonicalisationError) as caught:
        stack.accept(body(skills=honest_band, band_totals=totals))
    assert "no band-total declaration" in str(caught.value)
    assert _stored_count(stack) == 0


def test_the_duplicate_canonical_ref_check_is_the_services_own_and_is_load_bearing():
    """Two DIFFERENT source rows that resolve to ONE rung.

    The codec cannot catch this: its duplicate check compares source identity
    with whitespace collapsed but case preserved, while canonicalisation
    casefolds. So the two layers catch different things, and this test pins that
    the deeper one is actually doing work rather than being shadowed.
    """
    stack = Stack()
    rows = [dict(r) for r in SKILL_ROWS]
    rows.append(dict(SKILL_ROWS[3], milestone="SAYS ABOUT 50 WORDS",
                     state="not_demonstrated"))

    # The codec accepts it — two distinct source identities.
    assert len(decode_body(body(skills=rows))["skills"]) == 6

    # The canonicalisation boundary refuses it.
    with pytest.raises(SkillCanonicalisationError) as caught:
        stack.accept(body(skills=rows))
    assert "one canonical rung" in str(caught.value)
    assert _stored_count(stack) == 0


def test_evidence_for_a_band_with_no_declared_total_is_refused():
    stack = Stack()
    with pytest.raises(SkillCanonicalisationError):
        stack.accept(body(band_totals=[{"months": 24, "total_skills": 1}]))


def test_the_honest_projection_still_succeeds_and_derives_correctly():
    stack = Stack()
    projection = stack.accept().projection

    assert projection.assessment_complete(24) is True
    assert projection.band_mastered(24) is True
    assert projection.assessment_complete(30) is True
    # Not mastered: one deficit and one unknown.
    assert projection.band_mastered(30) is False

    states = sorted(e.state for e in projection.assessed_in_band(30))
    assert states == ["demonstrated", "demonstrated", "not_demonstrated",
                      "unknown"]
    # The unknown does not suppress the known deficit, and is not itself a
    # target: an unanswerable question is not evidence a skill is absent.
    unresolved = projection.unresolved_skills()
    assert len(unresolved) == 1
    assert unresolved[0].state == "not_demonstrated"


# ===========================================================================
# ITEM 9 continued — unmappable skills survive transport
# ===========================================================================


def test_a_canonicalisable_but_unmappable_skill_is_carried_not_dropped():
    """`pronouns` has no canonical activity family and must still project.

    Baseline evidence is about the CHILD; activity mappability is about OUR
    content. Dropping the skill would make the projection quietly smaller than
    the assessment, and a `not_demonstrated` pronoun would vanish rather than
    being available to a later clinical decision.
    """
    stack = Stack()
    rows = [dict(r) for r in SKILL_ROWS]
    pronouns = next(r for r in rows if "I me or we" in r["milestone"])
    pronouns["state"] = "not_demonstrated"

    projection = stack.accept(body(skills=rows)).projection
    expected = stack.rungs.ref_for(30, pronouns["milestone"])
    states = {e.rung_ref: e.state for e in projection.skill_evidence}
    assert states[expected] == "not_demonstrated"
    assert expected in {e.rung_ref for e in projection.unresolved_skills()}


# ===========================================================================
# ITEM 10 — immutability, replay, and the separate collection
# ===========================================================================


def _stored_count(stack):
    return len(stack.repos.parent_baseline_projections_v2.list_for_source(
        SESSION, DOMAIN))


def test_an_exact_replay_returns_the_existing_projection_and_writes_nothing():
    stack = Stack()
    first = stack.accept()
    second = stack.accept()

    assert first.created is True and second.created is False
    assert first.projection.projection_id == second.projection.projection_id
    assert first.projection == second.projection
    assert _stored_count(stack) == 1


def test_the_projection_id_is_deterministic_from_its_source_identity():
    stack = Stack()
    projection = stack.accept().projection
    assert projection.projection_id == projection_v2_id_for(
        SESSION, DOMAIN, DIGEST_A)


def test_a_changed_source_record_is_an_integrity_conflict_not_an_overwrite():
    """A finalized Parent v2 baseline is immutable, so two digests cannot both
    be it. Versioning would record a history the source does not have, and
    overwriting would destroy evidence a clinical decision may already rest on.
    """
    stack = Stack()
    stack.accept(digest=DIGEST_A)
    with pytest.raises(ProjectionIntegrityError):
        stack.accept(digest=DIGEST_B)
    # The original survives untouched.
    assert _stored_count(stack) == 1
    assert stack.repos.parent_baseline_projections_v2.find(
        projection_v2_id_for(SESSION, DOMAIN, DIGEST_A)) is not None


def test_the_repository_offers_no_way_to_edit_or_delete_a_projection():
    """The absence of a method is a stronger guarantee than a rule."""
    repo = Stack().repos.parent_baseline_projections_v2
    for forbidden in ("update", "set", "overwrite", "delete", "replace",
                      "save", "put"):
        assert not hasattr(repo, forbidden), forbidden
    assert sorted(m for m in dir(repo) if not m.startswith("_")) == [
        "create", "find", "list_for_source", "model", "record_type"]


def test_v1_and_v2_projections_live_in_different_collections():
    from pilot_backend.persistence.collections import COLLECTIONS

    assert (COLLECTIONS["parent_baseline_projection_v2"]
            == "pilot_parent_baseline_projections_v2")
    assert (COLLECTIONS["parent_baseline_projection"]
            != COLLECTIONS["parent_baseline_projection_v2"])

    stack = Stack()
    stack.accept()
    # Nothing landed in v1's collection, and v1's repo cannot see the v2 doc.
    assert stack.repos.parent_baseline_projections.list_for_source(
        SESSION, DOMAIN) == []


def test_the_stored_document_carries_no_clinical_prose():
    """ITEM 6's "must not persist" half, asserted on the stored bytes.

    The milestone was consumed to find the ref and discarded, so the collection
    could be exported in full without leaking a milestone.
    """
    stack = Stack()
    projection = stack.accept().projection
    raw = stack.repos.parent_baseline_projections_v2._store.get(
        "pilot_parent_baseline_projections_v2", projection.projection_id)
    blob = json.dumps(raw, default=str)

    assert sorted(raw) == [
        "area_id", "band_totals", "baseline_version", "child_id", "domain",
        "entry_choice_id", "not_demonstrated_months", "projected_at",
        "projection_id", "projection_schema", "routing_anchor_months",
        "schema_version", "skill_evidence", "source_record_digest",
        "source_session_id", "source_system", "status"]
    for forbidden in ("milestone", "subdomain", "skill_key", "question_id",
                      "raw_answer", "answer", "asked", "name things in a book",
                      "says about 50 words", "I me or we",
                      "chronological_months", "parent_uid", "owner_uid"):
        assert forbidden not in blob, forbidden
    for row in raw["skill_evidence"]:
        assert sorted(row) == ["months", "rung_ref", "state"]


def test_a_stored_projection_round_trips_through_the_codec():
    stack = Stack()
    original = stack.accept().projection
    reloaded = stack.repos.parent_baseline_projections_v2.find(
        original.projection_id)
    assert reloaded == original
    assert isinstance(reloaded, ParentBaselineProjectionV2)


# ===========================================================================
# child resolution, and the check ORDER
# ===========================================================================


def test_an_unlinked_session_is_refused_and_no_child_is_invented():
    """There is nothing to attach a projection to, and inventing a `Child` here
    would create a clinical record out of a message."""
    stack = Stack(link=False)
    with pytest.raises(ProjectionV2ChildUnresolved):
        stack.accept()
    assert _stored_count(stack) == 0
    # The service names no child repository at all, so it could not create one.
    tree = _strip_docstrings(ast.parse(
        _v2_sources()["baseline_projection_v2_service"].read_text()))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "children" not in attrs
    assert "source_links" in attrs


def test_two_active_links_for_one_session_fail_closed():
    stack = Stack()
    other = stack.repos.children.create(Child.create(actor_id="seed"))
    stack.repos.source_links.create(SourceSystemLink.create(
        other.child_id, SourceSystem.PARENT, SESSION, actor_id="seed"))
    with pytest.raises(ProjectionV2LinkAmbiguous):
        stack.accept()


def test_the_payload_cannot_name_a_child():
    """Even the authenticated Parent service can only name a SESSION."""
    stack = Stack()
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body(child_id="chld_attacker"))
    # And the summary allowlist refuses it too, one layer deeper.
    with pytest.raises(ProjectionV2Error):
        ParentBaselineProjectionV2.build(
            child_id="x", source_session_id=SESSION,
            source_record_digest=DIGEST_A,
            summary=dict(SUMMARY, child_id="chld_attacker"),
            skill_evidence=(), band_totals=())


def test_a_malformed_payload_never_causes_a_repository_read():
    """v1's rule, preserved: otherwise validation failures become a probe for
    which sessions exist."""
    class Exploding:
        def __getattr__(self, name):  # pragma: no cover - must not be reached
            raise AssertionError(f"the repository was read: {name}")

    service = BaselineProjectionV2Service(repos=Exploding(),
                                          rung_source=FakeRungSource())
    data = body()
    # A canonicalisation failure.
    with pytest.raises(SkillCanonicalisationError):
        service.accept(
            source_session_id=SESSION, source_record_digest=DIGEST_A,
            summary=data["summary"],
            skills=[dict(SKILL_ROWS[0], milestone="undeclared milestone")],
            band_totals=[{"months": 24, "total_skills": 1}])
    # A denominator failure.
    with pytest.raises(SkillCanonicalisationError):
        service.accept(
            source_session_id=SESSION, source_record_digest=DIGEST_A,
            summary=data["summary"], skills=data["skills"],
            band_totals=[{"months": 24, "total_skills": 1},
                         {"months": 30, "total_skills": 3}])
    # A bad digest.
    with pytest.raises(ProjectionValidationError):
        service.accept(source_session_id=SESSION, source_record_digest="nope",
                       summary=data["summary"], skills=data["skills"],
                       band_totals=data["band_totals"])


def test_a_service_without_a_rung_source_refuses_to_exist():
    """No degraded mode. A projection service with no canonical source could
    only accept identities it was handed."""
    with pytest.raises(ValueError):
        BaselineProjectionV2Service(repos=object(), rung_source=None)


# ===========================================================================
# the transport, and the router
# ===========================================================================


def _app(stack, *, verifier=None):
    return ProjectionV2App(
        verifier=verifier, service_factory=stack.service)


def _call(app, *, path=PROJECTION_V2_ROUTE, method="POST", data=None,
          headers=None):
    raw = json.dumps(data if data is not None else body()).encode()
    environ = {"PATH_INFO": path, "REQUEST_METHOD": method,
               "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
    environ.update(headers or {})
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = response_headers

    chunks = app(environ, start_response)
    payload = json.loads(b"".join(chunks))
    return int(captured["status"].split()[0]), payload, captured["headers"]


def test_the_route_returns_201_on_create_and_200_on_replay():
    stack = Stack()
    app = _app(stack)
    status, payload, _ = _call(app)
    assert status == 201
    assert payload["created"] is True
    assert payload["projection_id"].startswith("pbp2_")

    status, payload, _ = _call(app)
    assert status == 200
    assert payload["created"] is False


def test_the_response_carries_only_derived_ids():
    status, payload, _ = _call(_app(Stack()))
    assert sorted(payload) == ["child_id", "created", "projection_id"]
    blob = json.dumps(payload)
    for leaked in ("milestone", "state", "demonstrated", "band", "status",
                   "says about 50 words"):
        assert leaked not in blob, leaked


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH",
                                    "OPTIONS", "HEAD"])
def test_only_POST_is_allowed_on_the_v2_route(method):
    status, payload, _ = _call(_app(Stack()), method=method)
    assert status == 405
    assert payload == {"error": "method not allowed"}


def test_no_cors_header_is_ever_emitted():
    """A browser cannot use this endpoint, so there is nothing to negotiate —
    and nothing an origins list could widen."""
    _status, _payload, headers = _call(_app(Stack()))
    assert not [h for h, _v in headers if h.lower().startswith("access-control")]
    names = {h.lower() for h, _v in headers}
    assert "cache-control" in names and "x-content-type-options" in names


def test_every_refusal_returns_a_constant_phi_safe_body():
    """The status differs; the message never does. A projection body is clinical
    content, so the response says only that something was refused."""
    stack = Stack()
    app = _app(stack)

    # 400 — a shape failure.
    status, payload, _ = _call(app, data=body(skills=[]))
    assert (status, payload) == (400, {"error": "not accepted"})

    # 400 — a canonicalisation failure. Same body as a shape failure, so the
    # caller cannot tell which of its fields was wrong.
    status, payload, _ = _call(app, data=body(
        skills=[dict(SKILL_ROWS[0], milestone="undeclared milestone")],
        band_totals=[{"months": 24, "total_skills": 1}]))
    assert (status, payload) == (400, {"error": "not accepted"})

    # 400 — a denominator failure.
    status, payload, _ = _call(app, data=body(
        band_totals=[{"months": 24, "total_skills": 1},
                     {"months": 30, "total_skills": 3}]))
    assert (status, payload) == (400, {"error": "not accepted"})

    # 413 — too large, refused before the parse.
    big = body(skills=[dict(SKILL_ROWS[0],
                            milestone=f"padding milestone number {i}")
                       for i in range(MAX_SKILLS)])
    raw = json.dumps(big).encode()
    environ = {"PATH_INFO": PROJECTION_V2_ROUTE, "REQUEST_METHOD": "POST",
               "CONTENT_LENGTH": str(MAX_BODY_BYTES + 1),
               "wsgi.input": io.BytesIO(raw)}
    captured = {}
    chunks = app(environ, lambda s, h: captured.setdefault("status", s))
    assert captured["status"].startswith("413")
    assert json.loads(b"".join(chunks)) == {"error": "not accepted"}

    # 404 — an unlinked session. Constant message, so this is not an oracle for
    # which sessions exist; only the status differs.
    status, payload, _ = _call(_app(Stack(link=False)))
    assert (status, payload) == (404, {"error": "not accepted"})

    # 409 — an integrity conflict.
    _call(app, data=body(source_record_digest=DIGEST_A))
    status, payload, _ = _call(app, data=body(source_record_digest=DIGEST_B))
    assert (status, payload) == (409, {"error": "integrity conflict"})


def test_an_unexpected_internal_failure_never_quotes_the_exception():
    """A canonicalisation failure could otherwise quote a milestone back."""
    class Boom:
        def accept(self, **kwargs):
            raise RuntimeError(f"exploded while handling {PLANTED}")

    app = ProjectionV2App(verifier=None, service_factory=Boom)
    status, payload, _ = _call(app)
    assert (status, payload) == (500, {"error": "unavailable"})
    assert PLANTED not in json.dumps(payload)


def test_app_level_verification_runs_before_the_body_is_read():
    """A wrong caller must not be able to make this process parse JSON."""
    class Refusing:
        def verify(self, _bearer):
            raise ValueError("not permitted")

    class Exploding:
        def read(self, _n):  # pragma: no cover - must never be called
            raise AssertionError("the body was read for an unverified caller")

    app = ProjectionV2App(verifier=Refusing(),
                          service_factory=Stack().service)
    captured = {}
    chunks = app({"PATH_INFO": PROJECTION_V2_ROUTE, "REQUEST_METHOD": "POST",
                  "CONTENT_LENGTH": "100", "wsgi.input": Exploding()},
                 lambda s, h: captured.setdefault("status", s))
    assert captured["status"].startswith("401")
    assert json.loads(b"".join(chunks)) == {"error": "not permitted"}


def test_iam_only_mode_reads_no_token_at_all():
    """A SUPPORTED, DECLARED posture — Cloud Run IAM is the authoritative gate."""
    app = _app(Stack())
    assert app.verifies_tokens is False
    status, _payload, _h = _call(app, headers={"HTTP_AUTHORIZATION": "garbage"})
    assert status == 201


def test_a_verified_caller_is_accepted():
    class Accepting:
        def __init__(self):
            self.seen = []

        def verify(self, bearer):
            self.seen.append(bearer)
            return {"email": "parent-staging@example.iam.gserviceaccount.com"}

    verifier = Accepting()
    app = _app(Stack(), verifier=verifier)
    assert app.verifies_tokens is True
    status, _payload, _h = _call(
        app, headers={"HTTP_AUTHORIZATION": "Bearer good-token"})
    assert status == 201
    assert verifier.seen == ["Bearer good-token"]


# --- the router ------------------------------------------------------------


def _router(stack):
    v1 = ProjectionApp(verifier=None, service_factory=lambda: None)
    return InternalProjectionRouter(v1_app=v1, v2_app=_app(stack))


def test_the_router_sends_the_v2_path_to_the_v2_app():
    status, payload, _ = _call(_router(Stack()))
    assert status == 201
    assert payload["projection_id"].startswith("pbp2_")


def test_the_router_leaves_the_v1_route_and_its_404_to_the_v1_app():
    """v1 keeps ownership of what an unroutable internal request looks like, so
    there is no second 404 in this package that could drift from the first."""
    router = _router(Stack())

    # An unknown path falls through to v1's own 404.
    status, payload, _ = _call(router, path="/internal/nope")
    assert (status, payload) == (404, {"error": "not found"})

    # The v1 route reaches the v1 app: its service factory returns None, so a
    # well-formed v1 body would fail INSIDE v1 rather than being handled here.
    status, payload, _ = _call(router, path=PROJECTION_ROUTE,
                               data={"source_session_id": SESSION,
                                     "source_record_digest": DIGEST_A,
                                     "projection": {}})
    assert status == 500
    assert payload == {"error": "unavailable"}

    # A v2 BODY posted to the v1 path is refused by v1's own key check.
    status, payload, _ = _call(router, path=PROJECTION_ROUTE)
    assert (status, payload) == (400, {"error": "not accepted"})

    # And a v1 body posted to the v2 path is refused by the v2 codec.
    status, payload, _ = _call(router, path=PROJECTION_V2_ROUTE,
                               data={"source_session_id": SESSION,
                                     "source_record_digest": DIGEST_A,
                                     "projection": dict(SUMMARY)})
    assert (status, payload) == (400, {"error": "not accepted"})


def test_the_two_routes_are_distinct_and_neither_is_a_prefix_match():
    assert PROJECTION_V2_ROUTE != PROJECTION_ROUTE
    assert PROJECTION_V2_ROUTE == PROJECTION_ROUTE + "-v2"
    # A trailing slash still routes, and nothing longer does.
    status, _p, _h = _call(_router(Stack()), path=PROJECTION_V2_ROUTE + "/")
    assert status == 201
    status, payload, _ = _call(_router(Stack()),
                               path=PROJECTION_V2_ROUTE + "/extra")
    assert (status, payload) == (404, {"error": "not found"})


# ===========================================================================
# ITEM 11 — the logging audit. A RELEASE BLOCKER for this slice.
# ===========================================================================


def _v2_sources():
    return {
        "projection_v2_wsgi": PILOT_ROOT / "transport/projection_v2_wsgi.py",
        "projection_v2_request": PILOT_ROOT / "transport/projection_v2_request.py",
        "baseline_projection_v2_service":
            PILOT_ROOT / "integration/baseline_projection_v2_service.py",
        "baseline_skill_projection":
            PILOT_ROOT / "integration/baseline_skill_projection.py",
        "parent_baseline_projection_v2":
            PILOT_ROOT / "domain/parent_baseline_projection_v2.py",
    }


def _strip_docstrings(tree):
    """Recursively, so prose in a docstring cannot satisfy or trip a scan."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body_nodes = node.body
            if (body_nodes and isinstance(body_nodes[0], ast.Expr)
                    and isinstance(body_nodes[0].value, ast.Constant)
                    and isinstance(body_nodes[0].value.value, str)):
                node.body = body_nodes[1:]
    return tree


@pytest.mark.parametrize("name", sorted(_v2_sources()))
def test_no_v2_module_constructs_a_logger_or_prints(name):
    """Structural. The v2 request body carries MILESTONE PROSE, so a logged body
    or a quoted parse error would put clinical content into a log this
    application cannot redact afterwards."""
    tree = _strip_docstrings(ast.parse(_v2_sources()[name].read_text()))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "logging" not in imported, name
    assert "sys" not in imported, name

    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "print" not in called, name

    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attrs & {"getLogger", "basicConfig", "warning", "exception",
                        "stderr", "stdout"}, name


def test_the_v2_service_names_no_unrelated_repository():
    """It writes exactly one record type."""
    tree = _strip_docstrings(ast.parse(
        _v2_sources()["baseline_projection_v2_service"].read_text()))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ("goal_suggestions", "clinical_goals", "caregiver_goals",
                      "focus_plans", "weekly_cycles", "goal_allocations",
                      "suggestion_anchors", "clinical_goal_anchors",
                      "parent_baseline_projections", "audit_events",
                      "identity_claims"):
        assert forbidden not in attrs, forbidden
    assert "parent_baseline_projections_v2" in attrs


def test_a_planted_milestone_never_reaches_a_log_on_success_or_on_failure(
        caplog, capsys):
    """ITEM 11's runtime half, across all three outcomes.

    The planted string is unmistakable, so a single appearance anywhere in
    captured logging, stdout or stderr fails the test.
    """
    planted_rows = [dict(SKILL_ROWS[0], milestone=PLANTED)]
    rung_source = FakeRungSource(
        rungs=[(24, PLANTED, "expressive_language")])
    stack = Stack(rung_source=rung_source)
    app = _app(stack)

    scenarios = [
        # success
        (body(skills=planted_rows,
              band_totals=[{"months": 24, "total_skills": 1}]), 201),
        # codec failure — an invalid state, with the planted prose still present
        (body(skills=[dict(planted_rows[0], state="mastered")],
              band_totals=[{"months": 24, "total_skills": 1}]), 400),
        # canonicalisation failure — the planted prose at a band that has no
        # such rung
        (body(skills=[dict(planted_rows[0], months=30)],
              band_totals=[{"months": 30, "total_skills": 1}]), 400),
    ]

    for payload, expected in scenarios:
        caplog.clear()
        capsys.readouterr()
        with caplog.at_level(logging.DEBUG):
            status, response, _h = _call(app, data=payload)
        assert status == expected, (expected, response)

        captured = capsys.readouterr()
        haystack = "\n".join([
            caplog.text,
            "".join(r.getMessage() for r in caplog.records),
            captured.out, captured.err, json.dumps(response),
        ])
        assert PLANTED not in haystack, (expected, haystack[:400])
        assert "pelican" not in haystack
        # No record was emitted at all by these modules.
        assert not [r for r in caplog.records
                    if "projection" in (r.name or "")], caplog.records


def test_an_internal_exception_message_carries_no_milestone():
    """Chained causes matter: a traceback printed by a WSGI server would show
    `__cause__`. Every refusal this boundary raises is field-level prose."""
    rung_source = FakeRungSource(rungs=[(24, PLANTED, "expressive_language")])
    stack = Stack(rung_source=rung_source)

    with pytest.raises(SkillCanonicalisationError) as caught:
        stack.accept(body(
            skills=[dict(SKILL_ROWS[0], milestone=PLANTED, months=30)],
            band_totals=[{"months": 30, "total_skills": 1}]))

    chain, error = [], caught.value
    while error is not None:
        chain.append(str(error))
        error = error.__cause__
    joined = " | ".join(chain)
    assert PLANTED not in joined, joined
    assert "pelican" not in joined
