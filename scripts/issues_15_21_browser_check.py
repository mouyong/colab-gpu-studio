"""Offline browser check for issues 15–21. Mocks every API and never starts a GPU job."""
from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
OUT = Path("/opt/cursor/artifacts")
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:4177"

DEMO = {
    "id": "wf-demo",
    "workflow_key": "demo-mock",
    "workflow_version": 1,
    "model_id": "demo-mock",
    "state": "published",
    "active": True,
    "manifest": {
        "title": "离线演示",
        "description": "不连接 Google、不扣费，只用来熟悉界面。",
        "parameters": {
            "prompt": {"type": "string", "label": "提示词", "default": "默认提示"},
            "width": {"type": "integer", "label": "宽", "default": 832, "min": 256, "max": 2048, "step": 16},
            "height": {"type": "integer", "label": "高", "default": 1248, "min": 256, "max": 2048, "step": 16},
        },
    },
}
KREA = {
    "id": "wf-krea",
    "workflow_key": "krea2-moss-dual-t2i",
    "workflow_version": "1.1.1",
    "model_id": "krea2-moss-dual",
    "state": "published",
    "active": True,
    "manifest": {
        "title": "Krea-2 + MOSS 文生图",
        "description": "先构图再精修，不用上传参考图。",
        "parameters": {
            "prompt": {"type": "string", "label": "提示词", "default": "另一套默认"},
            "width": {"type": "integer", "label": "宽", "default": 512, "min": 256, "max": 512, "step": 16},
            "height": {"type": "integer", "label": "高", "default": 512, "min": 256, "max": 512, "step": 16},
        },
    },
}
H3 = {
    "id": "wf-h3",
    "workflow_key": "minimax-h3-i2v",
    "workflow_version": "1.1.0",
    "model_id": "minimax-h3",
    "state": "draft",
    "active": False,
    "manifest": {
        "title": "MiniMax H3 图生视频（带声音）",
        "description": "上传一张首帧图，生成带声音的短视频。",
        "parameters": {
            "first_frame": {"type": "image", "label": "首帧图", "required": True},
            "prompt": {"type": "string", "label": "提示词", "default": "动起来"},
        },
    },
}


def envelope(data):
    return {"data": data, "error": None, "request_id": "browser-check"}


