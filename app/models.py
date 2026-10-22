import enum
from datetime import datetime, date
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db


def now():
    return datetime.utcnow()


def cents(amount) -> int:
    """Convert a Decimal/str/float dollar amount to integer cents (banker-safe)."""
    if amount is None or amount == "":
        return 0
    from decimal import Decimal, ROUND_HALF_UP
    d = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not d.is_finite() or abs(d) > Decimal('21474836.47'):
        raise ValueError('Amount is not finite or exceeds supported precision.')
    return int(d * 100)


def money(cents_value: int) -> str:
    if cents_value is None:
        cents_value = 0
    sign = "-" if cents_value < 0 else ""
    cents_value = abs(cents_value)
    return f"{sign}{cents_value // 100:,}.{cents_value % 100:02d}"


class Role(str, enum.Enum):
    ADMIN = "admin"
    BOOKKEEPER = "bookkeeper"
    STAFF = "staff"
    VIEWER = "viewer"


class AccountType(str, enum.Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


DEBIT_NORMAL_TYPES = {AccountType.ASSET, AccountType.EXPENSE}


class ContactType(str, enum.Enum):
    CUSTOMER = "customer"
    VENDOR = "vendor"
    BOTH = "both"


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    SENT = "sent"
    PARTIAL = "partial"
    PAID = "paid"
    OVERDUE = "overdue"
    VOID = "void"


class BillStatus(str, enum.Enum):
    DRAFT = "draft"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    PARTIAL = "partial"
    PAID = "paid"
    VOID = "void"


class DocStatus(str, enum.Enum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    FAILED = "failed"
    NONE = "none"


class ClaimStatus(str, enum.Enum):
    DRAFT = "draft"                    # staff still editing
    SUBMITTED = "submitted"           # sent for processing
    AI_REVIEWED = "ai_reviewed"       # AI has read the receipts and suggested categories
    PENDING_APPROVAL = "pending_approval"
    CLARIFICATION = "clarification"   # admin asked the staffer for more info
    APPROVED = "approved"             # posted to the ledger, awaiting reimbursement
    REJECTED = "rejected"
    PAID = "paid"                     # reimbursed


# Statuses at which a claim is still owned/editable by the submitting staffer.
CLAIM_EDITABLE_STATUSES = {ClaimStatus.DRAFT.value, ClaimStatus.CLARIFICATION.value}


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=Role.VIEWER.value)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw, method="pbkdf2:sha256")

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    def is_admin(self):
        return self.role == Role.ADMIN.value

    def can_edit(self):
        return self.role in (Role.ADMIN.value, Role.BOOKKEEPER.value)

    def is_staff_only(self):
        """A claimant with no bookkeeping access — sees only their own expense claims."""
        return self.role == Role.STAFF.value

    def can_approve_claims(self):
        return self.role in (Role.ADMIN.value, Role.BOOKKEEPER.value)

    @property
    def is_active(self):
        return bool(self.active)


class CompanySettings(db.Model):
    __tablename__ = "company_settings"
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(200), default="Prism Group International Limited")
    legal_address = db.Column(db.Text, default="")
    tax_id = db.Column(db.String(60), default="")
    base_currency = db.Column(db.String(3), default="HKD")
    fiscal_year_start_month = db.Column(db.Integer, default=1)
    invoice_prefix = db.Column(db.String(20), default="INV-")
    next_invoice_seq = db.Column(db.Integer, default=1)
    bill_prefix = db.Column(db.String(20), default="BILL-")
    next_bill_seq = db.Column(db.Integer, default=1)
    logo_path = db.Column(db.String(255), default="")


class Currency(db.Model):
    __tablename__ = "currencies"
    code = db.Column(db.String(3), primary_key=True)
    name = db.Column(db.String(60))
    symbol = db.Column(db.String(5))


class ExchangeRate(db.Model):
    __tablename__ = "exchange_rates"
    id = db.Column(db.Integer, primary_key=True)
    currency_code = db.Column(db.String(3), db.ForeignKey("currencies.code"))
    rate_to_base = db.Column(db.Float, nullable=False)  # 1 unit of currency = X base currency
    as_of = db.Column(db.Date, default=date.today)


class CostCenter(db.Model):
    __tablename__ = "cost_centers"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(120))
    active = db.Column(db.Boolean, default=True)


class TaxRate(db.Model):
    __tablename__ = "tax_rates"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(60))
    rate = db.Column(db.Float, default=0.0)  # percent, e.g. 5.0 for 5%
    active = db.Column(db.Boolean, default=True)


