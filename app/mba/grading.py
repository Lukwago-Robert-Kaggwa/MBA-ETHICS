"""
Grading logic for MBA assessor grade calculations.
All grading rules live here so they can be adjusted without touching routes.
"""

GRADE_SCALE = 100

# (minimum_score, label) - first match wins (descending order)
CLASSIFICATIONS = [
    (75, "Distinction"),
    (65, "Merit"),
    (50, "Pass"),
    (0,  "Fail"),
]

ASSESSOR_SLOTS = ["assessor_1", "assessor_2", "assessor_3"]


def get_assessor_grades(project_id, forms_by_project):
    """Return list of (slot, grade_int) tuples for assessors who submitted a grade."""
    proj_forms = forms_by_project.get(project_id, {})
    results = []
    for slot in ASSESSOR_SLOTS:
        form_type = f"assessment_result_{slot}"
        form = proj_forms.get(form_type)
        if form and form.payload:
            try:
                grade = int(form.payload.get("grade", ""))
                if 0 <= grade <= GRADE_SCALE:
                    results.append((slot, grade))
            except (ValueError, TypeError):
                pass
    return results


def compute_final_grade(grades):
    """
    Average of all submitted assessor grades (list of ints).
    Returns None if no grades submitted yet.
    Returns a float rounded to one decimal place.
    """
    if not grades:
        return None
    return round(sum(grades) / len(grades), 1)


def grade_as_percentage(grade):
    """Express grade as a percentage string, e.g. 72.5 -> '72.5%'."""
    if grade is None:
        return None
    return f"{grade:.1f}%"


def grade_classification(grade):
    """Return a classification label for a numeric grade out of GRADE_SCALE."""
    if grade is None:
        return ""
    for threshold, label in CLASSIFICATIONS:
        if grade >= threshold:
            return label
    return "Fail"


def project_grade_summary(project_id, forms_by_project):
    """
    Compute grade summary for a single project.

    Returns a dict:
      grades          [(slot, grade_int), ...]   one entry per submitted assessor
      count           int                        number of grades received
      final           float | None               average grade, or None
      percentage      str | None                 e.g. "72.5%"
      classification  str                        e.g. "Merit", or ""
    """
    grades_with_slots = get_assessor_grades(project_id, forms_by_project)
    grade_values = [g for _, g in grades_with_slots]
    final = compute_final_grade(grade_values)
    return {
        "grades": grades_with_slots,
        "count": len(grade_values),
        "final": final,
        "percentage": grade_as_percentage(final),
        "classification": grade_classification(final),
    }
