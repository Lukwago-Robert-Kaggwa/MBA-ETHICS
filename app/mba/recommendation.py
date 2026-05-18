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

SHORT_MATCH_TERMS = {
    "ai",
    "esg",
    "hr",
    "jse",
    "roi",
    "sme",
}

PHRASE_ALIASES = {
    "artificial intelligence": {"ai", "machine", "learning", "data", "science", "analytics"},
    "business intelligence": {"analytics", "data"},
    "computer vision": {"ai", "image", "imaging", "machine", "learning"},
    "data science": {"ai", "analytics", "artificial", "intelligence", "machine", "learning"},
    "human resource": {"hr", "people", "talent"},
    "human resources": {"hr", "people", "talent"},
    "machine learning": {"ai", "analytics", "artificial", "data", "intelligence", "science"},
    "small and medium enterprises": {"sme", "enterprise"},
    "small medium enterprises": {"sme", "enterprise"},
}

TERM_ALIASES = {
    "bank": {"banking"},
    "banking": {"bank"},
    "employee": {"staff"},
    "finance": {"financial"},
    "financial": {"finance"},
    "organisation": {"organization", "organisational", "organizational"},
    "organisational": {"organisation", "organization", "organizational"},
    "organization": {"organisation", "organisational", "organizational"},
    "organizational": {"organisation", "organisational", "organization"},
    "performance": {"performing"},
    "staff": {"employee"},
    "strategic": {"strategy"},
    "strategy": {"strategic"},
}

SUPERVISOR_RECOMMENDATION_LIMIT = 2
ASSESSOR_RECOMMENDATION_LIMIT = 2


def term_variants(word):
    variants = {word}
    if word.endswith("ies") and len(word) > 4:
        variants.add(f"{word[:-3]}y")
    elif word.endswith("s") and len(word) > 3 and not word.endswith("ss"):
        variants.add(word[:-1])
    variants |= TERM_ALIASES.get(word, set())
    return variants


def tokenize(value):
    text = (value or "").strip().lower()
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", text)
    terms = set()
    for word in words:
        if word in MATCH_STOPWORDS:
            continue
        if len(word) >= 3 or word in SHORT_MATCH_TERMS:
            terms |= term_variants(word)
    for phrase, aliases in PHRASE_ALIASES.items():
        if phrase in text:
            terms |= aliases
    return terms


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


def candidate_workload(user, workload_by_user_id=None):
    workload_by_user_id = workload_by_user_id or {}
    try:
        return int(workload_by_user_id.get(user.id, 0) or 0)
    except (TypeError, ValueError):
        return 0


def rank_candidates(project, candidates, workload_by_user_id=None):
    project_terms = project_theme_terms(project)
    ranked = []
    for candidate in candidates:
        expertise_terms = candidate_profile_terms(candidate)
        matches = sorted(project_terms & expertise_terms)
        ranked.append(
            {
                "user": candidate,
                "score": len(matches),
                "matches": matches[:8],
                "workload_count": candidate_workload(candidate, workload_by_user_id),
            }
        )
    ranked.sort(key=lambda item: (item["workload_count"], -item["score"], item["user"].email))
    return ranked


def filter_ranked_matches(ranked):
    positive_matches = [item for item in ranked if item["score"] > 0]
    return positive_matches or ranked


def recommend_supervisors(project, supervisors, limit=SUPERVISOR_RECOMMENDATION_LIMIT, workload_by_user_id=None):
    return filter_ranked_matches(rank_candidates(project, supervisors, workload_by_user_id=workload_by_user_id))[:limit]


def recommend_assessors(
    project,
    examiners,
    excluded_user_ids=None,
    limit=ASSESSOR_RECOMMENDATION_LIMIT,
    workload_by_user_id=None,
):
    excluded_user_ids = set(excluded_user_ids or [])
    recommendations = []
    for item in filter_ranked_matches(rank_candidates(project, examiners, workload_by_user_id=workload_by_user_id)):
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
    supervisor_workload_by_user_id=None,
    assessor_workload_by_user_id=None,
):
    ranked_supervisors = recommend_supervisors(
        project,
        supervisors,
        limit=supervisor_limit,
        workload_by_user_id=supervisor_workload_by_user_id,
    )
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
        workload_by_user_id=assessor_workload_by_user_id,
    )

    return {
        "ranked_supervisors": ranked_supervisors,
        "ranked_examiners": ranked_examiners,
        "supervisor": supervisor_choice,
        "assessors": [item["user"] for item in ranked_examiners],
    }
