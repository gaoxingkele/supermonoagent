"""Read-only projections of daemon-owned runtime state.

The journal owns receipts and history. These projections neither persist data
nor confer authority to control a process.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal


TARGET = ('window_id', 'session_id', 'journal_id', 'process_generation')
_DIRECTORY = ('state', 'provider', 'provider_version', 'provider_root_id',
              'provider_root_confirmed', 'policy', 'last_outcome')
_ACTIVE = ('submission_id', 'provider_turn_id', 'cancel_version')
_PERMISSION = ('permission_request_id', 'submission_id', 'provider_turn_id', 'tool_name',
               'choices', 'requires_user_interaction', 'expires_at', 'state')
_RECEIPT = (*TARGET, 'nonce', 'submission_id', 'event_id', 'seq', 'state', 'dispatch_state')


def _fields(value, allowed):
    return {key: value[key] for key in allowed if key in value}


@dataclass(frozen=True, slots=True)
class ManagedRuntimeSnapshot:
    """One captured generation, with separate catalog, control, and event views."""

    source_revision: int | None
    _directory: dict = field(repr=False)
    _control: dict = field(repr=False)
    _event: dict = field(repr=False)
    runtime_verification: Literal['unverifiable'] | None = None

    @classmethod
    def capture(cls, target, *, state, provider, descriptor, active, permissions,
                last_outcome, source_revision=None, runtime_verification=None):
        return cls.from_status(dict(target, interaction_mode='managed', state=state,
            provider=provider, provider_version=descriptor.get('provider_version'),
            provider_root_id=descriptor.get('provider_root_id'),
            provider_root_confirmed=descriptor.get('confirmed', False), policy=descriptor.get('policy', {}),
            active_submission=active, permissions=list(permissions), last_outcome=last_outcome),
            source_revision=source_revision, runtime_verification=runtime_verification)

    @classmethod
    def from_status(cls, status, *, source_revision=None, runtime_verification=None):
        control = deepcopy(_fields(status, (*TARGET, *_DIRECTORY, 'interaction_mode', 'active_submission', 'permissions')))
        directory = _fields(control, _DIRECTORY)
        event = _fields(control, (*TARGET, *_DIRECTORY, 'interaction_mode'))
        active = control['active_submission']
        event['active_submission'] = _fields(active, _ACTIVE) if active else None
        event['permissions'] = [_fields(item, _PERMISSION) for item in control['permissions'] if item['state'] == 'pending']
        return cls(source_revision, directory, control, event, runtime_verification)

    def directory(self):
        return deepcopy(self._directory)

    def control(self, *, details=None, receipt=None, negotiated=False):
        result = deepcopy(self._control)
        if details is not None:
            for permission in result['permissions']:
                permission['details'] = details.get(permission['permission_request_id'], '')
        result['receipt'] = deepcopy(receipt)
        if negotiated and self.runtime_verification is not None:
            result['runtime_verification'] = self.runtime_verification
        return result

    def event(self):
        return deepcopy(self._event)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """Identity and restricted data for a single OP26 frame."""

    host_installation_id: str
    runtime_epoch: str
    revision: int
    target: dict = field(repr=False)
    public_status: dict | None = field(default=None, repr=False)
    accepted_receipt: dict | None = field(default=None, repr=False)

    @staticmethod
    def project_receipt(receipt):
        return _fields(receipt, _RECEIPT) if receipt and receipt.get('state') == 'accepted' else None

    def wire_payload(self):
        body = dict(self.target if self.public_status is None else self.public_status,
                    host_installation_id=self.host_installation_id, runtime_epoch=self.runtime_epoch,
                    revision=self.revision)
        if self.public_status is None:
            body['resync'] = True
        elif receipt := self.project_receipt(self.accepted_receipt):
            body['receipt'] = receipt
        return body


@dataclass(frozen=True, slots=True)
class WorkItemRecord:
    work_item_id: str
    bot_id: str
    status: Literal['proposed', 'awaiting_approval', 'queued', 'running', 'waiting_user', 'completed', 'failed', 'cancelled']
    created_at: float


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    worker_id: str
    bot_id: str
    work_item_id: str
    attempt_id: str
    repo: str
    base_commit: str
    branch: str
    worktree_path: str
    session_id: str | None
    process_generation: str | None
    controller_owner: str
    status: str
    created_at: float
