from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.orm import deferred
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from .enums import EthicsRole, EthicsSubmissionStatus, MbaRole, MbaScholarRole, ProjectStatus
from .helpers import normalize_email


class UserAuthMixin(UserMixin):
    system_name = None

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=True)
    microsoft_subject = db.Column(db.String(255), nullable=True, unique=True)
    first_name = db.Column(db.String(120), nullable=True)
    last_name = db.Column(db.String(120), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    popia_confirmed_at = db.Column(db.DateTime, nullable=True)
    popia_notice_version = db.Column(db.String(40), nullable=True)
    popia_confirmed_ip = db.Column(db.String(64), nullable=True)
    popia_confirmed_user_agent = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_id(self):
        return f"{self.system_name}:{self.id}"

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    @classmethod
    def find_by_email(cls, email):
        return cls.query.filter(db.func.lower(cls.email) == normalize_email(email)).first()


class MbaUser(UserAuthMixin, db.Model):
    __tablename__ = "mba_users"
    __table_args__ = (
        CheckConstraint("role in ('main_admin','admin','scholar','student','examiner','hdc')", name="mba_user_role_check"),
        CheckConstraint("scholar_role is null or scholar_role in ('examiner','supervisor','both')", name="mba_scholar_role_check"),
    )

    system_name = "mba"
    role = db.Column(db.String(40), nullable=False, default=MbaRole.STUDENT.value)
    scholar_role = db.Column(db.String(40), nullable=True)
    has_profile = db.Column(db.Boolean, nullable=False, default=False)
    has_signature = db.Column(db.Boolean, nullable=False, default=False)
    has_cv = db.Column(db.Boolean, nullable=False, default=False)

    def is_admin_role(self):
        return self.role in {MbaRole.MAIN_ADMIN.value, MbaRole.ADMIN.value}

    def is_student_role(self):
        return self.role == MbaRole.STUDENT.value

    def is_supervisor_role(self):
        return self.scholar_role in {MbaScholarRole.SUPERVISOR.value, MbaScholarRole.BOTH.value}

    def is_examiner_role(self):
        return self.role == MbaRole.EXAMINER.value or self.scholar_role in {
            MbaScholarRole.EXAMINER.value,
            MbaScholarRole.BOTH.value,
        }


class EthicsUser(UserAuthMixin, db.Model):
    __tablename__ = "ethcis_users"
    __table_args__ = (
        CheckConstraint(
            "role in ('super_admin','admin','reviewer','supervisor','student','rec','dean')",
            name="ethcis_user_role_check",
        ),
    )

    system_name = "ethics"
    role = db.Column(db.String(40), nullable=False, default=EthicsRole.STUDENT.value)
    student_number = db.Column(db.String(40), nullable=True, index=True)
    supervisor_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=True)
    supervisor = db.relationship("EthicsUser", remote_side="EthicsUser.id", foreign_keys=[supervisor_id])
    staff_number = db.Column(db.String(80), nullable=True)
    specialisation = db.Column(db.String(180), nullable=True)
    authenticated_student = db.Column(db.Boolean, nullable=False, default=False)
    watched_demo = db.Column(db.Boolean, nullable=False, default=False)

    def is_admin_role(self):
        return self.role in {EthicsRole.SUPER_ADMIN.value, EthicsRole.ADMIN.value}

    def is_committee_role(self):
        return self.role in {EthicsRole.REC.value, EthicsRole.DEAN.value}


