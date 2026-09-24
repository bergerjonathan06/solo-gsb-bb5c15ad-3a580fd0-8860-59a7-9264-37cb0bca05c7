"""自动选强度嵌入：低→中→高逐档嵌入 + 模拟攻击盲检，选首个达标档。

设计概述
========
* 用户上传原图与文本，并指定 JPEG 质量（20–100）、中心裁切保留比例
  （30%–100%）与最低 PSNR（20–60 dB）。
* 服务端按 low → medium → high 顺序从**同一份原图**分别嵌入水印；
  每档对嵌入结果**先中心裁切、再 JPEG 压缩**，然后调用现有盲检
  ``dctwm.detect_watermark``（与鲁棒性矩阵评测完全相同的攻击顺序与检测器）。
* 仅当某档**恢复文本与原文逐字一致**且**嵌入图 PSNR ≥ 用户最低要求**时
  该档通过；按顺序取**首个通过档**。
* 三档全部实测并逐档返回（嵌入 PSNR、模拟攻击后的恢复文本与一致性、
  置信度与同步指标），便于页面展示各档实测结果；三档均未通过时
  不创建任务，仅返回逐档实测结果。
* 评测全程在内存中进行：不写盘、不建任务、不触碰检测记录与归档。
"""

from __future__ import annotations

import numpy as np

from . import dctwm as wm

STRENGTH_ORDER = ("low", "medium", "high")
MIN_JPEG_QUALITY = 20
MAX_JPEG_QUALITY = 100
MIN_CROP_PERCENT = 30.0
MAX_CROP_PERCENT = 100.0
MIN_PSNR_DB = 20.0
MAX_PSNR_DB = 60.0


class AutoEmbedError(ValueError):
    """自动选强度参数错误（映射为 HTTP 400）。"""


def validate_params(jpeg_quality, crop_percent,
                    min_psnr) -> tuple[int, float, float]:
    """校验并归一化三项参数，返回 (JPEG 质量, 裁切保留比例 0~1, 最低 PSNR dB)。"""
    if isinstance(jpeg_quality, bool) or not isinstance(jpeg_quality, int):
        raise AutoEmbedError("JPEG 质量必须为整数。")
    if not (MIN_JPEG_QUALITY <= jpeg_quality <= MAX_JPEG_QUALITY):
        raise AutoEmbedError(
            f"JPEG 质量需在 {MIN_JPEG_QUALITY}~{MAX_JPEG_QUALITY} 之间"
            f"（收到 {jpeg_quality}）。")

    if isinstance(crop_percent, bool) or not isinstance(crop_percent, (int, float)):
        raise AutoEmbedError("中心裁切保留比例必须为数字。")
    crop_percent = float(crop_percent)
    if not (MIN_CROP_PERCENT - 1e-9 <= crop_percent <= MAX_CROP_PERCENT + 1e-9):
        raise AutoEmbedError(
            f"中心裁切保留比例需在 {int(MIN_CROP_PERCENT)}%~"
            f"{int(MAX_CROP_PERCENT)}% 之间（收到 {crop_percent:g}）。")

    if isinstance(min_psnr, bool) or not isinstance(min_psnr, (int, float)):
        raise AutoEmbedError("最低 PSNR 必须为数字。")
    min_psnr = float(min_psnr)
    if not (MIN_PSNR_DB - 1e-9 <= min_psnr <= MAX_PSNR_DB + 1e-9):
        raise AutoEmbedError(
            f"最低 PSNR 需在 {int(MIN_PSNR_DB)}~{int(MAX_PSNR_DB)} dB 之间"
            f"（收到 {min_psnr:g}）。")

    fraction = round(min(max(crop_percent / 100.0, 0.30), 1.0), 3)
    return jpeg_quality, fraction, round(min_psnr, 2)


def _tier_metrics(res: wm.DetectResult) -> dict:
    return {
        "confidence": res.confidence,
        "sync_score": res.sync_score,
        "sync_agreement": res.sync_agreement,
        "votes_per_bit": res.votes_per_bit,
        "marked_fraction": res.marked_fraction,
    }


def run_auto_embed(
    rgb: np.ndarray,
    text: str,
    jpeg_quality: int,
    crop_percent: float,
    min_psnr: float,
) -> tuple[dict, wm.EmbedResult | None]:
    """逐档实测，返回 (评测结果 dict, 首个通过档的 EmbedResult 或 None)。

    三档无论是否已有通过档都会全部实测（逐档结果需随响应展示）；
    全程不写盘、不创建任务、不产生检测记录与归档。
    """
    quality, fraction, min_psnr_db = validate_params(
        jpeg_quality, crop_percent, min_psnr)
    # 与手动嵌入一致：文本超长等参数问题直接抛错（HTTP 400），不进入逐档评测
    wm.encode_stream(text)

    tiers: list[dict] = []
    embeds: dict[str, wm.EmbedResult] = {}
    selected_strength: str | None = None
    for strength in STRENGTH_ORDER:
        tier: dict = {
            "strength": strength,
            "psnr_db": None,
            "psnr_ok": False,
            "detected": False,
            "recovered_text": "",
            "text_match": False,
            "reason": "",
            "passed": False,
            "selected": False,
            "metrics": None,
        }
        try:
            # 每档都从同一份原图重新嵌入（embed_watermark 不修改入参）
            emb = wm.embed_watermark(rgb, text, strength=strength)
            embeds[strength] = emb
            tier["psnr_db"] = emb.psnr_db
            tier["psnr_ok"] = bool(emb.psnr_db >= min_psnr_db)

            # 与鲁棒性矩阵评测一致：先中心裁切，后 JPEG 压缩，再现有盲检
            attacked = wm.center_crop(emb.image, fraction)
            attacked = wm.jpeg_compress(attacked, quality)
            det = wm.detect_watermark(attacked)
            tier["detected"] = bool(det.detected)
            tier["recovered_text"] = det.text or ""
            tier["text_match"] = bool(det.detected and det.text == text)
            tier["reason"] = det.reason or ""
            tier["metrics"] = _tier_metrics(det)
        except Exception as exc:         # 单档异常隔离，不拖垮其余档
            tier["reason"] = f"该档评测执行失败：{exc}"
        tier["passed"] = bool(tier["text_match"] and tier["psnr_ok"])
        if tier["passed"] and selected_strength is None:
            selected_strength = strength
            tier["selected"] = True
        tiers.append(tier)

    selected = embeds.get(selected_strength) if selected_strength else None
    evaluation = {
        "mode": "auto",
        "strength_order": list(STRENGTH_ORDER),
        "params": {
            "jpeg_quality": quality,
            "crop_fraction": fraction,
            "crop_percent": round(fraction * 100, 1),
            "min_psnr_db": min_psnr_db,
        },
        "tiers": tiers,
        "passed": selected_strength is not None,
        "selected_strength": selected_strength,
        "summary": (
            f"已选定「{selected_strength}」档：恢复文本逐字一致且嵌入图 "
            f"PSNR ≥ {min_psnr_db:g} dB。"
            if selected_strength else
            f"低/中/高三档均未同时满足「恢复文本逐字一致且嵌入图 PSNR ≥ "
            f"{min_psnr_db:g} dB」，未创建任务。"
        ),
    }
    return evaluation, selected