class Account(db.Model):
    __tablename__ = "accounts"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(150), nullable=False)
    type = db.Column(db.String(20), nullable=False)
    subtype = db.Column(db.String(30), nullable=True)  # e.g. 'cogs' within expense
    description = db.Column(db.String(255), default="")
    active = db.Column(db.Boolean, default=True)
    is_bank = db.Column(db.Boolean, default=False)
    system_locked = db.Column(db.Boolean, default=False)  # can't delete

    def balance_cents(self, as_of=None, cost_center_id=None):
        from sqlalchemy import func
        q = db.session.query(
            func.coalesce(func.sum(JournalLine.debit_cents), 0),
            func.coalesce(func.sum(JournalLine.credit_cents), 0),
        ).join(JournalEntry).filter(
            JournalLine.account_id == self.id,
            JournalEntry.is_posted == True,  # noqa: E712
        )
        if as_of:
            q = q.filter(JournalEntry.date <= as_of)
        if cost_center_id:
            q = q.filter(JournalLine.cost_center_id == cost_center_id)
        debit, credit = q.one()
        if self.type in (AccountType.ASSET.value, AccountType.EXPENSE.value):
            return debit - credit
        return credit - debit


class Contact(db.Model):
    __tablename__ = "contacts"
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(20), default=ContactType.CUSTOMER.value)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(200), default="")
    phone = db.Column(db.String(60), default="")
    address = db.Column(db.Text, default="")
    currency_code = db.Column(db.String(3), default="HKD")
    tax_id = db.Column(db.String(60), default="")
    notes = db.Column(db.Text, default="")
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)


class Document(db.Model):
    __tablename__ = "documents"
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255))
    filepath = db.Column(db.String(500))
    mimetype = db.Column(db.String(100))
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_at = db.Column(db.DateTime, default=now)
    extraction_status = db.Column(db.String(20), default=DocStatus.NONE.value)
    extracted_json = db.Column(db.Text, default="")
    extraction_error = db.Column(db.Text, default="")
    bill_id = db.Column(db.Integer, db.ForeignKey("bills.id"), nullable=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("expense_claims.id"), nullable=True)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey('journal_entries.id'), nullable=True)
    sha256 = db.Column(db.String(64), nullable=True)
    drive_file_id = db.Column(db.String(200), nullable=True)


class JournalEntry(db.Model):
    __tablename__ = "journal_entries"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, default=date.today, nullable=False)
    memo = db.Column(db.String(255), default="")
    reference = db.Column(db.String(100), default="")
    source_type = db.Column(db.String(30), default="manual")  # invoice/bill/payment/manual/bank
    source_id = db.Column(db.Integer, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=now)
    is_posted = db.Column(db.Boolean, default=True)

    lines = db.relationship("JournalLine", backref="entry", cascade="all, delete-orphan")

    def total_debit(self):
        return sum(l.debit_cents for l in self.lines)

    def total_credit(self):
        return sum(l.credit_cents for l in self.lines)

    def is_balanced(self):
        return self.total_debit() == self.total_credit()