class MbaStudentProfile(db.Model):
    __tablename__ = "mba_student_profiles"
    __table_args__ = (UniqueConstraint("student_number", name="uq_mba_student_number"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"), nullable=False, unique=True)
    user = db.relationship("MbaUser", backref=db.backref("student_profile", uselist=False))
    name = db.Column(db.String(120))
    surname = db.Column(db.String(120))
    title = db.Column(db.String(40))
    contact = db.Column(db.String(80))
    student_number = db.Column(db.String(40), nullable=True)
    secondary_email = db.Column(db.String(255))
    module = db.Column(db.String(120))
    block_id = db.Column(db.String(120))
    degree = db.Column(db.String(80), nullable=False, default="MBA")
    address = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class MbaScholarProfile(db.Model):
    __tablename__ = "mba_scholar_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"), nullable=False, unique=True)
    user = db.relationship("MbaUser", backref=db.backref("scholar_profile", uselist=False))
    name = db.Column(db.String(120))
    surname = db.Column(db.String(120))
    title = db.Column(db.String(40))
    skills = db.Column(db.Text)
    address = db.Column(db.Text)
    department = db.Column(db.String(160))
    position = db.Column(db.String(160))
    contact = db.Column(db.String(80))
    students = db.Column(db.Integer, nullable=False, default=0)
    qualification = db.Column(db.String(180))
    affiliation = db.Column(db.String(180))
    research_themes = db.Column(db.Text)
    research_interests = db.Column(db.Text)
    research_disciplines = db.Column(db.Text)
    academic_experience = db.Column(db.Integer, nullable=False, default=0)
    students_supervised_total = db.Column(db.Integer, nullable=False, default=0)
    students_assessed_total = db.Column(db.Integer, nullable=False, default=0)
    publication_count = db.Column(db.Integer, nullable=False, default=0)
    selected_publications = db.Column(db.Text)
    scholarly_profile_links = db.Column(db.Text)
    approved_before = db.Column(db.Boolean, nullable=False, default=False)
    international_assessor = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class MbaResearchInterest(db.Model):
    __tablename__ = "mba_research_interests"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    created_by = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class MbaDiscipline(db.Model):
    __tablename__ = "mba_disciplines"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class MbaProject(db.Model):
    __tablename__ = "mba_projects"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"), nullable=False)
    student = db.relationship("MbaUser", foreign_keys=[student_id], backref="projects")
    project_title = db.Column(db.String(220), nullable=False)
    project_description = db.Column(db.Text, nullable=False)
    discipline = db.Column(db.Text, nullable=False)
    discipline_id = db.Column(db.Integer, db.ForeignKey("mba_disciplines.id"), nullable=True)
    discipline_option = db.relationship("MbaDiscipline", foreign_keys=[discipline_id])
    qualification = db.Column(db.String(120))
    project_status = db.Column(db.String(60), nullable=False, default=ProjectStatus.CREATED.value)
    title_approved = db.Column(db.Boolean, nullable=False, default=False)
    nomination_form_approved = db.Column(db.Boolean, nullable=False, default=False)
    nomination_form_submitted = db.Column(db.Boolean, nullable=False, default=False)
    intent_form_approved = db.Column(db.Boolean, nullable=False, default=False)
    intent_form_submitted = db.Column(db.Boolean, nullable=False, default=False)
    primary_supervisor_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"))
    primary_supervisor = db.relationship("MbaUser", foreign_keys=[primary_supervisor_id])
    assignment_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    invitations_sent_at = db.Column(db.DateTime, nullable=True)
    primary_supervisor_invitation_status = db.Column(db.String(20), nullable=True)
    assessor_1_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"))
    assessor_1 = db.relationship("MbaUser", foreign_keys=[assessor_1_id])
    assessor_1_invitation_status = db.Column(db.String(20), nullable=True)
    assessor_1_invited_at = db.Column(db.DateTime, nullable=True)
    assessor_1_reminder_sent_at = db.Column(db.DateTime, nullable=True)
    assessor_1_hdc_decision = db.Column(db.String(20), nullable=True)
    assessor_1_hdc_decision_at = db.Column(db.DateTime, nullable=True)
    assessor_1_hdc_decision_assessor_id = db.Column(db.Integer, nullable=True)
    assessor_2_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"))
    assessor_2 = db.relationship("MbaUser", foreign_keys=[assessor_2_id])
    assessor_2_invitation_status = db.Column(db.String(20), nullable=True)
    assessor_2_invited_at = db.Column(db.DateTime, nullable=True)
    assessor_2_reminder_sent_at = db.Column(db.DateTime, nullable=True)
    assessor_2_hdc_decision = db.Column(db.String(20), nullable=True)
    assessor_2_hdc_decision_at = db.Column(db.DateTime, nullable=True)
    assessor_2_hdc_decision_assessor_id = db.Column(db.Integer, nullable=True)
    assessor_3_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"))
    assessor_3 = db.relationship("MbaUser", foreign_keys=[assessor_3_id])
    assessor_3_invitation_status = db.Column(db.String(20), nullable=True)
    assessor_3_invited_at = db.Column(db.DateTime, nullable=True)
    assessor_3_reminder_sent_at = db.Column(db.DateTime, nullable=True)
    comments = db.Column(db.Text)
    hdc_comments = db.Column(db.Text)
    jbs5_hdc_comments = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Track when supervisor accepts invitation for document visibility
    supervisor_accepted_at = db.Column(db.DateTime, nullable=True)
    supervisor_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    assessors_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    assessors_nominated_at = db.Column(db.DateTime, nullable=True)
    dissertation_released_to_assessors = db.Column(db.Boolean, nullable=False, default=False)
    dissertation_released_at = db.Column(db.DateTime, nullable=True)
    supervisor_pool_released_at = db.Column(db.DateTime, nullable=True)
    supervisor_pool_released_by_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"), nullable=True)
    dissertation_moodle_request_sent_at = db.Column(db.DateTime, nullable=True)
    dissertation_resubmission_requested_at = db.Column(db.DateTime, nullable=True)
    dissertation_resubmission_open = db.Column(db.Boolean, nullable=False, default=False)
    dissertation_resubmission_opened_at = db.Column(db.DateTime, nullable=True)
    additional_assessment_requested_at = db.Column(db.DateTime, nullable=True)
    corrections_requested_at = db.Column(db.DateTime, nullable=True)
    corrections_student_resubmitted_at = db.Column(db.DateTime, nullable=True)
    corrections_supervisor_approved_at = db.Column(db.DateTime, nullable=True)
    corrections_supervisor_comments = db.Column(db.Text, nullable=True)
    corrections_supervisor_rejected_at = db.Column(db.DateTime, nullable=True)
    corrections_supervisor_rejection_comments = db.Column(db.Text, nullable=True)
    assessment_results_forwarded_to_supervisor_at = db.Column(db.DateTime, nullable=True)
    corrections_released_to_student_at = db.Column(db.DateTime, nullable=True)
    module_completion_status = db.Column(db.String(60), nullable=False, default="not_checked")
    module_completion_marks_email = db.Column(db.String(255), nullable=True)
    module_completion_verification_token = db.Column(db.String(128), nullable=True, unique=True)
    module_completion_requested_at = db.Column(db.DateTime, nullable=True)
    module_completion_responded_at = db.Column(db.DateTime, nullable=True)
    module_completion_response = db.Column(db.String(10), nullable=True)
    jbs5_hdc_approved_at = db.Column(db.DateTime, nullable=True)
    results_submitted_to_hdc_at = db.Column(db.DateTime, nullable=True)
    results_hdc_decision = db.Column(db.String(20), nullable=True)
    results_hdc_reviewed_at = db.Column(db.DateTime, nullable=True)
    results_hdc_comments = db.Column(db.Text)
    results_released_to_supervisor_at = db.Column(db.DateTime, nullable=True)
    supervisor_title_change_requested_at = db.Column(db.DateTime, nullable=True)
    supervisor_title_change_request = db.Column(db.Text, nullable=True)
    supervisor_title_change_resolved_at = db.Column(db.DateTime, nullable=True)

    @property
    def can_confirm_assessors(self):
        # HDC must approve JBS5, then the student must submit JBS10 and Intent to Submit.
        student_doc_types = {
            doc.doc_type
            for doc in self.documents
            if doc.uploaded_by_id == self.student_id
        }
        hdc_declined_nomination = (
            self.project_status == ProjectStatus.HDC_DECLINED.value or
            self.assessor_1_hdc_decision == "declined" or
            self.assessor_2_hdc_decision == "declined"
        )
        assessor_revision_needed = (
            self.assessor_1_invitation_status == "declined" or
            self.assessor_2_invitation_status == "declined"
        )
        return (
            self.jbs5_hdc_approved_at is not None and
            'jbs10' in student_doc_types and
            'intent_to_submit' in student_doc_types and
            (self.supervisor_confirmed or self.supervisor_accepted_at is not None) and
            (not self.assessors_confirmed or hdc_declined_nomination or assessor_revision_needed) and
            (hdc_declined_nomination or assessor_revision_needed or not (
                self.assessor_1_invitation_status == "accepted" and
                self.assessor_2_invitation_status == "accepted"
            ))
        )

    @property
    def dissertation_pack_submitted(self):
        student_doc_types = {
            doc.doc_type
            for doc in self.documents
            if doc.uploaded_by_id == self.student_id
        }
        all_doc_types = {doc.doc_type for doc in self.documents}
        return {"dissertation"}.issubset(all_doc_types) and {
            "global_document",
            "combined_turnitin_ai_report",
        }.issubset(student_doc_types)

    @property
    def dissertation_pack_locked(self):
        return self.dissertation_pack_submitted and not self.dissertation_resubmission_open

    @property
    def discipline_name(self):
        if self.discipline_option and self.discipline_option.name:
            return self.discipline_option.name
        return self.discipline


