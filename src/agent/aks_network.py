"""Focused, passive Service/backend evidence. No connectivity probes."""

import ipaddress
import re


def _missing(value):
    return isinstance(value, dict) and (value.get("reason") == "NotFound" or value.get("code") == 404)


def _labels(value):
    return isinstance(value, dict) and all(
        isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9./_-]+", k)
        and isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._-]*", v) for k, v in value.items()
    )


def service_details(service, deployment=None, pods=None):
    spec = service.get("spec", {}) if isinstance(service, dict) else {}
    if not isinstance(spec, dict):
        spec = {}
    selector = spec.get("selector")
    selection = {"state": "missing" if selector is None or selector == {} else "unknown"}
    if _labels(selector) and selector:
        selection = {"state": "present", "labels": dict(selector), "deployment_comparison": "unknown"}
        dspec = deployment.get("spec", {}) if isinstance(deployment, dict) else {}
        dspec = dspec if isinstance(dspec, dict) else {}
        dselector = dspec.get("selector", {}).get("matchLabels") if isinstance(dspec.get("selector"), dict) else None
        if _labels(dselector) and dselector:
            if any(k in dselector and dselector[k] != v for k, v in selector.items()):
                selection["deployment_comparison"] = "mismatch"
            elif all(dselector.get(k) == v for k, v in selector.items()):
                selection["deployment_comparison"] = "match"
        selection["pod_matches"] = []
        for pod in pods.get("items", []) if isinstance(pods, dict) else []:
            metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
            labels = metadata.get("labels")
            if metadata.get("namespace") == "default" and _labels(labels):
                selection["pod_matches"].append({"name": metadata.get("name"),
                    "matches": all(labels.get(k) == v for k, v in selector.items())})
    ports = spec.get("ports")
    port_evidence = {"state": "missing" if ports is None or ports == [] else "unknown", "items": []}
    if isinstance(ports, list) and ports:
        for port in ports:
            if not isinstance(port, dict):
                break
            number, target, protocol = port.get("port"), port.get("targetPort", port.get("port")), port.get("protocol", "TCP")
            valid_number = lambda n: type(n) is int and 1 <= n <= 65535
            if not valid_number(number) or not (valid_number(target) or (
                isinstance(target, str) and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,13}[a-z0-9])?", target)
            )) or protocol not in ("TCP", "UDP", "SCTP"):
                break
            port_evidence["items"].append({"port": number, "targetPort": target, "protocol": protocol})
        else:
            port_evidence["state"] = "present"
        if port_evidence["state"] != "present":
            port_evidence["items"] = []
    meta = service.get("metadata", {}) if isinstance(service, dict) else {}
    valid = isinstance(meta, dict) and meta.get("namespace") == "default" and meta.get("name") == "task-manager"
    return {"existence": "missing" if _missing(service) else "present" if valid else "unknown",
            "selector": selection, "ports": port_evidence}


