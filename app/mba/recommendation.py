import re


MATCH_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "that",
    "this",
    "project",
    "research",
    "mba",
    "study",
    "using",
    "use",
    "about",
    "are",
    "was",
    "were",
}

SUPERVISOR_RECOMMENDATION_LIMIT = 2
ASSESSOR_RECOMMENDATION_LIMIT = 2


def tokenize(value):
    text = (value or "").strip().lower()
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", text)
    return {word for word in words if len(word) >= 3 and word not in MATCH_STOPWORDS}


def project_theme_terms(project):
    terms = set()
    terms |= tokenize(project.discipline_name)
    terms |= tokenize(project.project_title)
    terms |= tokenize(project.project_description)
    if project.student and project.student.student_profile:
        terms |= tokenize(project.student.student_profile.module)
        terms |= tokenize(project.student.student_profile.block_id)
    return terms


def candidate_profile_terms(user):
    profile = user.scholar_profile
    if not profile:
        return set()
    terms = set()
    terms |= tokenize(profile.research_themes)
    terms |= tokenize(profile.research_interests)
    terms |= tokenize(profile.research_disciplines)
    terms |= tokenize(profile.skills)
    terms |= tokenize(profile.department)
    terms |= tokenize(profile.position)
    terms |= tokenize(profile.qualification)
    terms |= tokenize(profile.affiliation)
    return terms


def rank_candidates(project, candidates):
    project_terms = project_theme_terms(project)
    ranked = []
    for candidate in candidates:
        expertise_terms = candidate_profile_terms(candidate)
        matches = sorted(project_terms & expertise_terms)
        ranked.append({"user": candidate, "score": len(matches), "matches": matches[:8]})
    ranked.sort(key=lambda item: (-item["score"], item["user"].email))
    return ranked


def filter_ranked_matches(ranked):
    positive_matches = [item for item in ranked if item["score"] > 0]
    return positive_matches or ranked


def recommend_supervisors(project, supervisors, limit=SUPERVISOR_RECOMMENDATION_LIMIT):
    return filter_ranked_matches(rank_candidates(project, supervisors))[:limit]


def recommend_assessors(project, examiners, excluded_user_ids=None, limit=ASSESSOR_RECOMMENDATION_LIMIT):
    excluded_user_ids = set(excluded_user_ids or [])
    recommendations = []
    for item in filter_ranked_matches(rank_candidates(project, examiners)):
        user = item["user"]
        if user.id in excluded_user_ids:
            continue
        recommendations.append(item)
        excluded_user_ids.add(user.id)
        if len(recommendations) == limit:
            break
    return recommendations


def match_recommendations(
    project,
    supervisors,
    examiners,
    supervisor_limit=SUPERVISOR_RECOMMENDATION_LIMIT,
    assessor_limit=ASSESSOR_RECOMMENDATION_LIMIT,
):
    ranked_supervisors = recommend_supervisors(project, supervisors, limit=supervisor_limit)
    supervisor_choice = ranked_supervisors[0]["user"] if ranked_supervisors else None
    if getattr(project, "primary_supervisor_id", None):
        excluded_ids = {project.primary_supervisor_id}
    else:
        excluded_ids = {supervisor_choice.id} if supervisor_choice else set()
    ranked_examiners = recommend_assessors(
        project,
        examiners,
        excluded_user_ids=excluded_ids,
        limit=assessor_limit,
    )

    return {
        "ranked_supervisors": ranked_supervisors,
        "ranked_examiners": ranked_examiners,
        "supervisor": supervisor_choice,
        "assessors": [item["user"] for item in ranked_examiners],
    }
