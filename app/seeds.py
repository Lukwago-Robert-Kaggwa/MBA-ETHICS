from .extensions import db
from .models import MbaDiscipline, MbaProject, MbaResearchInterest


def seed_mba_disciplines():
    names = {"General"}

    for interest in MbaResearchInterest.query.all():
        name = (interest.name or "").strip()
        if name:
            names.add(name)

    for project in MbaProject.query.all():
        name = (project.discipline or "").strip()
        if name:
            names.add(name)

    existing = {discipline.name.lower(): discipline for discipline in MbaDiscipline.query.all()}
    for index, name in enumerate(sorted(names)):
        if name.lower() not in existing:
            db.session.add(MbaDiscipline(name=name, sort_order=index))

    db.session.flush()

    discipline_by_name = {
        discipline.name.lower(): discipline
        for discipline in MbaDiscipline.query.order_by(MbaDiscipline.sort_order.asc(), MbaDiscipline.name.asc()).all()
    }

    for project in MbaProject.query.all():
        if project.discipline_id and project.discipline_option:
            project.discipline = project.discipline_option.name
            continue

        name = (project.discipline or "").strip()
        if not name:
            continue

        discipline = discipline_by_name.get(name.lower())
        if discipline:
            project.discipline_id = discipline.id
            project.discipline = discipline.name
