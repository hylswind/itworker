"""Build a Route 53 Domains ContactDetail from the registration contact the
workflow passed in (a JSON object surfaced on SetupConfig.contact). All fields come
from the operator's workflow input — nothing is read from the account."""

from __future__ import annotations

# Route 53 Domains rejects a RegisterDomain whose contacts miss any of these.
REQUIRED = ("FirstName", "LastName", "AddressLine1", "City",
            "CountryCode", "ZipCode", "PhoneNumber", "Email")
# Passed straight through if present.
OPTIONAL = ("AddressLine2", "State", "OrganizationName", "ContactType", "Fax")


class ContactError(Exception):
    pass


def build_contact(contact: dict) -> dict:
    """Validate and normalize into a ContactDetail dict. ContactType defaults to
    PERSON; PhoneNumber must already be in Route 53's `+1.2025551234` form (the
    operator's responsibility) — we don't reformat it."""
    missing = [k for k in REQUIRED if not contact.get(k)]
    if missing:
        raise ContactError(f"registration contact missing fields: {missing}")
    detail = {k: contact[k] for k in REQUIRED}
    for k in OPTIONAL:
        if contact.get(k):
            detail[k] = contact[k]
    detail.setdefault("ContactType", "PERSON")
    return detail
