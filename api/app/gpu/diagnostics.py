"""GPU device evidence measured on the runtime by nvidia-smi."""


def gpu_diagnostics(value):
    """Keep only device names and memory; return None when nothing usable was reported."""
    if not isinstance(value, dict) or value.get("source") != "runtime_nvidia_smi":
        return None
    try:
        devices = [
            {"name": str(device["name"]).strip()[:120], "memory_mib": int(device["memory_mib"])}
            for device in value["devices"][:8]
        ]
        captured = float(value["captured_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if not devices or not all(device["name"] for device in devices):
        return None
    return {"source": "runtime_nvidia_smi", "captured_at": captured, "devices": devices}
