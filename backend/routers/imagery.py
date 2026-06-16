"""
imagery.py - 影像 API 路由

提供影像数据的 REST API 端点：
- GET  /api/imagery/list          列出可用影像
- GET  /api/imagery/{id}          获取影像详情
- GET  /api/imagery/{id}/tile     获取影像切片（PNG）
- GET  /api/imagery/{id}/overview 获取影像概览图
- POST /api/imagery/download      触发新影像下载任务
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import Response

from backend.models.imagery import DownloadRequest, ImageryDetail, ImageryListItem
from backend.services.zarr_service import ZarrService

router = APIRouter(prefix="/api/imagery", tags=["imagery"])

# ZARR 服务实例
zarr_service = ZarrService()


@router.get("/list", response_model=list[ImageryListItem])
async def list_imagery():
    """
    列出所有可用的影像。

    扫描 ZARR 目录，返回每个影像的基本信息（ID、日期、云量等）。
    """
    imagery = zarr_service.list_available_imagery()
    return [
        ImageryListItem(
            id=item["id"],
            date=item["date"],
            cloud_cover=item["cloud_cover"],
            has_zarr=item["has_zarr"],
            zarr_path=item["zarr_path"],
        )
        for item in imagery
    ]


@router.get("/{imagery_id}", response_model=ImageryDetail)
async def get_imagery_detail(imagery_id: str):
    """
    获取指定影像的详细信息。

    Args:
        imagery_id: 影像 ID
    """
    detail = zarr_service.get_imagery_detail(imagery_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"影像 {imagery_id} 不存在")

    return ImageryDetail(
        id=detail["id"],
        date=detail["date"],
        datetime=detail.get("datetime", ""),
        cloud_cover=detail["cloud_cover"],
        bbox=detail.get("bbox"),
        zarr_path=detail.get("zarr_path"),
        bands=detail.get("metadata", {}).get("bands", []),
        has_cloud_mask=True,
    )


@router.get("/{imagery_id}/tile")
async def get_imagery_tile(
    imagery_id: str,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    width: int = 256,
    height: int = 256,
):
    """
    获取影像切片（PNG 格式）。

    用于前端地图的瓦片加载。根据请求的空间范围裁剪影像，
    返回指定尺寸的 PNG 图像。

    Args:
        imagery_id: 影像 ID
        x_min, x_max, y_min, y_max: 空间裁剪范围
        width, height: 输出图像尺寸
    """
    detail = zarr_service.get_imagery_detail(imagery_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"影像 {imagery_id} 不存在")

    try:
        png_data = zarr_service.get_tile_as_png(
            zarr_path=detail["zarr_path"],
            bbox=(x_min, y_min, x_max, y_max),
            size=(width, height),
        )
        return Response(content=png_data, media_type="image/png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"切片生成失败: {str(e)}")


@router.get("/{imagery_id}/overview")
async def get_imagery_overview(
    imagery_id: str,
    max_size: int = 512,
):
    """
    获取影像概览图（缩略图）。

    返回整个影像的缩略图，用于列表展示。

    Args:
        imagery_id: 影像 ID
        max_size: 最大边长
    """
    detail = zarr_service.get_imagery_detail(imagery_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"影像 {imagery_id} 不存在")

    try:
        png_data = zarr_service.get_overview(
            zarr_path=detail["zarr_path"],
            max_size=max_size,
        )
        return Response(content=png_data, media_type="image/png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"概览图生成失败: {str(e)}")


@router.post("/download")
async def trigger_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    """
    触发新的影像下载任务。

    在后台执行完整的处理管线（搜索 → 下载 → 合成 → 去云 → ZARR 转换）。
    请求立即返回，任务在后台异步执行。

    Args:
        request: 下载请求参数（区域、日期、云量）
    """
    import importlib.util

    # 动态导入管线模块
    scripts_dir = Path(__file__).resolve().parent.parent.parent / "scripts"
    pipeline_path = scripts_dir / "06_pipeline.py"
    spec = importlib.util.spec_from_file_location("pipeline", pipeline_path)
    pipeline_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipeline_module)

    # 在后台任务中执行管线
    background_tasks.add_task(
        pipeline_module.run_pipeline,
        bbox=request.bbox,
        date_range=request.date_range,
        cloud_cover_max=request.cloud_cover_max,
        output_dir=Path(__file__).resolve().parent.parent.parent / "data",
    )

    return {
        "status": "accepted",
        "message": "下载任务已提交，正在后台处理",
        "params": {
            "bbox": request.bbox,
            "date_range": request.date_range,
            "cloud_cover_max": request.cloud_cover_max,
        },
    }
