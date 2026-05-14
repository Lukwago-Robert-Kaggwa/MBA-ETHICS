from .extensions import db
from .models import EthicsRole, EthicsUser, MbaProject, MbaUser, normalize_email


def ethics_supervisor_from_mba_user(mba_supervisor):
    if not mba_supervisor:
        return None

    email = normalize_email(mba_supervisor.email)
    if not email:
        return None

    ethics_supervisor = EthicsUser.find_by_email(email)
    if not ethics_supervisor:
        ethics_supervisor = EthicsUser(
            email=email,
            first_name=mba_supervisor.first_name,
            last_name=mba_supervisor.last_name,
            role=EthicsRole.SUPERVISOR.value,
        )
        db.session.add(ethics_supervisor)
        db.session.flush()
    elif ethics_supervisor.role != EthicsRole.SUPERVISOR.value:
        return None

    return ethics_supervisor


def sync_ethics_supervisor_from_mba(ethics_student, mba_student=None):
    if not ethics_student or ethics_student.supervisor_id:
        return None

    if mba_student is None:
        mba_student = MbaUser.find_by_email(ethics_student.email)
    if not mba_student:
        return None

    project = (
        MbaProject.query.filter_by(student_id=mba_student.id)
        .filter(MbaProject.primary_supervisor_id.isnot(None))
        .order_by(MbaProject.updated_at.desc())
        .first()
    )
    if not project:
        return None

    mba_supervisor = db.session.get(MbaUser, project.primary_supervisor_id)
    ethics_supervisor = ethics_supervisor_from_mba_user(mba_supervisor)
    if not ethics_supervisor:
        return None

    ethics_student.supervisor_id = ethics_supervisor.id
    ethics_student.authenticated_student = True
    return ethics_supervisor