def main():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DIST), **kwargs)

        def log_message(self, format, *args):  # noqa: A003
            return

    server = ThreadingHTTPServer(("127.0.0.1", 4177), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    calls = []
    demo_active = {"value": True}

    def workflows():
        demo = {**DEMO, "active": demo_active["value"]}
        return [demo, KREA, H3]

    def answer(method, path):
        calls.append((method, path))
        if path == "/api/auth/login":
            return envelope({"access_token": "browser-check-token"})
        if path == "/api/gpu/projects":
            return envelope([
                {"id": "mock-a", "title": "回归验收（MOCK）"},
                {"id": "mock-b", "title": "回归验收（MOCK）"},
                {"id": "colab", "title": "colab"},
            ])
        if path == "/api/gpu/models":
            return envelope([
                {"model_id": "demo-mock", "display_name": "离线演示", "enabled": True},
                {"model_id": "krea2-moss-dual", "display_name": "Krea", "enabled": True},
                {"model_id": "minimax-h3", "display_name": "MiniMax H3", "enabled": False},
            ])
        if path == "/api/gpu/workflows":
            return envelope(workflows())
        if path == "/api/gpu/presets":
            return envelope([])
        if path == "/api/gpu/settings":
            return envelope({"real_enabled": True, "offline_mode": True, "budget": {"daily_units": 10, "max_runtime_minutes": 30}})
        if path == "/api/gpu/usage":
            return envelope({"estimated_units": 0, "actual_units": 0, "daily_limit": 10, "active_runtimes": 0})
        if path == "/api/gpu/accounts":
            return envelope([])
        if path.startswith("/api/gpu/instances"):
            return envelope([{
                "id": "dead-instance",
                "instance_id": "dead-instance",
                "instance_label": "旧机器",
                "account_id": "acct-user",
                "state": "released",
                "provider": "colab",
                "gpu_reported": "T4",
            }])
        if path.startswith("/api/gpu/jobs/") and method == "GET":
            return envelope({
                "id": "job-done",
                "project_id": "colab",
                "workflow_key": "krea2-moss-dual-t2i",
                "workflow_version": "1.1.1",
                "state": "completed",
                "provider": "colab",
                "workflow_smoke": False,
                "created_at": "2026-11-08T09:30:43Z",
                "instance_id": "dead-instance",
                "instance_label": "旧机器",
                "instance_state": "released",
                "timings": {"comfy_start": 29.1, "restore_weights": 122.4, "sampling": 224.4},
                "timing_details": {
                    "comfy_start": {"seconds": 29.1, "source": "worker", "status": "completed"},
                    "restore_weights": {"seconds": 122.4, "source": "worker", "status": "completed"},
                    "sampling": {"seconds": 224.4, "source": "worker", "status": "completed"},
                },
                "resolved_params": {"prompt": "A quiet garden in soft daylight.", "width": 832, "height": 1248},
                "attempts": [{"id": "a1", "attempt": 1, "state": "completed"}],
                "logs": [],
                "events": [],
            })
        if path.startswith("/api/gpu/jobs"):
            project = "colab"
            if "project_id=" in path:
                project = path.split("project_id=", 1)[1].split("&", 1)[0]
            if project != "colab":
                return envelope([])
            return envelope([{
                "id": "job-done",
                "project_id": "colab",
                "workflow_key": "krea2-moss-dual-t2i",
                "state": "completed",
                "provider": "colab",
                "created_at": "2026-11-08T09:30:43Z",
                "instance_id": "dead-instance",
                "instance_label": "旧机器",
                "instance_state": "released",
                "gpu_reported": "T4",
            }])
        if path.startswith("/api/gpu/workflows/") and path.endswith("/schema"):
            workflow_id = path.split("/")[4]
            found = next(item for item in workflows() if item["id"] == workflow_id)
            return envelope(found["manifest"])
        if path.endswith("/activation"):
            return envelope({
                "workflow": {"id": "wf-h3", "workflow_key": "minimax-h3-i2v", "version": "1.1.0", "state": "draft", "active": False},
                "model": {"model_id": "minimax-h3", "review_approved": False, "license_accepted": False, "enabled": False},
                "steps": [
                    {"key": "validate", "label": "校验模板", "done": False, "next": True},
                    {"key": "real_smoke", "label": "真实 GPU 冒烟（≤30 分钟）", "done": False},
                ],
                "required_inputs": [{"key": "first_frame", "label": "首帧图", "description": "会被当成视频的第一帧。", "type": "image", "required": True}],
                "smoke": None,
                "smoke_job_completed": False,
                "real_enabled": False,
                "max_runtime_minutes": 30,
                "blockers": ["服务端未开启真实 GPU（GPU_REAL_ENABLED）"],
                "done": False,
            })
        if path.endswith("/deactivate"):
            demo_active["value"] = False
            return envelope({**DEMO, "active": False})
        if path.startswith("/api/gpu/gallery") or path.startswith("/api/assets"):
            return envelope([])
        if path == "/api/gpu/jobs" and method == "POST":
            raise AssertionError("browser check submitted a generation job")
        if "/real-smoke" in path:
            raise AssertionError("browser check submitted a smoke job")
        return envelope([])

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 420, "height": 900},
            record_video_dir=str(OUT),
            record_video_size={"width": 420, "height": 900},
        )
        page = context.new_page()
        page.on("pageerror", lambda error: calls.append(("pageerror", str(error))))

        def handle(route):
            request = route.request
            parsed = urlparse(request.url)
            if not parsed.path.startswith("/api/"):
                route.fallback()
                return
            route.fulfill(status=200, content_type="application/json", body=json.dumps(answer(request.method, parsed.path + (("?" + parsed.query) if parsed.query else ""))))

        page.route("**/*", handle)
        page.goto(BASE)
        page.locator("input[type=password]").fill("mock-local-only")
        page.get_by_role("button", name="进入工作台").click()
        page.get_by_label("提示词").wait_for()
        page.screenshot(path=str(OUT / "issues-16-workflow-names.png"))
        body_text = page.locator("body").inner_text()
        assert "离线演示" in body_text and "demo-mock" in body_text

        page.get_by_label("提示词").fill("安静的花园")
        page.get_by_role("button", name="自定义").click()
        page.locator("input[inputmode=numeric]").first.fill("864")
        page.get_by_role("button", name="切换工作流").click()
        page.get_by_role("button", name="Krea-2 + MOSS 文生图").click()
        page.get_by_label("提示词").wait_for()
        page.wait_for_timeout(300)
        assert page.get_by_label("提示词").input_value() == "安静的花园"
        assert "512" in page.locator("body").inner_text()
        page.screenshot(path=str(OUT / "issues-21-prompt-kept.png"))
        page.get_by_role("button", name="切换工作流").click()
        page.get_by_role("button", name="离线演示").click()
        page.wait_for_timeout(300)
        assert page.get_by_label("提示词").input_value() == "安静的花园"

        page.get_by_role("button", name="切换项目").click()
        page.get_by_role("button", name="回归验收（MOCK） · mock-a").wait_for()
        page.get_by_role("button", name="回归验收（MOCK） · mock-b").wait_for()
        page.screenshot(path=str(OUT / "issues-17-project-names.png"))
        page.get_by_role("button", name="colab", exact=True).click()

        page.get_by_role("button", name="打开设置").click()
        page.get_by_role("button", name="工作流与模型").click()
        page.get_by_text("MiniMax H3 图生视频（带声音）").wait_for()
        page.get_by_role("button", name="MiniMax H3 图生视频（带声音）").click()
        page.get_by_role("button", name="一键启用").click()
        page.get_by_text("首帧图（必填）").wait_for()
        page.get_by_text("colab").first.wait_for()
        assert page.get_by_role("button", name="一键跑通并启用").is_disabled()
        page.screenshot(path=str(OUT / "issues-15-required-image.png"))
        assert not any("/real-smoke" in path for _, path in calls)

        page.get_by_role("button", name="离线演示").first.click()
        page.get_by_role("button", name="停用").click()
        page.get_by_role("button", name="设为当前", exact=True).wait_for()
        page.screenshot(path=str(OUT / "issues-18-deactivate.png"))

        page.get_by_role("button", name="设置", exact=True).click()
        page.get_by_role("navigation").locator("button").filter(has_text="任务").click()
        page.get_by_role("button", name="查看详情").click()
        page.get_by_text("不表示工作流已经发布").wait_for()
        page.get_by_text("2分2.4秒").wait_for()
        page.get_by_text("恢复模型权重").wait_for()
        body = page.locator("body").inner_text()
        assert '"comfy_start"' not in body
        page.screenshot(path=str(OUT / "issues-19-stage-seconds.png"))
        page.get_by_role("button", name="关闭").click()

        page.get_by_role("button", name="旧机器").click()
        page.get_by_role("navigation").locator("button").filter(has_text="创作").click()
        page.get_by_role("button", name="立即生成").click()
        page.get_by_role("button", name="确认启动").click()
        page.get_by_role("dialog", name="确认提交任务").get_by_text("没有提交任务").wait_for()
        page.screenshot(path=str(OUT / "issues-20-stale-instance.png"))
        assert not any(method == "POST" and path.split("?", 1)[0] == "/api/gpu/jobs" for method, path in calls)

        pageerrors = [item for item in calls if item[0] == "pageerror"]
        assert not pageerrors, pageerrors
        video = page.video
        context.close()
        browser.close()
        if video:
            saved = Path(video.path())
            target = OUT / "issues-15-21-browser.webm"
            if saved.resolve() != target.resolve():
                saved.replace(target)
        report = {
            "screenshots": [
                "issues-16-workflow-names.png",
                "issues-21-prompt-kept.png",
                "issues-17-project-names.png",
                "issues-15-required-image.png",
                "issues-18-deactivate.png",
                "issues-19-stage-seconds.png",
                "issues-20-stale-instance.png",
            ],
            "posted_generation": False,
            "posted_smoke": False,
        }
        (OUT / "issues-15-21-browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))
    server.shutdown()


if __name__ == "__main__":
    main()
