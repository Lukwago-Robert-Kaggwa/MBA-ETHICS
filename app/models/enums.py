import enum


class MbaRole(enum.Enum):
    MAIN_ADMIN = "main_admin"
    ADMIN = "admin"
    SCHOLAR = "scholar"
    STUDENT = "student"
    EXAMINER = "examiner"
    HDC = "hdc"


class MbaScholarRole(enum.Enum):
    EXAMINER = "examiner"
    SUPERVISOR = "supervisor"
    BOTH = "both"


class EthicsRole(enum.Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    REVIEWER = "reviewer"
    SUPERVISOR = "supervisor"
    STUDENT = "student"
    REC = "rec"
    DEAN = "dean"


class EthicsFormType(enum.Enum):
    FORM_A = "A"
    FORM_B = "B"
    FORM_C = "C"


class EthicsSubmissionStatus(enum.Enum):
    DRAFT = "draft"
    AWAITING_SUPERVISOR = "awaiting_supervisor"
    SENT_BACK_BY_SUPERVISOR = "sent_back_by_supervisor"
    AWAITING_ADMIN = "awaiting_admin"
    AWAITING_REVIEWERS = "awaiting_reviewers"
    REVIEW_IN_PROGRESS = "review_in_progress"
    SENT_BACK_BY_REVIEWER = "sent_back_by_reviewer"
    AWAITING_REC = "awaiting_rec"
    APPROVED = "approved"
    APPROVED_WITH_MINOR_CHANGES = "approved_with_minor_changes"
    RESUBMISSION_REQUIRED = "resubmission_required"
    REJECTED = "rejected"
    CERTIFICATE_ISSUED = "certificate_issued"


class ProjectStatus(enum.Enum):
    CREATED = "created"
    ADMIN_SUBMITTED = "admin_submitted"
    JBS5_SUBMITTED_TO_HDC = "jbs5_submitted_to_hdc"
    JBS5_HDC_APPROVED = "jbs5_hdc_approved"
    JBS5_HDC_DECLINED = "jbs5_hdc_declined"
    ADMIN_APPROVED = "admin_approved"
    ADMIN_DECLINED = "admin_declined"
    SUPERVISOR_ACCEPTED = "supervisor_accepted"
    HDC_VERIFIED = "hdc_verified"
    HDC_DECLINED = "hdc_declined"
    RESULTS_SUBMITTED_TO_HDC = "results_submitted_to_hdc"
    RESULTS_APPROVED = "results_approved"
    RESULTS_DECLINED = "results_declined"
    GRADUATED = "graduated"
