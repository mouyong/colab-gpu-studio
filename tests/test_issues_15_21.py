"""Regressions for public issues 15, 17, 18 and 19.

These cover the server gates the UI was missing or contradicting:
required smoke inputs stay mandatory, the smoke runtime is one number,
an activation job reports the project it actually landed in, and disabling
the current workflow does not cancel work already queued.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from app import review_routes
from app.common import APIError
from app.db import Store
from app.gpu.compute import GpuConfig
from app.gpu.registry import Registry
from app.gpu.repository import Repository

OWNER = {"id": "owner", "role": "admin"}
COMMIT = "a" * 40
REVISION = "b" * 40
SHA256 = "c" * 64


def request(headers=None):
    return SimpleNamespace(headers=headers or {}, state=SimpleNamespace(request_id="req-issues"))


def body(response):
    return json.loads(response.body)["data"]


def _manifest(**parameters):
    return {
        "manifest_version": 1,
        "model_id": "resume-model",
        "parameters": parameters or {
            "prompt": {"type": "string", "default": "hello", "binding": {"node": "1", "input": "text"}},
        },
        "dependencies": {
            "comfyui_commit": COMMIT,
            "custom_nodes": [],
            "weight_files": [{
                "repo_id": "acme/model",
                "filename": "model.safetensors",
                "revision": REVISION,
                "sha256": SHA256,
                "target": "checkpoints/model.safetensors",
            }],
        },
        "max_runtime_minutes": 40,
    }


def _workflow(repo: Repository, manifest=None, api_graph=None, object_info=None):
    repo.create(
        "models",
        OWNER["id"],
        {
            "model_id": "resume-model",
            "source": "https://huggingface.co/acme/model",
            "revision": REVISION,
            "license": "apache-2.0",
            "resource_profile": {"vram_gb": 8, "ram_gb": 16},
            "enabled": False,
            "is_mock": False,
            "review_approved": True,
            "license_accepted": True,
        },
    )
    return repo.create(
        "workflows",
        OWNER["id"],
        {
            "workflow_key": "minimax-h3-i2v",
            "workflow_version": "1.1.0",
            "model_id": "resume-model",
            "manifest": manifest or _manifest(),
            "api_graph": api_graph or {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"text": "hello"}}},
            "object_info": object_info or {"CheckpointLoaderSimple": {"input": {"required": {"text": ["STRING"]}}}},
            "state": "validated",
            "review_approved": True,
        },
    )


def test_activation_lists_required_images_without_dropping_optional_ones():
    store = Store("sqlite:///:memory:")
    manifest = _manifest(
        first_frame={"type": "image", "label": "首帧图", "required": True, "binding": {"node": "1", "input": "image"}},
        last_frame={"type": "image", "label": "尾帧图", "required": False, "binding": {"node": "2", "input": "image"}},
        prompt={"type": "string", "label": "提示词", "required": True, "default": "hello", "binding": {"node": "3", "input": "prompt"}},
    )
    workflow = _workflow(Repository(store), manifest)
    data = body(review_routes.workflow_activation(workflow["id"], request(), user=OWNER, st=store))
    listed = {item["key"]: item for item in data["required_inputs"]}
    assert listed["first_frame"]["required"] is True
    assert listed["first_frame"]["label"] == "首帧图"
    assert listed["last_frame"]["required"] is False
    assert "prompt" not in listed


def test_real_smoke_still_rejects_a_missing_required_image(monkeypatch):
    store = Store("sqlite:///:memory:")
    repo = Repository(store)
    manifest = _manifest(
        first_frame={"type": "image", "label": "首帧图", "required": True, "binding": {"node": "1", "input": "image"}},
    )
    workflow = _workflow(
        repo,
        manifest,
        {"1": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}}},
        {"LoadImage": {"input": {"required": {"image": [["placeholder.png"], {}]}}}},
    )
    store.create("projects", OWNER["id"], {"title": "colab"}, record_id="project-user")
    repo.create("accounts", OWNER["id"], {"enabled": True, "allow_allocation": True}, record_id="account-1")
    monkeypatch.setattr(review_routes.GpuConfig, "from_env", lambda: GpuConfig(real_enabled=True, unit_rate=0.1, max_runtime_minutes=30))
    with pytest.raises(APIError) as exc:
        review_routes.real_smoke(
            workflow["id"],
            {
                "confirmed": True,
                "project_id": "project-user",
                "account_id": "account-1",
                "params": {},
                "asset_ids": [],
                "spec": {"key": {"variant": "GPU", "accelerator": "T4", "shape": "STANDARD"}},
            },
            request({"Idempotency-Key": "missing-image"}),
            OWNER,
            store,
        )
    assert exc.value.code == "WORKFLOW_INVALID"
    assert "first_frame" in exc.value.message
    assert repo.list("jobs", OWNER["id"]) == []


def test_smoke_runtime_policy_matches_the_cap_and_names_the_project(monkeypatch):
    store = Store("sqlite:///:memory:")
    repo = Repository(store)
    workflow = _workflow(repo)
    store.create("projects", OWNER["id"], {"title": "colab"}, record_id="project-user")
    repo.create("accounts", OWNER["id"], {"enabled": True, "allow_allocation": True}, record_id="account-1")
    monkeypatch.setattr(
        review_routes.GpuConfig,
        "from_env",
        lambda: GpuConfig(real_enabled=True, unit_rate=0.1, max_runtime_minutes=30, daily_units=100, monthly_units=100, max_job_units=5),
    )
    monkeypatch.setattr(review_routes.GoogleService, "status", lambda self: {"colab": {"connected": True}})
    monkeypatch.setattr(
        review_routes.GoogleColabProvider,
        "list_specs",
        lambda self: [{"eligible": True, "key": {"variant": "GPU", "accelerator": "T4", "shape": "STANDARD"}}],
    )
    created = body(review_routes.real_smoke(
        workflow["id"],
        {
            "confirmed": True,
            "project_id": "project-user",
            "account_id": "account-1",
            "params": {"prompt": "hello"},
            "asset_ids": [],
            "spec": {"key": {"variant": "GPU", "accelerator": "T4", "shape": "STANDARD"}},
        },
        request({"Idempotency-Key": "smoke-cap"}),
        OWNER,
        store,
    ))
    job = repo.get("jobs", created["job_id"], OWNER["id"])
    assert job["project_id"] == "project-user"
    assert job["max_runtime_minutes"] == 30
    assert job["execution_policy"]["max_runtime_minutes"] == 30
    event = next(item for item in repo.list("events", OWNER["id"]) if item.get("job_id") == job["id"])
    assert "最长 30 分钟" in event["message"]
    activation = body(review_routes.workflow_activation(workflow["id"], request(), user=OWNER, st=store))
    assert activation["max_runtime_minutes"] == 30
    assert "≤30 分钟" in next(step["label"] for step in activation["steps"] if step["key"] == "real_smoke")
    assert activation["smoke"]["project_id"] == "project-user"
    assert activation["smoke"]["max_runtime_minutes"] == 30


def test_deactivate_clears_the_default_and_leaves_inflight_jobs():
    store = Store("sqlite:///:memory:")
    repo = Repository(store)
    registry = Registry(repo)
    workflow = repo.create(
        "workflows",
        OWNER["id"],
        {
            "workflow_key": "demo-mock",
            "workflow_version": 1,
            "model_id": "demo-mock",
            "state": "published",
            "active": True,
            "manifest": {"parameters": {}},
            "api_graph": {},
            "object_info": {},
        },
    )
    job, _ = repo.create_job(OWNER["id"], {"project_id": "project-user", "workflow_key": "demo-mock"}, "running-job")
    repo.update("jobs", job["id"], {"state": "running"}, OWNER["id"])
    updated = registry.deactivate(OWNER["id"], workflow["id"])
    assert updated["active"] is False
    assert updated["state"] == "published"
    assert repo.get("jobs", job["id"], OWNER["id"])["state"] == "running"
    assert registry.deactivate(OWNER["id"], workflow["id"])["active"] is False
    draft = repo.create(
        "workflows",
        OWNER["id"],
        {
            "workflow_key": "demo-mock",
            "workflow_version": 2,
            "model_id": "demo-mock",
            "state": "draft",
            "active": False,
            "manifest": {},
            "api_graph": {},
            "object_info": {},
        },
    )
    with pytest.raises(ValueError, match="已发布"):
        registry.deactivate(OWNER["id"], draft["id"])
