"""自动选强度嵌入：参数校验 + 逐档实测选择 + 失败不建任务 + 通过后沿用现有流程。

运行（需项目依赖）：
    WM_DATA_DIR 由本测试自动指向临时目录；
    python -m pytest tests/test_autoembed.py -q
或直接：
    python tests/test_autoembed.py
"""
import io
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEXT = "机密-自动-2026"


def _make_png(size=384, seed=11) -> bytes:
    """与 test_matrix 相同的纹理测试图（seed=11 上的逐档行为已实测确定）。"""
    rng = np.random.RandomState(seed)
    y, x = np.mgrid[0:size, 0:size]
    base = (
        128
        + 40 * np.sin(2 * np.pi * x / 130)
        + 30 * np.cos(2 * np.pi * y / 95)
        + rng.normal(0, 12, (size, size))
    ).clip(0, 255).astype(np.uint8)
    rgb = np.stack([base, (base * 0 + 128), (255 - base)], axis=-1)
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 参数校验（纯逻辑）

def test_validate_params():
    from app.autoembed import AutoEmbedError, validate_params

    assert validate_params(50, 75, 30) == (50, 0.75, 30.0)
    assert validate_params(100, 100, 60) == (100, 1.0, 60.0)
    assert validate_params(20, 30, 20) == (20, 0.3, 20.0)
    # 百分数输入、裁切比例吸附到 3 位小数
    assert validate_params(90, 74.9, 33.333) == (90, 0.749, 33.33)

    for bad_q in (19, 101, 50.0, True, "50", None):
        try:
            validate_params(bad_q, 75, 30)
        except AutoEmbedError:
            pass
        else:
            raise AssertionError(f"JPEG 质量应拒绝 {bad_q!r}")

    for bad_c in (29, 101, True, "75", None):
        try:
            validate_params(50, bad_c, 30)
        except AutoEmbedError:
            pass
        else:
            raise AssertionError(f"裁切比例应拒绝 {bad_c!r}")

    for bad_p in (19, 61, True, "30", None):
        try:
            validate_params(50, 75, bad_p)
        except AutoEmbedError:
            pass
        else:
            raise AssertionError(f"最低 PSNR 应拒绝 {bad_p!r}")


# ---------------------------------------------------------------- API 端到端

def _fresh_client():
    tmp = tempfile.mkdtemp(prefix="wm-auto-")
    os.environ["WM_DATA_DIR"] = tmp
    os.environ["WM_MAX_JOBS"] = "200"
    for mod in ("app.main", "app.archive", "app.autoembed"):
        sys.modules.pop(mod, None)
    from fastapi.testclient import TestClient
    from app.main import app, DATA_DIR  # noqa: E402

    return TestClient(app), Path(DATA_DIR)


def _post_auto(client, *, quality=90, crop=100, min_psnr=30, text=TEXT):
    return client.post(
        "/api/embed-auto",
        files={"image": ("img.png", _make_png(), "image/png")},
        data={"text": text,
              "jpeg_quality": str(quality),
              "crop_percent": str(crop),
              "min_psnr": str(min_psnr)},
    )


def _job_dirs(data_dir: Path) -> list[Path]:
    if not data_dir.exists():
        return []
    return [p for p in data_dir.iterdir()
            if p.is_dir() and (p / "meta.json").is_file()]