class JournalLine(db.Model):
    __tablename__ = "journal_lines"
    id = db.Column(db.Integer, primary_key=True)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=True)
    description = db.Column(db.String(255), default="")
    debit_cents = db.Column(db.Integer, default=0)
    credit_cents = db.Column(db.Integer, default=0)

    account = db.relationship("Account")
    cost_center = db.relationship("CostCenter")


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=False)
    issue_date = db.Column(db.Date, default=date.today)
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), default=InvoiceStatus.DRAFT.value)
    currency_code = db.Column(db.String(3), default="HKD")
    fx_rate = db.Column(db.Float, default=1.0)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    subtotal_cents = db.Column(db.Integer, default=0)
    tax_cents = db.Column(db.Integer, default=0)
    total_cents = db.Column(db.Integer, default=0)
    amount_paid_cents = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text, default="")
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    recurring_rule_id = db.Column(db.Integer, db.ForeignKey("recurring_invoices.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=now)

    contact = db.relationship("Contact")
    lines = db.relationship("InvoiceLine", backref="invoice", cascade="all, delete-orphan")
    cost_center = db.relationship("CostCenter")

    def balance_due_cents(self):
        return self.total_cents - self.amount_paid_cents


class InvoiceLine(db.Model):
    __tablename__ = "invoice_lines"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    description = db.Column(db.String(255))
    quantity = db.Column(db.Float, default=1.0)
    unit_price_cents = db.Column(db.Integer, default=0)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    tax_rate_id = db.Column(db.Integer, db.ForeignKey("tax_rates.id"), nullable=True)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    amount_cents = db.Column(db.Integer, default=0)

    account = db.relationship("Account")
    tax_rate = db.relationship("TaxRate")


class RecurringInvoice(db.Model):
    __tablename__ = "recurring_invoices"
    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"))
    frequency = db.Column(db.String(20), default="monthly")  # weekly/monthly/quarterly/annual
    next_run_date = db.Column(db.Date)
    active = db.Column(db.Boolean, default=True)
    template_json = db.Column(db.Text)  # lines, notes, due terms
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    currency_code = db.Column(db.String(3), default="HKD")
    due_days = db.Column(db.Integer, default=14)

    contact = db.relationship("Contact")


class Bill(db.Model):
    __tablename__ = "bills"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True)
    vendor_ref = db.Column(db.String(80), default="")
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=True)
    bill_date = db.Column(db.Date, default=date.today)
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), default=BillStatus.DRAFT.value)
    currency_code = db.Column(db.String(3), default="HKD")
    fx_rate = db.Column(db.Float, default=1.0)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    subtotal_cents = db.Column(db.Integer, default=0)
    tax_cents = db.Column(db.Integer, default=0)
    total_cents = db.Column(db.Integer, default=0)
    amount_paid_cents = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text, default="")
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=now)

    contact = db.relationship("Contact")
    lines = db.relationship("BillLine", backref="bill", cascade="all, delete-orphan")
    documents = db.relationship("Document", backref="bill")
    cost_center = db.relationship("CostCenter")

    def balance_due_cents(self):
        return self.total_cents - self.amount_paid_cents


class BillLine(db.Model):
    __tablename__ = "bill_lines"
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("bills.id"), nullable=False)
    description = db.Column(db.String(255))
    quantity = db.Column(db.Float, default=1.0)
    unit_price_cents = db.Column(db.Integer, default=0)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    tax_rate_id = db.Column(db.Integer, db.ForeignKey("tax_rates.id"), nullable=True)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    amount_cents = db.Column(db.Integer, default=0)
    category_guess = db.Column(db.String(120), default="")

    account = db.relationship("Account")
    tax_rate = db.relationship("TaxRate")


class BankAccount(db.Model):
    __tablename__ = "bank_accounts"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150))
    account_number = db.Column(db.String(80), default="")
    currency_code = db.Column(db.String(3), default="HKD")
    gl_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"))
    opening_balance_cents = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)

    gl_account = db.relationship("Account")


class BankTransaction(db.Model):
    __tablename__ = "bank_transactions"
    id = db.Column(db.Integer, primary_key=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_accounts.id"))
    date = db.Column(db.Date)
    description = db.Column(db.String(255))
    amount_cents = db.Column(db.Integer)  # signed: + inflow, - outflow
    import_batch = db.Column(db.String(60), default="")
    status = db.Column(db.String(20), default="unmatched")  # unmatched/matched/reconciled
    matched_journal_line_id = db.Column(db.Integer, db.ForeignKey("journal_lines.id"), nullable=True)
    source_key = db.Column(db.String(128), nullable=True)

    bank_account = db.relationship("BankAccount")


class LedgerControl(db.Model):
    __tablename__ = 'ledger_control'
    id = db.Column(db.Integer, primary_key=True)
    locked_through = db.Column(db.Date, nullable=True)


class MutationReceipt(db.Model):
    __tablename__ = 'mutation_receipts'
    key = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)
    endpoint = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=now)


class SampleDataBundle(db.Model):
    __tablename__ = 'sample_data_bundles'
    key = db.Column(db.String(80), primary_key=True)
    status = db.Column(db.String(20), nullable=False)
    manifest_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now)


class JournalReversal(db.Model):
    __tablename__ = 'journal_reversals'
    original_id = db.Column(db.Integer, db.ForeignKey('journal_entries.id'), primary_key=True)
    reversal_id = db.Column(db.Integer, db.ForeignKey('journal_entries.id'), unique=True, nullable=False)
    reason = db.Column(db.String(500), nullable=False)


