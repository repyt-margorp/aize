from __future__ import annotations

from typing import Any

from wire.protocol import message_meta, message_meta_get, message_set_meta

DEFAULT_NETWORK_TTL = 16


def parse_service_address(address: str) -> tuple[str | None, str]:
    value = str(address or "").strip()
    if not value:
        return None, ""
    if "::" in value:
        node_id, service_id = value.split("::", 1)
        return node_id.strip() or None, service_id.strip()
    if "/" in value:
        node_id, service_id = value.split("/", 1)
        return node_id.strip() or None, service_id.strip()
    if "@" in value:
        service_id, node_id = value.rsplit("@", 1)
        return node_id.strip() or None, service_id.strip()
    return None, value


def normalize_network_message(manifest: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(message)
    local_node = str(manifest.get("node_id") or "").strip()
    from_node = str(message_meta_get(normalized, "from_node", "") or "").strip() or local_node
    message_set_meta(normalized, "from_node", from_node)

    to_node_from_address, service_id = parse_service_address(str(normalized.get("to", "")))
    if service_id:
        normalized["to"] = service_id
    to_node = to_node_from_address or str(message_meta_get(normalized, "to_node", "") or "").strip() or local_node
    message_set_meta(normalized, "to_node", to_node)

    if to_node != local_node and message_meta_get(normalized, "ttl") is None:
        message_set_meta(normalized, "ttl", DEFAULT_NETWORK_TTL)
    return normalized


def prepare_outbound_network_message(
    manifest: dict[str, Any],
    message: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    normalized = normalize_network_message(manifest, message)
    local_node = str(manifest.get("node_id") or "").strip()
    to_node = str(message_meta_get(normalized, "to_node", "") or "").strip()
    if not to_node or to_node == local_node:
        return normalized, "local"

    meta = dict(message_meta(normalized))
    raw_ttl = meta.get("ttl", DEFAULT_NETWORK_TTL)
    try:
        ttl = int(raw_ttl)
    except (TypeError, ValueError):
        ttl = DEFAULT_NETWORK_TTL
    if ttl <= 0:
        return None, "ttl_expired"

    raw_hops = meta.get("hops")
    hops = [str(item) for item in raw_hops] if isinstance(raw_hops, list) else []
    if local_node in hops:
        return None, "routing_loop"
    hops.append(local_node)
    message_set_meta(normalized, "ttl", ttl - 1)
    message_set_meta(normalized, "hops", hops)
    return normalized, "remote"