def test_auto_selects_low_when_lowest_passes():
    """温和攻击 + 低 PSNR 门槛：低档首个通过即选中，不再需要更高档。"""
    client, data_dir = _fresh_client()
    r = _post_auto(client, quality=90, crop=100, min_psnr=30)
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["job_id"] is not None
    ev = data["auto_select"]
    assert ev["passed"] is True
    assert ev["selected_strength"] == "low"
    assert ev["strength_order"] == ["low", "medium", "high"]
    assert ev["params"] == {
        "jpeg_quality": 90, "crop_fraction": 1.0,
        "crop_percent": 100.0, "min_psnr_db": 30.0,
    }

    tiers = {t["strength"]: t for t in ev["tiers"]}
    assert set(tiers) == {"low", "medium", "high"}
    low = tiers["low"]
    assert low["passed"] and low["selected"]
    assert low["text_match"] and low["psnr_ok"]
    assert low["recovered_text"] == TEXT
    assert low["psnr_db"] >= 30
    # 三档全部实测（页面要展示各档 PSNR 与恢复结果）
    for t in ev["tiers"]:
        assert t["psnr_db"] is not None and t["metrics"] is not None
        assert {"confidence", "sync_score", "sync_agreement",
                "votes_per_bit", "marked_fraction"} <= set(t["metrics"])

    # 通过时沿用现有任务保存流程
    assert data["strength"] == "low"
    assert data["embed_mode"] == "auto"
    assert data["psnr_db"] == low["psnr_db"]
    job = data["job_id"]
    job_dir = data_dir / job
    assert (job_dir / "original.png").is_file()
    assert (job_dir / "watermarked.png").is_file()
    assert not (job_dir / "target.png").exists()
    meta = __import__("json").loads((job_dir / "meta.json").read_text("utf-8"))
    assert meta["embed_mode"] == "auto"
    assert meta["strength"] == "low"
    assert meta["auto_select"]["selected_strength"] == "low"

    # 下载流程可用
    dl = client.get(f"/api/jobs/{job}/watermarked")
    assert dl.status_code == 200 and dl.headers["content-type"] == "image/png"


def test_auto_skips_to_medium_when_low_recovery_fails():
    """Q35（seed=11 实测）：低档恢复失败、中/高档恢复成功 -> 选中首个通过的中档。"""
    client, data_dir = _fresh_client()
    r = _post_auto(client, quality=35, crop=100, min_psnr=30)
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["job_id"] is not None
    ev = data["auto_select"]
    assert ev["selected_strength"] == "medium"
    tiers = ev["tiers"]
    assert tiers[0]["strength"] == "low"
    assert tiers[0]["passed"] is False
    assert tiers[0]["text_match"] is False       # 恢复未逐字一致
    assert tiers[0]["psnr_ok"] is True           # 但 PSNR 本身合格
    assert tiers[1]["strength"] == "medium"
    assert tiers[1]["passed"] and tiers[1]["selected"]
    assert tiers[1]["recovered_text"] == TEXT
    assert tiers[2]["selected"] is False

    # 保存的是所选中档的含水印图
    assert data["strength"] == "medium"
    assert data["psnr_db"] == tiers[1]["psnr_db"]


def test_auto_all_fail_returns_measured_tiers_without_job():
    """Q35 + PSNR 门槛 37dB：低档恢复失败、中/高档 PSNR 不达标 -> 三档均不通过。"""
    client, data_dir = _fresh_client()
    before = _job_dirs(data_dir)
    r = _post_auto(client, quality=35, crop=100, min_psnr=37)
    assert r.status_code == 200, r.text
    data = r.json()

    # 不创建任务
    assert data["job_id"] is None
    assert data["passed"] is False
    assert data["selected_strength"] is None
    assert _job_dirs(data_dir) == before
    assert not (data_dir / "archive.log.jsonl").exists()
    assert "未创建任务" in data["summary"]

    tiers = data["tiers"]
    assert [t["strength"] for t in tiers] == ["low", "medium", "high"]
    assert all(t["passed"] is False for t in tiers)
    # 逐档实测结果仍完整返回
    assert tiers[0]["text_match"] is False and tiers[0]["psnr_ok"] is True
    assert tiers[1]["psnr_ok"] is False
    assert tiers[2]["psnr_ok"] is False
    for t in tiers:
        assert isinstance(t["psnr_db"], (int, float)) and t["psnr_db"] > 0
        assert t["metrics"] is not None
        assert not t["selected"]