def _source(payload, metadata, *, slices, pods=None, selector=None):
    result = {"state": "unknown", "ready": 0, "not_ready": 0, "unknown_readiness": 0,
              "backends": [], "pod_correlations": []}
    if metadata and metadata.get("state") == "unknown":
        result["state"] = "error" if metadata.get("reason") in {"tool_failure", "tool_transport_failure"} else "unknown"
        result["reason"] = metadata.get("reason", "invalid_evidence")
        return result
    if _missing(payload):
        result["state"] = "missing"
        return result
    if not isinstance(payload, dict):
        return result
    addresses = {}
    correlations = set()
    try:
        entries = []
        if slices:
            items = payload["items"]
            if not isinstance(items, list):
                raise ValueError
            for item in items:
                meta = item["metadata"]
                if meta.get("namespace") != "default" or meta.get("labels", {}).get("kubernetes.io/service-name") != "task-manager":
                    raise ValueError
                if not isinstance(item.get("endpoints"), list):
                    raise ValueError
                for endpoint in item["endpoints"]:
                    ready = endpoint.get("conditions", {}).get("ready")
                    if ready is not None and type(ready) is not bool:
                        raise ValueError
                    if not isinstance(endpoint.get("addresses"), list) or not endpoint["addresses"]:
                        raise ValueError
                    entries.extend((address, ready, endpoint.get("targetRef")) for address in endpoint["addresses"])
        else:
            meta = payload["metadata"]
            if meta.get("namespace") != "default" or meta.get("name") != "task-manager":
                raise ValueError
            subsets = payload.get("subsets", [])
            if not isinstance(subsets, list):
                raise ValueError
            for subset in subsets:
                for key, ready in (("addresses", True), ("notReadyAddresses", False)):
                    if not isinstance(subset.get(key, []), list):
                        raise ValueError
                    entries.extend((a["ip"], ready, a.get("targetRef")) for a in subset.get(key, []))
        for address, ready, reference in entries:
            if not isinstance(address, str):
                raise ValueError
            address = str(ipaddress.ip_address(address))
            if address in addresses and addresses[address] != ready:
                raise ValueError
            addresses[address] = ready
            if isinstance(reference, dict) and reference.get("kind") == "Pod" and reference.get("namespace") == "default" and reference.get("uid"):
                for pod in pods.get("items", []) if isinstance(pods, dict) else []:
                    meta = pod.get("metadata", {})
                    labels = meta.get("labels")
                    if (meta.get("uid") == reference["uid"] and meta.get("name") == reference.get("name")
                            and meta.get("namespace") == "default" and _labels(labels)
                            and _labels(selector) and selector and all(labels.get(k) == v for k, v in selector.items())):
                        correlations.add(meta["name"])
    except (KeyError, TypeError, AttributeError, ValueError):
        result["reason"] = "invalid_or_uncorrelated_endpoint_evidence"
        return result
    result.update(state="present", ready=sum(v is True for v in addresses.values()),
                  not_ready=sum(v is False for v in addresses.values()),
                  unknown_readiness=sum(v is None for v in addresses.values()),
                  backends=[{"address": k, "ready": v} for k, v in sorted(addresses.items())],
                  pod_correlations=sorted(correlations))
    return result


def network_evidence(service, endpoints, slices, service_metadata=None,
                     endpoints_metadata=None, slices_metadata=None, deployment=None, pods=None):
    details = service_details(service, deployment, pods)
    if service_metadata and service_metadata.get("state") == "unknown":
        details["existence"] = "error" if service_metadata.get("reason") in {"tool_failure", "tool_transport_failure"} else "unknown"
    selector = details["selector"].get("labels")
    sources = {"endpoints": _source(endpoints, endpoints_metadata, slices=False, pods=pods, selector=selector),
               "endpoint_slices": _source(slices, slices_metadata, slices=True, pods=pods, selector=selector)}
    result = {"state": "unknown", "service": details, "sources": sources,
              "ready_endpoints": None, "selected_source": None,
              "reachability": "not_tested", "qualification": "Backend readiness and LoadBalancer assignment do not prove application or external connectivity."}
    if details["existence"] == "missing":
        result["state"] = "service_missing"
        return result
    if details["existence"] != "present":
        return result
    ep, es = sources.values()
    result["incomplete"] = any(source["state"] != "present" for source in sources.values())
    if ep["state"] == es["state"] == "present" and ep["backends"] != es["backends"]:
        result["state"] = "conflicting_evidence"
        return result
    chosen = "endpoint_slices" if es["state"] == "present" else "endpoints" if ep["state"] == "present" else None
    if chosen:
        source = sources[chosen]
        result.update(selected_source=chosen, ready_endpoints=source["ready"],
                      state="unknown" if source["unknown_readiness"] else "ready_endpoints" if source["ready"] else
                      "not_ready_endpoints" if source["not_ready"] else "no_endpoints")
        if not source["backends"] and any(s["state"] in {"unknown", "error"} for s in sources.values()):
            result["state"] = "unknown"
    return result
