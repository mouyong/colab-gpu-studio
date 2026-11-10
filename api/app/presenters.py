"""Allowlisted public representations; never expose runtime connection handles."""

def public_asset(asset):
    return {k: v for k, v in asset.items() if k not in {"storage_key", "owner_id"}}


def public_job(job):
    # Only explicit business fields are exposed. Runtime handles/connectionInfo never leave the server.
    keys = {
        "id",
        "project_id",
        "model_id",
        "workflow_key",
        "workflow_version",
        "state",
        "provider",
        "params",
        "resolved_seed",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "estimated_units",
        "actual_units",
        "ccu_before",
        "ccu_before_at",
        "ccu_after",
        "ccu_after_at",
        "ccu_attribution",
        "ccu_source",
        "ccu_measured_at",
        "simulated_units",
        "unit_rate",
        "max_runtime_minutes",
        "output_asset_ids",
        "cancel_requested",
        "progress",
        "runtime_deleted",
        "execution_id",
        "attempt_number",
        "batch_id",
        "queue_order",
        "stage",
        "prep",
        "for_job_id",
        "prep_job_id",
        "awaiting_prep",
        "requested_account_id",
        "requested_instance_id",
        "account_id",
        "drive_account_id",
        "account_alias_snapshot",
        "instance_id",
        "instance_label",
        "instance_state",
        "gpu_reported",
        "retained_until",
        "selection_reason",
    }
    result = {k: v for k, v in job.items() if k in keys}
    result["job_id"] = job["id"]
    result["mock"] = job.get("provider") == "mock"
    result["workflow_smoke"] = job.get("workflow_smoke") is True or (job.get("snapshot") or {}).get("workflow_smoke") is True
    result["resolved_params"] = (job.get("snapshot") or {}).get("resolved_params", {})
    # The requested Colab spec is shown separately from the GPU the worker actually measured.
    requested = (job.get("requested_spec") or (job.get("execution_policy") or {}).get("requested_spec")
                 or ((job.get("snapshot") or {}).get("manifest") or {}).get("compute_spec"))
    if isinstance(requested, dict):
        requested = requested.get("key", requested)
        result["runtime_spec_requested"] = {k: requested[k] for k in ("variant", "accelerator", "shape") if k in requested}
    return result


