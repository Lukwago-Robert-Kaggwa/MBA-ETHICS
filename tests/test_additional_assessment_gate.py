from datetime import datetime
from types import SimpleNamespace

from app.mba.route_support import (
    INVITATION_ACCEPTED,
    INVITATION_PENDING,
    additional_assessment_required,
    project_has_any_invitation_response,
    project_invitation_snapshot,
    required_assessor_acceptance_packs_complete,
)
from app.models import ProjectStatus


def _doc(doc_type, uploaded_by_id):
    return SimpleNamespace(doc_type=doc_type, uploaded_by_id=uploaded_by_id)


def _acceptance_docs(slot, assessor_id):
    return [
        _doc(f"assessor_temp_appointment_{slot}", assessor_id),
        _doc(f"assessor_temp_claim_{slot}", assessor_id),
        _doc(f"assessor_cv_{slot}", assessor_id),
        _doc(f"assessor_highest_qualification_{slot}", assessor_id),
    ]


def _project(additional_status=INVITATION_PENDING, include_additional_docs=False):
    docs = [
        *_acceptance_docs("assessor_1", 201),
        *_acceptance_docs("assessor_2", 202),
    ]
    if include_additional_docs:
        docs.extend(_acceptance_docs("assessor_3", 203))
    return SimpleNamespace(
        primary_supervisor_id=101,
        primary_supervisor_invitation_status=INVITATION_ACCEPTED,
        supervisor_invitations=[],
        assessor_1_id=201,
        assessor_1_invitation_status=INVITATION_ACCEPTED,
        assessor_2_id=202,
        assessor_2_invitation_status=INVITATION_ACCEPTED,
        assessor_3_id=203,
        assessor_3_invitation_status=additional_status,
        invitations_sent_at=None,
        additional_assessment_requested_at=datetime.utcnow(),
        project_status=ProjectStatus.HDC_VERIFIED.value,
        documents=docs,
    )


def test_additional_assessment_reopens_invitation_snapshot_for_third_assessor():
    project = _project()

    assert additional_assessment_required(project)
    assert project_has_any_invitation_response(project)

    snapshot = project_invitation_snapshot(project)

    assert snapshot["statuses"]["assessor_3"] == INVITATION_PENDING
    assert snapshot["required_assessor_count"] == 3
    assert snapshot["required_assessor_accepted_count"] == 2
    assert not snapshot["required_assessors_accepted"]
    assert not snapshot["all_assigned_accepted"]
    assert not snapshot["assessor_packs_complete"]


def test_additional_assessment_completes_gate_after_third_assessor_accepts():
    project = _project(additional_status=INVITATION_ACCEPTED, include_additional_docs=True)

    snapshot = project_invitation_snapshot(project)

    assert snapshot["statuses"]["assessor_3"] == INVITATION_ACCEPTED
    assert snapshot["required_assessor_accepted_count"] == 3
    assert snapshot["required_assessors_accepted"]
    assert snapshot["all_assigned_accepted"]
    assert snapshot["assessor_packs_complete"]
    assert required_assessor_acceptance_packs_complete(project)