class BankImport(db.Model):
    __tablename__ = 'bank_imports'
    id = db.Column(db.Integer, primary_key=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey('bank_accounts.id'), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    filepath = db.Column(db.String(500), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=now)
    __table_args__ = (db.UniqueConstraint('bank_account_id', 'sha256', name='uq_bank_import'),)


class IntegrationReview(db.Model):
    __tablename__ = 'integration_reviews'
    id = db.Column(db.Integer, primary_key=True)
    integration_id = db.Column(db.Integer, db.ForeignKey('sales_integrations.id'), nullable=False)
    external_id = db.Column(db.String(120), nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    detail = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='pending')
    journal_entry_id = db.Column(db.Integer, db.ForeignKey('journal_entries.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=now)
    __table_args__ = (db.UniqueConstraint('integration_id', 'external_id', 'payload_hash', name='uq_integration_review'),)


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    direction = db.Column(db.String(3))  # "in" or "out"
    contact_id = db.Column(db.Integer, db.ForeignKey("contacts.id"), nullable=True)
    date = db.Column(db.Date, default=date.today)
    amount_cents = db.Column(db.Integer)
    currency_code = db.Column(db.String(3), default="HKD")
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_accounts.id"))
    method = db.Column(db.String(40), default="")
    reference = db.Column(db.String(100), default="")
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=now)

    contact = db.relationship("Contact")
    bank_account = db.relationship("BankAccount")
    allocations = db.relationship("PaymentAllocation", backref="payment", cascade="all, delete-orphan")


class PaymentAllocation(db.Model):
    __tablename__ = "payment_allocations"
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"), nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("bills.id"), nullable=True)
    amount_cents = db.Column(db.Integer)

    invoice = db.relationship("Invoice")
    bill = db.relationship("Bill")


class Budget(db.Model):
    __tablename__ = "budgets"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    period = db.Column(db.String(7))  # "YYYY-MM"
    amount_cents = db.Column(db.Integer, default=0)

    account = db.relationship("Account")
    cost_center = db.relationship("CostCenter")


class ExpenseClaim(db.Model):
    """A staff expense/reimbursement claim: one or more out-of-pocket expenses
    that flow through Draft -> Submitted -> AI Reviewed -> Pending Approval ->
    Approved/Rejected -> Paid."""
    __tablename__ = "expense_claims"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(200), default="")
    business_purpose = db.Column(db.Text, default="")
    claim_date = db.Column(db.Date, default=date.today)
    currency_code = db.Column(db.String(3), default="HKD")
    fx_rate = db.Column(db.Float, default=1.0)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    status = db.Column(db.String(30), default=ClaimStatus.DRAFT.value)

    subtotal_cents = db.Column(db.Integer, default=0)
    tax_cents = db.Column(db.Integer, default=0)
    total_cents = db.Column(db.Integer, default=0)
    amount_paid_cents = db.Column(db.Integer, default=0)

    ai_confidence = db.Column(db.Float, nullable=True)      # 0..1 overall
    ai_summary = db.Column(db.Text, default="")

    reviewer_notes = db.Column(db.Text, default="")         # clarification request / rejection reason
    submitted_at = db.Column(db.DateTime, nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)

    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    payment_journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=now)

    staff = db.relationship("User", foreign_keys=[staff_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_id])
    cost_center = db.relationship("CostCenter")
    lines = db.relationship("ExpenseClaimLine", backref="claim", cascade="all, delete-orphan")
    documents = db.relationship("Document", backref="claim")

    def balance_due_cents(self):
        return self.total_cents - self.amount_paid_cents

    def recalc_totals(self):
        self.subtotal_cents = sum(l.amount_cents for l in self.lines)
        self.tax_cents = sum(l.tax_cents() for l in self.lines)
        self.total_cents = self.subtotal_cents + self.tax_cents

    def has_receipt(self):
        return len(self.documents) > 0

    def min_confidence(self):
        confidences = [l.ai_confidence for l in self.lines if l.ai_confidence is not None]
        return min(confidences) if confidences else None


class ExpenseClaimLine(db.Model):
    __tablename__ = "expense_claim_lines"
    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("expense_claims.id"), nullable=False)
    expense_date = db.Column(db.Date, nullable=True)
    merchant = db.Column(db.String(200), default="")
    description = db.Column(db.String(255), default="")
    amount_cents = db.Column(db.Integer, default=0)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    tax_rate_id = db.Column(db.Integer, db.ForeignKey("tax_rates.id"), nullable=True)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    category_guess = db.Column(db.String(120), default="")   # plain-English AI guess
    ai_confidence = db.Column(db.Float, nullable=True)        # 0..1 for the account suggestion

    account = db.relationship("Account")
    tax_rate = db.relationship("TaxRate")
    cost_center = db.relationship("CostCenter")

    def tax_cents(self):
        if self.tax_rate and self.tax_rate.rate:
            return round(self.amount_cents * (self.tax_rate.rate / 100))
        return 0


