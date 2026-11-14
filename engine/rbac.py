"""Role-based access control.

Every user has one role. Every action in the app names a permission, and the server checks it on every request
(not just in the page): a hidden button is a convenience, the check is the control.

Separation of duties follows the Technical Proposal: Magic SMEs (a vendor responsibility per UMGC) own accuracy;
UMGC roles own UMGC approval gates and material changes; UMGC Reviewers can read and comment but change nothing.
"""
from __future__ import annotations

ADMIN = "Administrator"
ROLES = [
    ADMIN,
    "Magic Project Manager",
    "Magic Learning Experience Architect",
    "Magic SME (vendor-provided)",
    "Magic Instructional Designer",
    "Magic Accessibility Specialist",
    "Magic Editor",
    "Magic Content Quality Owner",
    "Magic AI Governance Lead",
    "UMGC Program Sponsor",
    "UMGC Program Lead",
    "UMGC Reviewer",
]

PM, LXA, SME, ID, A11Y, ED, CQO, GOV = ROLES[1:9]
U_SPONSOR, U_LEAD, U_REVIEW = ROLES[9:12]
MAGIC = [PM, LXA, SME, ID, A11Y, ED, CQO, GOV]
UMGC_APPROVERS = [U_SPONSOR, U_LEAD]

# permission -> roles that hold it (Administrator holds every permission)
PERMS: dict[str, list[str]] = {
    "program.view":       ROLES,
    "comment":            ROLES,
    "program.create":     [PM, LXA],
    "program.setup":      [PM, LXA],
    "program.pause":      [PM],
    "source.upload":      MAGIC + [U_LEAD],
    "source.approve":     [SME, LXA],
    "skills.edit":        [SME, LXA],
    "skills.reopen":      [LXA, U_LEAD, U_SPONSOR],
    "employers.record":   [LXA],
    "blueprint.edit":     [ID, LXA],
    "blueprint.reopen":   [LXA, U_LEAD, U_SPONSOR],
    "outline.edit":       [ID, SME, LXA],
    "outline.approve":    [ID, LXA],
    "content.edit":       [ID, SME, ED],
    "owners.assign":      [PM, LXA],
    "team.edit":          [PM, LXA],
    "gate.umgc":          UMGC_APPROVERS,
    "gate.magic":         [LXA],
    "gate.val1":          [SME],
    "gate.val2":          [ID, LXA],
    "gate.val3":          [A11Y],
    "gate.val4":          [ID, A11Y],
    "gate.val5":          [ED, PM],
    "ticket.routine":     [PM, LXA, ID],
    "ticket.material":    UMGC_APPROVERS,
    "event.raise":        MAGIC + [U_LEAD],
    "refresh.run":        [LXA, PM],
    "jobs.retry":         [PM],
    "sample.score":       [CQO],
    "a11y.record":        [A11Y],
    "governance.edit":    [GOV],
    "incident.manage":    [GOV, CQO],
    "deliverable.submit": [PM],
    "deliverable.accept": UMGC_APPROVERS,
    "docs.generate":      [PM, LXA, ID],
    "demographics.config": [PM, U_LEAD],
    "autopilot":          [],          # Administrator only (demo fast-forward)
    "users.manage":       [],          # Administrator only
}

LABELS = {
    "program.view": "View Programs, records and downloads", "comment": "Comment and reply",
    "program.create": "Create a Program", "program.setup": "Edit Program set-up (duration, occupation, credential)",
    "program.pause": "Pause or resume a Program", "source.upload": "Upload or paste source material",
    "source.approve": "Approve a source for AI grounding", "skills.edit": "Edit skills and competencies",
    "skills.reopen": "Reopen an approved skills map", "employers.record": "Record employer validation",
    "blueprint.edit": "Edit the Blueprint", "blueprint.reopen": "Reopen an approved Blueprint",
    "outline.edit": "Edit the Module outline", "outline.approve": "Approve or reopen the Module outline",
    "content.edit": "Write and submit Modules and assessments", "owners.assign": "Assign asset owners",
    "team.edit": "Edit the Program team profile", "gate.umgc": "Decide UMGC approval gates",
    "gate.magic": "Decide Magic gates (employer validation)", "gate.val1": "Validation gate 1: SME accuracy",
    "gate.val2": "Validation gate 2: Instructional fit", "gate.val3": "Validation gate 3: Accessibility",
    "gate.val4": "Validation gate 4: Bias and appropriateness", "gate.val5": "Validation gate 5: Final editorial",
    "ticket.routine": "Approve routine change tickets", "ticket.material": "Approve material change tickets",
    "event.raise": "Raise an event-triggered ticket", "refresh.run": "Run the Lightcast refresh",
    "jobs.retry": "Retry failed jobs", "sample.score": "Score sampled AI-assisted assets",
    "a11y.record": "Record assistive technology tests and limitations", "governance.edit": "Manage models, prompts and evaluation sets",
    "incident.manage": "Report and resolve AI incidents", "deliverable.submit": "Submit deliverables to UMGC",
    "deliverable.accept": "Accept or return deliverables", "docs.generate": "Generate guides, packets and reports",
    "demographics.config": "Configure learner attributes", "autopilot": "Demo autopilot (fast-forward gates)", "users.manage": "Manage users and roles",
}


def can(role: str | None, perm: str) -> bool:
    if not role:
        return False
    if role == ADMIN:
        return True
    return role in PERMS.get(perm, [])


def gate_perm(kind: str, gate_no: int | None) -> str:
    if kind == "umgc":
        return "gate.umgc"
    if kind == "magic":
        return "gate.magic"
    if kind == "outline":
        return "outline.approve"
    return f"gate.val{gate_no}"


def org_of(role: str) -> str:
    return "UMGC" if role.startswith("UMGC") else "Magic"