def test_auto_all_fail_when_psnr_floor_too_high():
    """最低 PSNR 60dB 高于任一档：即便恢复一致也三档全不通过、不建任务。"""
    client, _ = _fresh_client()
    r = _post_auto(client, quality=90, crop=100, min_psnr=60)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["job_id"] is None
    assert data["passed"] is False
    assert all(t["text_match"] for t in data["tiers"])      # 恢复都成功
    assert all(not t["psnr_ok"] for t in data["tiers"])    # PSNR 全不达标
    assert all(t["psnr_db"] < 60 for t in data["tiers"])


def test_auto_validation_errors():
    client, data_dir = _fresh_client()

    # 参数越界 -> 400
    for params in [
        dict(quality=10), dict(quality=101),
        dict(crop=20), dict(crop=101),
        dict(min_psnr=19), dict(min_psnr=61),
    ]:
        kw = dict(quality=90, crop=100, min_psnr=30)
        kw.update(params)
        r = _post_auto(client, **kw)
        assert r.status_code == 400, (params, r.status_code, r.text)
    assert _job_dirs(data_dir) == []

    # 空文本 -> 400
    r = _post_auto(client, text="   ")
    assert r.status_code == 400
    # 文本超长 -> 400（与手动嵌入一致，在逐档评测前拒绝）
    r = _post_auto(client, text="字" * 30)
    assert r.status_code == 400
    # JPEG 质量非整数 -> 422（类型解析失败）
    r = client.post(
        "/api/embed-auto",
        files={"image": ("img.png", _make_png(), "image/png")},
        data={"text": TEXT, "jpeg_quality": "abc",
              "crop_percent": "75", "min_psnr": "30"})
    assert r.status_code == 422
    # 缺字段 -> 422
    r = client.post(
        "/api/embed-auto",
        files={"image": ("img.png", _make_png(), "image/png")},
        data={"text": TEXT, "jpeg_quality": "75", "crop_percent": "75"})
    assert r.status_code == 422
    # 评测被拒绝时同样不建任务
    assert _job_dirs(data_dir) == []


def test_auto_success_job_works_with_existing_flow():
    """自动选档通过后：攻击 / 检测 / 鲁棒性矩阵等现有流程行为不变。"""
    client, _ = _fresh_client()
    r = _post_auto(client, quality=50, crop=75, min_psnr=30)
    assert r.status_code == 200, r.text
    job = r.json()["job_id"]
    assert job is not None

    # 以评测相同参数施加一次真实攻击，再走现有盲检
    a = client.post("/api/attack",
                    json={"job_id": job, "jpeg_quality": 50,
                          "crop_fraction": 0.75})
    assert a.status_code == 200
    det = client.post("/api/detect", data={"job_id": job})
    assert det.status_code == 200
    dj = det.json()
    assert dj["detected"] is True
    assert dj["text"] == TEXT

    # 鲁棒性矩阵（只读）同样可用
    mx = client.post(
        f"/api/jobs/{job}/robustness-matrix",
        json={"jpeg_qualities": [50], "crop_fractions": [0.75]})
    assert mx.status_code == 200
    assert mx.json()["counts"]["successful"] == 1


def test_manual_embed_behavior_unchanged():
    """手动模式保持现有行为：直接建任务，无自动评测，meta 标记 manual。"""
    client, data_dir = _fresh_client()
    r = client.post(
        "/api/embed",
        files={"image": ("img.png", _make_png(), "image/png")},
        data={"text": TEXT, "strength": "medium"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["job_id"] is not None
    assert data["strength"] == "medium"
    assert data["embed_mode"] == "manual"
    assert "auto_select" not in data
    job_dir = data_dir / data["job_id"]
    assert (job_dir / "watermarked.png").is_file()
    meta = __import__("json").loads((job_dir / "meta.json").read_text("utf-8"))
    assert meta["embed_mode"] == "manual"
    assert "auto_select" not in meta


if __name__ == "__main__":
    import inspect
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and inspect.isfunction(v)]
    for fn in fns:
        fn()
        print(f"[PASS] {fn.__name__}")
    print(f"\n{len(fns)} 个自动选强度测试全部通过 ✔")