class MbaProjectComment(db.Model):
    __tablename__ = "mba_project_comments"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("mba_projects.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"), nullable=False, index=True)
    comment = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    project = db.relationship(
        "MbaProject",
        backref=db.backref("project_comments", cascade="all, delete-orphan", order_by="MbaProjectComment.created_at"),
    )
    author = db.relationship("MbaUser", foreign_keys=[author_id])


class MbaReminderState(db.Model):
    __tablename__ = "mba_reminder_states"

    id = db.Column(db.Integer, primary_key=True)
    reminder_key = db.Column(db.String(255), nullable=False, unique=True, index=True)
    last_sent_at = db.Column(db.DateTime, nullable=True)
    last_sent_by_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"), nullable=True)
    last_sent_by = db.relationship("MbaUser", foreign_keys=[last_sent_by_id])
    dismissed_at = db.Column(db.DateTime, nullable=True)
    dismissed_by_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"), nullable=True)
    dismissed_by = db.relationship("MbaUser", foreign_keys=[dismissed_by_id])
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class MbaProjectDocument(db.Model):
    __tablename__ = "mba_project_documents"
    __table_args__ = (UniqueConstraint("project_id", "doc_type", name="uq_mba_project_document_type"),)

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("mba_projects.id"), nullable=False)
    project = db.relationship("MbaProject", backref="documents")
    doc_type = db.Column(db.String(60), nullable=False)  # e.g. 'jbs5' or 'supervisor_agreement'
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    file_data = deferred(db.Column(db.LargeBinary, nullable=True))
    mime_type = db.Column(db.String(120), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"), nullable=False)
    uploaded_by = db.relationship("MbaUser", foreign_keys=[uploaded_by_id])
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class MbaForm(db.Model):
    __tablename__ = "mba_forms"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("mba_projects.id"), nullable=False)
    project = db.relationship("MbaProject", backref="forms")
    form_type = db.Column(db.String(50), nullable=False)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    student_signed = db.Column(db.Boolean, nullable=False, default=False)
    supervisor_signed = db.Column(db.Boolean, nullable=False, default=False)
    submitted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class EthicsApplication(db.Model):
    __tablename__ = "ethcis_applications"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=False)
    student = db.relationship("EthicsUser", foreign_keys=[student_id], backref="applications")
    supervisor_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"))
    title = db.Column(db.String(220), nullable=False)
    summary = db.Column(db.Text, nullable=False)
    risk_level = db.Column(db.String(40), nullable=False, default="low")
    status = db.Column(db.String(60), nullable=False, default="draft")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class EthicsReview(db.Model):
    __tablename__ = "ethcis_reviews"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("ethcis_applications.id"), nullable=False)
    application = db.relationship("EthicsApplication", backref="reviews")
    reviewer_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=False)
    reviewer = db.relationship("EthicsUser", backref="reviews")
    recommendation = db.Column(db.String(80), nullable=False)
    comments = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class EthicsFormRequirement(db.Model):
    __tablename__ = "ethcis_form_requirements"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=False)
    student = db.relationship("EthicsUser", backref="form_requirements", foreign_keys=[student_id])
    form_type = db.Column(db.String(1), nullable=False)
    needs_permission = db.Column(db.Boolean, nullable=False, default=False)
    has_clearance = db.Column(db.Boolean, nullable=False, default=False)
    company_requires_jbs = db.Column(db.Boolean, nullable=False, default=False)
    has_ethics_evidence = db.Column(db.Boolean, nullable=False, default=False)
    proposal_filename = db.Column(db.String(255), nullable=True)
    permission_letter_filename = db.Column(db.String(255), nullable=True)
    research_tools_filename = db.Column(db.String(255), nullable=True)
    impact_assessment_filename = db.Column(db.String(255), nullable=True)
    participation_info_filename = db.Column(db.String(255), nullable=True)
    pending_note_filename = db.Column(db.String(255), nullable=True)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    submitted_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class EthicsFormSubmission(db.Model):
    __tablename__ = "ethcis_form_submissions"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=False)
    student = db.relationship("EthicsUser", backref="form_submissions", foreign_keys=[student_id])
    supervisor_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=True)
    supervisor = db.relationship("EthicsUser", foreign_keys=[supervisor_id])
    requirement_id = db.Column(db.Integer, db.ForeignKey("ethcis_form_requirements.id"), nullable=True)
    requirement = db.relationship("EthicsFormRequirement", backref="submissions")
    form_type = db.Column(db.String(1), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    summary = db.Column(db.Text, nullable=True)
    department = db.Column(db.String(160), nullable=True)
    degree = db.Column(db.String(120), nullable=True)
    risk_level = db.Column(db.String(40), nullable=False, default="low")
    status = db.Column(db.String(80), nullable=False, default=EthicsSubmissionStatus.DRAFT.value, index=True)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    submitted_at = db.Column(db.DateTime, nullable=True)
    supervisor_decision = db.Column(db.String(80), nullable=True)
    supervisor_comments = db.Column(db.Text, nullable=True)
    supervisor_reviewed_at = db.Column(db.DateTime, nullable=True)
    submitted_to_admin = db.Column(db.Boolean, nullable=False, default=False)
    submitted_to_reviewers = db.Column(db.Boolean, nullable=False, default=False)
    submitted_to_rec = db.Column(db.Boolean, nullable=False, default=False)
    rec_status = db.Column(db.String(80), nullable=True)
    rec_comments = db.Column(db.Text, nullable=True)
    rec_decided_at = db.Column(db.DateTime, nullable=True)
    certificate_code = db.Column(db.String(120), nullable=True)
    certificate_issued_at = db.Column(db.DateTime, nullable=True)
    certificate_valid_years = db.Column(db.Integer, nullable=True)
    certificate_end_date = db.Column(db.DateTime, nullable=True)
    certificate_issuer = db.Column(db.String(255), nullable=True)
    certificate_heading = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class EthicsSubmissionFile(db.Model):
    __tablename__ = "ethcis_submission_files"

    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("ethcis_form_submissions.id"), nullable=True, index=True)
    submission = db.relationship("EthicsFormSubmission", backref=db.backref("files", cascade="all, delete-orphan"))
    student_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=False, index=True)
    student = db.relationship("EthicsUser", foreign_keys=[student_id])
    field_name = db.Column(db.String(120), nullable=True)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False, unique=True, index=True)
    file_data = deferred(db.Column(db.LargeBinary, nullable=False))
    mime_type = db.Column(db.String(120), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class EthicsFormDraft(db.Model):
    __tablename__ = "ethcis_form_drafts"
    __table_args__ = (UniqueConstraint("student_id", "form_type", name="uq_ethcis_student_form_draft"),)

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=False)
    student = db.relationship("EthicsUser", backref="form_drafts", foreign_keys=[student_id])
    form_type = db.Column(db.String(1), nullable=False)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class EthicsReviewerAssignment(db.Model):
    __tablename__ = "ethcis_reviewer_assignments"
    __table_args__ = (UniqueConstraint("submission_id", "reviewer_id", name="uq_ethcis_submission_reviewer"),)

    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("ethcis_form_submissions.id"), nullable=False)
    submission = db.relationship("EthicsFormSubmission", backref="reviewer_assignments")
    reviewer_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=False)
    reviewer = db.relationship(
        "EthicsUser",
        backref="ethics_review_assignments",
        foreign_keys=[reviewer_id],
    )
    assigned_by_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=True)
    assigned_by = db.relationship("EthicsUser", foreign_keys=[assigned_by_id])
    recommendation = db.Column(db.String(80), nullable=True)
    comments = db.Column(db.Text, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class EthicsActivityLog(db.Model):
    __tablename__ = "ethcis_activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("ethcis_users.id"), nullable=False)
    user = db.relationship("EthicsUser", backref="activity_logs")
    action = db.Column(db.String(120), nullable=False)
    target_type = db.Column(db.String(80), nullable=True)
    target_id = db.Column(db.String(80), nullable=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class MbaProjectSupervisorInvitation(db.Model):
    __tablename__ = "mba_project_supervisor_invitations"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("mba_projects.id"), nullable=False)
    supervisor_id = db.Column(db.Integer, db.ForeignKey("mba_users.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending, accepted, declined, expired
    invited_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    reminder_sent_at = db.Column(db.DateTime, nullable=True)
    responded_at = db.Column(db.DateTime, nullable=True)
    project = db.relationship("MbaProject", back_populates="supervisor_invitations")
    supervisor = db.relationship("MbaUser")


MbaProject.supervisor_invitations = db.relationship(
    "MbaProjectSupervisorInvitation",
    back_populates="project",
    cascade="all, delete-orphan"
)
