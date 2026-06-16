"""
zarr_service.py - ZARR 数据读取服务

提供 ZARR 数据的读取、裁剪、降采样等操作。
后端通过此服务读取 ZARR 数据，返回给前端进行展示。

ZARR 格式优势：
- 分块存储：只需加载请求区域的 chunk，无需读取整个文件
- 压缩存储：节省磁盘空间和网络传输
- 并发读取：支持多用户同时访问
"""

import json
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import xarray as xr

from config.settings import ZARR_DIR


class ZarrService:
    """ZARR 数据读取服务"""

    def __init__(self, zarr_dir: Path = ZARR_DIR):
        """
        初始化 ZARR 服务。

        Args:
            zarr_dir: ZARR 数据目录
        """
        self.zarr_dir = Path(zarr_dir)

    def list_available_imagery(self) -> List[dict]:
        """
        列出所有可用的 ZARR 影像。

        扫描 ZARR 目录，返回每个影像的基本信息。

        Returns:
            List[dict]: 影像信息列表
        """
        imagery_list = []

        if not self.zarr_dir.exists():
            return imagery_list

        for zarr_path in sorted(self.zarr_dir.glob("*.zarr")):
            # 从目录名提取信息
            name = zarr_path.stem

            # 尝试读取自定义元数据
            metadata_path = zarr_path / "_rs_metadata.json"
            metadata = {}
            if metadata_path.exists():
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)

            # 从文件名解析 scene_id 和日期
            # 文件名格式: {scene_id}_{date}_{cloud_cover}_RGB_cloudmasked
            parts = name.split("_")
            scene_id = parts[0] if len(parts) > 0 else name
            date = parts[1] if len(parts) > 1 else "unknown"
            cloud_cover = parts[2] if len(parts) > 2 else 0

            imagery_list.append({
                "id": scene_id,
                "name": name,
                "date": date,
                "cloud_cover": float(cloud_cover) if cloud_cover else 0,
                "zarr_path": str(zarr_path),
                "has_zarr": True,
                "metadata": metadata,
            })

        return imagery_list

    def get_imagery_detail(self, imagery_id: str) -> Optional[dict]:
        """
        获取指定影像的详细信息。

        Args:
            imagery_id: 影像 ID

        Returns:
            dict: 影像详细信息，未找到则返回 None
        """
        all_imagery = self.list_available_imagery()
        for item in all_imagery:
            if item["id"] == imagery_id:
                return item
        return None

    def read_zarr_data(
        self,
        zarr_path: str,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        band: Optional[int] = None,
    ) -> xr.DataArray:
        """
        读取 ZARR 数据。

        Args:
            zarr_path: ZARR 目录路径
            bbox: 可选的空间裁剪范围 (min_x, min_y, max_x, max_y)
            band: 可选的波段索引（1=Red, 2=Green, 3=Blue）

        Returns:
            xr.DataArray: 读取的数据
        """
        # 使用 xarray 打开 ZARR
        da = xr.open_zarr(zarr_path)

        # 如果指定了波段，选择该波段
        if band is not None:
            da = da.sel(band=band)

        # 如果指定了空间范围，进行裁剪
        if bbox is not None:
            min_x, min_y, max_x, max_y = bbox
            da = da.sel(
                x=slice(min_x, max_x),
                y=slice(max_y, min_y),  # 注意：y 轴通常是反向的
            )

        return da

    def get_tile_as_png(
        self,
        zarr_path: str,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        size: Tuple[int, int] = (256, 256),
    ) -> bytes:
        """
        获取影像切片并转换为 PNG 格式。

        用于前端地图展示。

        Args:
            zarr_path: ZARR 目录路径
            bbox: 空间裁剪范围
            size: 输出图像尺寸 (width, height)

        Returns:
            bytes: PNG 图像数据
        """
        from PIL import Image
        import io

        # 读取数据
        da = self.read_zarr_data(zarr_path, bbox=bbox)

        # 获取 RGB 三个波段
        if "band" in da.dims:
            # 取前3个波段 (Red, Green, Blue)
            rgb = da.sel(band=slice(1, 3))
            # 转换为 (height, width, 3) 格式
            data = rgb.values
            if data.ndim == 3:
                data = np.transpose(data, (1, 2, 0))
        else:
            data = da.values

        # 归一化到 0-255
        if data.dtype != np.uint8:
            # 简单的百分比截断归一化
            p2, p98 = np.percentile(data[~np.isnan(data)], (2, 98))
            data = np.clip((data - p2) / (p98 - p2) * 255, 0, 255).astype(np.uint8)

        # 创建 PIL 图像并调整大小
        img = Image.fromarray(data)
        img = img.resize(size, Image.Resampling.LANCZOS)

        # 转换为 PNG 字节
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()

    def get_overview(
        self,
        zarr_path: str,
        max_size: int = 512,
    ) -> bytes:
        """
        获取影像概览图（缩略图）。

        Args:
            zarr_path: ZARR 目录路径
            max_size: 最大边长

        Returns:
            bytes: PNG 图像数据
        """
        from PIL import Image
        import io

        # 读取全部数据
        da = self.read_zarr_data(zarr_path)

        # 获取 RGB 波段
        if "band" in da.dims:
            rgb = da.sel(band=slice(1, 3))
            data = rgb.values
            if data.ndim == 3:
                data = np.transpose(data, (1, 2, 0))
        else:
            data = da.values

        # 归一化
        if data.dtype != np.uint8:
            p2, p98 = np.percentile(data[~np.isnan(data)], (2, 98))
            data = np.clip((data - p2) / (p98 - p2) * 255, 0, 255).astype(np.uint8)

        # 创建图像并按比例缩放
        img = Image.fromarray(data)
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()
