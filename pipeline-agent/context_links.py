"""Discover indirect CRM context without silently assigning it to a deal."""
from __future__ import annotations

import hashlib
import json


def content_hash(properties):
    return hashlib.sha256(json.dumps(properties, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def collect_indirect(deal_id, contact_ids, company_ids, direct, *, associate, read, properties,
                     confirmations=()):
    """Injected read functions keep this collector offline-testable.

    Company contacts are traversed even if absent from the deal. An activity linked
    to another deal is excluded. An unlinked activity requires a human confirmation
    bound to its current content, never a company-name or keyword match.
    """
    contacts = set(contact_ids)
    company_contacts = associate("company", "contact", sorted(company_ids))
    for ids in company_contacts.values():
        contacts.update(ids)
    confirmed = {}
    for item in confirmations:
        if not isinstance(item, dict) or set(item) != {"deal_id", "source_id", "content_hash", "reason"}:
            raise ValueError("Context confirmations need deal_id, source_id, content_hash, reason")
        if str(item["deal_id"]) != str(deal_id):
            continue
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ValueError("Explain the confirmed context association")
        if item["source_id"] in confirmed:
            raise ValueError("Duplicate context confirmation")
        confirmed[item["source_id"]] = item
    merged = {kind: list(items) for kind, items in direct.items()}
    candidates, excluded = [], 0
    for kind, props in properties.items():
        via = {}
        for object_type, ids in (("contact", sorted(contacts)), ("company", sorted(company_ids))):
            associations = associate(object_type, kind, ids)
            for parent, event_ids in associations.items():
                for event_id in event_ids:
                    via.setdefault(event_id, []).append(f"{object_type}:{parent}")
        direct_ids = {str(item["_source_id"]) for item in direct.get(kind, [])}
        ids = sorted(set(via) - direct_ids)
        if not ids:
            continue
        linked_deals = associate(kind, "deal", ids)
        records = read(kind, ids, props)
        for event_id in ids:
            links = set(linked_deals.get(event_id, []))
            # No cross-deal borrowing, including an explicitly multi-deal activity.
            if links and links != {str(deal_id)}:
                excluded += 1
                continue
            value = records[event_id]
            source_id = f"{kind}:{event_id}"
            digest = content_hash(value)
            confirmation = confirmed.get(source_id)
            accepted = links == {str(deal_id)} or (
                confirmation is not None and confirmation["content_hash"] == digest)
            if accepted:
                merged.setdefault(kind, []).append({**value, "_source_id": event_id})
            else:
                candidates.append({"source_id": source_id, "content_hash": digest,
                                   "via": sorted(via[event_id]), "properties": value,
                                   "status": "confirmation_stale" if confirmation else "association_to_confirm"})
    return merged, {"status": "complete", "candidates": candidates,
                    "excluded_other_deal": excluded, "contacts_examined": len(contacts),
                    "confirmed_sources": sorted(
                        f"{kind}:{item['_source_id']}"
                        for kind, items in merged.items() for item in items
                        if str(item["_source_id"]) not in {str(d["_source_id"]) for d in direct.get(kind, [])})}
