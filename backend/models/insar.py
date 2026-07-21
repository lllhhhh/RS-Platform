"""
insar.py - InSAR 处理数据模型

定义 InSAR 处理相关的 Pydantic 模型，用于 API 请求和响应的数据验证。
"""

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class SLCDownloadRequest(BaseModel):
    """SLC 数据下载请求"""

    bbox: List[float] = Field(
        ...,
        description="搜索区域边界框 [min_lon, min_lat, max_lon, max_lat]",
        min_length=4,
        max_length=4,
    )
    date_range: str = Field(
        ...,
        description="日期范围，格式: YYYY-MM-DD/YYYY-MM-DD",
    )
    orbit_direction: Optional[str] = Field(
        default=None,
        description="轨道方向: ASCENDING 或 DESCENDING（可选，不指定则自动选择）",
    )
    output_dir: Optional[str] = Field(
        default=None,
        description="输出目录路径（默认 data/tasks/TIMESTAMP_S1_SLC）",
    )


class SLCDownloadResponse(BaseModel):
    """SLC 数据下载响应"""

    status: str = Field(..., description="下载状态: accepted/running/completed/error")
    task_id: str = Field(..., description="任务 ID")
    message: str = Field(default="", description="消息")
    output_dir: Optional[str] = Field(None, description="输出目录")
    downloaded: List[str] = Field(default_factory=list, description="已下载的文件列表")


class InSARProcessRequest(BaseModel):
    """InSAR 处理请求

    支持三种数据来源方式（优先级：master/slave > slc_dir > data_dir）：
    - master + slave: 直接指定主从影像路径
    - slc_dir: 指定 SLC 数据目录，交互式选择
    - data_dir: 从任务目录中自动查找 SLC 数据
    """

    data_dir: Optional[str] = Field(
        default=None,
        description="数据目录路径（包含 downloads/s1_slc 子目录）",
    )
    slc_dir: Optional[str] = Field(
        default=None,
        description="SLC 数据目录路径（直接指定，优先于 data_dir）",
    )
    master: Optional[str] = Field(
        default=None,
        description="主影像路径（SAFE 目录或 TIF 文件）",
    )
    slave: Optional[str] = Field(
        default=None,
        description="从影像路径（SAFE 目录或 TIF 文件）",
    )
    polarization: str = Field(
        default="vv",
        description="极化通道: vv 或 vh",
        pattern="^(vv|vh)$",
    )
    output_dir: Optional[str] = Field(
        default=None,
        description="输出目录路径（默认自动生成）",
    )


class InSARDeformationStats(BaseModel):
    """形变统计信息"""

    mean_mm: float = Field(..., description="平均形变量（mm）")
    std_mm: float = Field(..., description="形变标准差（mm）")
    max_uplift_mm: float = Field(..., description="最大抬升量（mm）")
    max_subsidence_mm: float = Field(..., description="最大沉降量（mm）")
    valid_pixels: int = Field(..., description="有效像素数量")


class InSARProcessResponse(BaseModel):
    """InSAR 处理响应"""

    status: str = Field(..., description="处理状态: success 或 error")
    task_id: str = Field(..., description="任务 ID")
    output_dir: Optional[str] = Field(None, description="输出目录路径")
    files: Dict[str, str] = Field(default_factory=dict, description="输出文件列表 {名称: 路径}")
    deformation: Optional[InSARDeformationStats] = Field(None, description="形变统计信息")
    message: str = Field(default="", description="处理消息")


class InSARTaskInfo(BaseModel):
    """InSAR 任务信息"""

    task_id: str = Field(..., description="任务 ID")
    status: str = Field(..., description="任务状态: pending/running/completed/failed")
    master: str = Field(..., description="主影像名称")
    slave: str = Field(..., description="从影像名称")
    polarization: str = Field(..., description="极化通道")
    created_at: str = Field(..., description="创建时间")
    completed_at: Optional[str] = Field(None, description="完成时间")
    output_dir: Optional[str] = Field(None, description="输出目录")
    error: Optional[str] = Field(None, description="错误信息")


class InSARListResponse(BaseModel):
    """InSAR 任务列表响应"""

    tasks: List[InSARTaskInfo] = Field(default_factory=list, description="任务列表")
    total: int = Field(..., description="任务总数")
