"""Member contact lookup for notifications.

Notifications for members are queued with recipient "member:<id>". The worker
resolves that id to a phone number and email here, just before sending.

SUPPAI_MEMBER_CONTACTS_PROVIDER picks the source:
  memory    in-process dictionary (development and tests; starts empty)
  platform  the main SuppAI platform (not built yet, see PlatformContactSource)
"""

from dataclasses import dataclass

from app.config import get_settings

MEMBER_PREFIX = "member:"


@dataclass(frozen=True)
class Contact:
    phone: str | None = None
    email: str | None = None


class ContactNotFound(LookupError):
    pass


class MemberContactSource:
    def get(self, member_id: str) -> Contact | None:
        """Returns the member's contact details, or None if the member is unknown."""
        raise NotImplementedError


class InMemoryContactSource(MemberContactSource):
    def __init__(self, contacts: dict[str, Contact] | None = None) -> None:
        self._contacts = dict(contacts or {})

    def add(self, member_id: str, *, phone: str | None = None, email: str | None = None) -> None:
        self._contacts[str(member_id)] = Contact(phone=phone, email=email)

    def get(self, member_id: str) -> Contact | None:
        return self._contacts.get(str(member_id))


class PlatformContactSource(MemberContactSource):
    """Production adapter for member contacts held by the main SuppAI platform.

    TODO(production): implement get() against the real member data source.
    It has not been decided whether that is an internal API on the main SuppAI
    platform or a direct read of its member collection, so nothing is guessed
    here. Return None for an unknown member; raise for transient errors so the
    worker retries.
    """

    def get(self, member_id: str) -> Contact | None:
        raise NotImplementedError("Member contact lookup from the SuppAI platform is not built yet")


_source: MemberContactSource | None = None


def source() -> MemberContactSource:
    global _source
    if _source is None:
        name = get_settings().member_contacts_provider
        if name == "memory":
            _source = InMemoryContactSource()
        elif name == "platform":
            _source = PlatformContactSource()
        else:
            raise ValueError(f"Unknown SUPPAI_MEMBER_CONTACTS_PROVIDER {name!r}")
    return _source


def reset() -> None:
    """Drops the cached source so the next call reads the setting again."""
    global _source
    _source = None


def is_member_recipient(recipient: str | None) -> bool:
    return bool(recipient) and recipient.startswith(MEMBER_PREFIX)


def resolve_member(member_id: str) -> Contact:
    member_id = str(member_id).strip()
    if not member_id:
        raise ContactNotFound("Empty member id")
    contact = source().get(member_id)
    if contact is None or not (contact.phone or contact.email):
        raise ContactNotFound(f"No contact details for member {member_id}")
    return contact


def resolve_recipient(recipient: str) -> Contact:
    """Resolves "member:<id>" to contact details."""
    if not is_member_recipient(recipient):
        raise ValueError(f"Not a member recipient: {recipient!r}")
    return resolve_member(recipient.removeprefix(MEMBER_PREFIX))