class AuditLog(db.Model):
    """Append-only trail of who did what and when across the system."""
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, default=now, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    user_name = db.Column(db.String(120), default="")   # denormalised so history survives user deletion
    action = db.Column(db.String(40))                   # create/update/submit/approve/reject/clarify/pay/void/login
    entity_type = db.Column(db.String(40))              # claim/bill/invoice/payment/user/auth
    entity_id = db.Column(db.Integer, nullable=True)
    summary = db.Column(db.String(400), default="")
    detail = db.Column(db.Text, default="")             # optional JSON of before/after

    user = db.relationship("User")


class SalesIntegration(db.Model):
    """Connection to an external sales channel (Shopify today; Stripe/others later).

    Holds the credentials and the accounting choices (FX rate, optional processor
    fee %, and which GL accounts to post to) used when importing orders as
    balanced journal entries.
    """
    __tablename__ = "sales_integrations"
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(20), default="shopify")     # shopify | stripe
    name = db.Column(db.String(120), default="")               # friendly label, e.g. "Lunara"
    shop_domain = db.Column(db.String(200), default="")        # e.g. lunara-global.com or *.myshopify.com
    access_token = db.Column(db.String(255), default="")       # Admin API access token (shpat_...)
    api_version = db.Column(db.String(20), default="2024-10")
    store_currency = db.Column(db.String(3), default="USD")
    fx_rate = db.Column(db.Float, default=1.0)                 # 1 store-currency unit = fx_rate base units
    fee_percent = db.Column(db.Float, default=0.0)            # optional processor fee % to accrue
    # GL account codes the sync posts to (defaults match the seeded chart)
    clearing_code = db.Column(db.String(20), default="1030")
    revenue_code = db.Column(db.String(20), default="4000")
    shipping_code = db.Column(db.String(20), default="4100")
    discount_code = db.Column(db.String(20), default="4950")
    tax_code = db.Column(db.String(20), default="2200")
    fee_code = db.Column(db.String(20), default="5100")
    cost_center_id = db.Column(db.Integer, db.ForeignKey("cost_centers.id"), nullable=True)
    active = db.Column(db.Boolean, default=True)
    last_sync_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=now)

    cost_center = db.relationship("CostCenter")

    def masked_token(self):
        t = self.access_token or ""
        return (t[:6] + "…" + t[-4:]) if len(t) > 12 else ("set" if t else "")


class SyncedOrder(db.Model):
    """One imported external order -> one journal entry. The unique (integration,
    external_id) key makes re-syncing a date range safe: an order already booked
    is skipped, never double-posted."""
    __tablename__ = "synced_orders"
    id = db.Column(db.Integer, primary_key=True)
    integration_id = db.Column(db.Integer, db.ForeignKey("sales_integrations.id"), nullable=False)
    external_id = db.Column(db.String(120), nullable=False)   # Shopify order GID
    order_number = db.Column(db.String(40), default="")       # e.g. #1001
    order_date = db.Column(db.Date, nullable=True)
    currency = db.Column(db.String(3), default="")
    gross_cents = db.Column(db.Integer, default=0)            # order total in base currency
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    synced_at = db.Column(db.DateTime, default=now)
    source_snapshot = db.Column(db.Text, nullable=True)

    integration = db.relationship("SalesIntegration")
    journal_entry = db.relationship("JournalEntry")

    __table_args__ = (db.UniqueConstraint("integration_id", "external_id", name="uq_synced_order"),)


class CategoryRule(db.Model):
    """Learned mapping from a keyword (merchant / description token) to a GL
    account, reinforced each time a human approves a categorisation. Drives the
    AI's confidence and lets suggestions improve over time."""
    __tablename__ = "category_rules"
    id = db.Column(db.Integer, primary_key=True)
    keyword = db.Column(db.String(120), index=True)    # lower-cased
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    hits = db.Column(db.Integer, default=1)
    updated_at = db.Column(db.DateTime, default=now, onupdate=now)

    account = db.relationship("Account")

    __table_args__ = (db.UniqueConstraint("keyword", "account_id", name="uq_category_rule"),)
