"""
imagery.py - 影像数据模型

定义影像元数据的 Pydantic 模型，用于 API 请求和响应的数据验证。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class BandInfo(BaseModel):
    """单个波段的信息"""
    name: str = Field(..., description="波段名称，如 B02, B03, B04")
    url: str = Field(..., description="原始下载 URL")
    filename: str = Field(..., description="本地文件名")
    resolution_m: int = Field(..., description="分辨率（米）")


class SceneMetadata(BaseModel):
    """单个影像场景的元数据"""
    scene_id: str = Field(..., description="场景 ID")
    datetime: str = Field(..., description="影像采集时间")
    date: str = Field(..., description="采集日期 YYYYMMDD")
    cloud_cover: float = Field(..., description="云量百分比")
    bbox: Optional[List[float]] = Field(None, description="边界框 [min_lon, min_lat, max_lon, max_lat]")
    coverage_ratio: Optional[float] = Field(None, description="对研究区的覆盖率 (0.0~1.0)")
    bands: dict = Field(default_factory=dict, description="各波段信息")


class ImageryListItem(BaseModel):
    """影像列表项"""
    id: str = Field(..., description="影像 ID（scene_id）")
    date: str = Field(..., description="采集日期")
    cloud_cover: float = Field(..., description="云量百分比")
    has_zarr: bool = Field(False, description="是否已转换为 ZARR")
    zarr_path: Optional[str] = Field(None, description="ZARR 文件路径")


class ImageryDetail(BaseModel):
    """影像详细信息"""
    id: str = Field(..., description="影像 ID")
    date: str = Field(..., description="采集日期")
    datetime: str = Field(..., description="完整采集时间")
    cloud_cover: float = Field(..., description="云量百分比")
    bbox: Optional[List[float]] = Field(None, description="边界框")
    zarr_path: Optional[str] = Field(None, description="ZARR 文件路径")
    tif_path: Optional[str] = Field(None, description="TIF 文件路径")
    bands: dict = Field(default_factory=dict, description="波段信息")
    has_cloud_mask: bool = Field(False, description="是否已去云")


class DownloadRequest(BaseModel):
    """影像下载请求

    研究区输入方式（优先级：adcode > admin_name > aoi_path > bbox）：
    - adcode: 行政区划代码（如 "110000"）
    - admin_name: 行政区划名称（如 "北京市"）
    - aoi_path: SHP 文件路径
    - bbox: 边界框 [min_lon, min_lat, max_lon, max_lat]
    """
    bbox: Optional[List[float]] = Field(
        default=None,
        description="搜索区域边界框 [min_lon, min_lat, max_lon, max_lat]",
        min_length=4,
        max_length=4,
    )
    adcode: Optional[str] = Field(
        default=None,
        description="行政区划代码（如 '110000'），自动从 DataV 获取边界",
    )
    admin_name: Optional[str] = Field(
        default=None,
        description="行政区划名称（如 '北京市'），模糊搜索并自动获取边界",
    )
    aoi_path: Optional[str] = Field(
        default=None,
        description="研究区 SHP 文件路径",
    )
    date_range: str = Field(
        ...,
        description="日期范围，格式: YYYY-MM-DD/YYYY-MM-DD",
    )
    cloud_cover_max: int = Field(
        default=20,
        description="最大云量百分比",
        ge=0,
        le=100,
    )


class ZarrTileRequest(BaseModel):
    """ZARR 切片请求"""
    x_min: float = Field(..., description="X 最小值（经度或像素坐标）")
    x_max: float = Field(..., description="X 最大值")
    y_min: float = Field(..., description="Y 最小值（纬度或像素坐标）")
    y_max: float = Field(..., description="Y 最大值")
    band: Optional[int] = Field(None, description="波段索引（1=Red, 2=Green, 3=Blue）")
