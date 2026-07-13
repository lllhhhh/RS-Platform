"""
07_mosaic_clip.py - 多景影像拼接与研究区裁剪

功能：
1. 读取去云后的多景 TIF 文件
2. 使用 rasterio.merge 拼接为一张大影像
3. 使用 rasterio.mask 按研究区几何裁剪
4. 输出完整覆盖研究区的单张 TIF

使用方法：
    python scripts/07_mosaic_clip.py --data-dir ./data
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import rasterio
from rasterio.merge import merge
from rasterio.mask import mask as rasterio_mask
from shapely.geometry import shape
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import CLOUD_MASKED_DIR, MERGED_DIR


def load_aoi_geometry(data_dir: Path):
    """
    从 data 目录加载研究区几何对象。

    Args:
        data_dir: 数据目录

    Returns:
        Shapely Geometry 或 None
    """
    aoi_path = data_dir / "aoi_geometry.json"
    if not aoi_path.exists():
        return None
    with open(aoi_path, "r", encoding="utf-8") as f:
        geom_dict = json.load(f)
    return shape(geom_dict)


def scan_tif_by_scene_group(tif_dir: Path) -> dict:
    """
    扫描 TIF 文件，按 scene_id 前缀分组。

    对于去云后的文件，文件名格式为:
    {scene_id}_{date}_{cloud_cover}_RGB_cloudmasked.tif

    Args:
        tif_dir: TIF 文件目录

    Returns:
        dict: {group_key: [tif_path, ...]}
    """
    groups = defaultdict(list)
    for tif_file in sorted(tif_dir.glob("*_cloudmasked.tif")):
        groups["all"].append(tif_file)
    if not groups["all"]:
        for tif_file in sorted(tif_dir.glob("*_RGB.tif")):
            groups["all"].append(tif_file)
    return dict(groups)


def mosaic_and_clip(
    scene_paths: list,
    aoi_geom,
    output_path: Path,
) -> Path:
    """
    将多景影像拼接并裁剪到研究区。

    处理流程：
    1. 读取所有待拼接的 TIF 文件
    2. rasterio.merge.merge() 拼接
    3. rasterio.mask.mask() 按研究区裁剪
    4. 输出完整覆盖研究区的 TIF

    Args:
        scene_paths: TIF 文件路径列表
        aoi_geom: 研究区几何对象（Shapely Geometry）
        output_path: 输出文件路径

    Returns:
        Path: 输出文件路径
    """
    if not scene_paths:
        print("[拼接] 无待拼接文件")
        return None

    print(f"[拼接] 拼接 {len(scene_paths)} 个文件...")

    src_files = [rasterio.open(p) for p in scene_paths]
    try:
        mosaic_data, mosaic_transform = merge(src_files)
        mosaic_crs = src_files[0].crs
        mosaic_profile = src_files[0].profile.copy()
    finally:
        for src in src_files:
            src.close()

    mosaic_profile.update(
        driver="GTiff",
        height=mosaic_data.shape[1],
        width=mosaic_data.shape[2],
        transform=mosaic_transform,
        count=mosaic_data.shape[0],
    )

    if aoi_geom is not None:
        print(f"[裁剪] 按研究区几何裁剪...")

        # 将 AOI 几何从 EPSG:4326 转换为影像的 CRS
        import geopandas as gpd
        from shapely.geometry import mapping

        if mosaic_crs and not mosaic_crs.to_epsg() == 4326:
            print(f"[裁剪] 转换 AOI 坐标系: EPSG:4326 → {mosaic_crs}")
            gdf = gpd.GeoDataFrame(geometry=[aoi_geom], crs="EPSG:4326")
            gdf = gdf.to_crs(mosaic_crs)
            aoi_geom_transformed = gdf.geometry.iloc[0]
        else:
            aoi_geom_transformed = aoi_geom

        aoi_geojson = [mapping(aoi_geom_transformed)]

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            with rasterio.open(tmp_path, "w", **mosaic_profile) as tmp_dst:
                tmp_dst.write(mosaic_data)

            with rasterio.open(tmp_path) as tmp_src:
                clipped_data, clipped_transform = rasterio_mask(
                    tmp_src,
                    aoi_geojson,
                    crop=True,
                    nodata=0,
                )
                clipped_profile = tmp_src.profile.copy()
        finally:
            tmp_path.unlink(missing_ok=True)

        clipped_profile.update(
            height=clipped_data.shape[1],
            width=clipped_data.shape[2],
            transform=clipped_transform,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(output_path, "w", **clipped_profile) as dst:
            dst.write(clipped_data)

        file_size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"[完成] {output_path.name} ({file_size_mb:.1f} MB)")
        print(f"  尺寸: {clipped_data.shape[2]} x {clipped_data.shape[1]} 像素")
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(output_path, "w", **mosaic_profile) as dst:
            dst.write(mosaic_data)

        file_size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"[完成] {output_path.name} ({file_size_mb:.1f} MB)")
        print(f"  尺寸: {mosaic_data.shape[2]} x {mosaic_data.shape[1]} 像素")

    return output_path


def process_mosaic_clip(data_dir: Path) -> Path:
    """
    对去云后的 TIF 执行拼接和裁剪。

    Args:
        data_dir: 数据目录

    Returns:
        Path: 拼接裁剪后的 TIF 路径
    """
    cloud_masked_dir = data_dir / "cloud_masked"
    merged_dir = data_dir / "merged"

    tif_dir = cloud_masked_dir if cloud_masked_dir.exists() else merged_dir
    if not tif_dir.exists():
        print(f"[错误] 目录不存在: {tif_dir}")
        return None

    tif_files = sorted(tif_dir.glob("*_cloudmasked.tif"))
    if not tif_files:
        tif_files = sorted(tif_dir.glob("*_RGB.tif"))
    if not tif_files:
        print(f"[错误] 未找到 TIF 文件: {tif_dir}")
        return None

    print(f"[拼接] 找到 {len(tif_files)} 个 TIF 文件")

    aoi_geom = load_aoi_geometry(data_dir)

    output_dir = data_dir / "mosaicked"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "mosaicked_clipped.tif"

    result = mosaic_and_clip(tif_files, aoi_geom, output_path)
    return result


def main():
    """主函数：解析参数并执行拼接裁剪。"""
    parser = argparse.ArgumentParser(
        description="多景影像拼接与研究区裁剪工具",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="数据目录",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)

    print("=" * 60)
    print("RS-Platform: 多景拼接与研究区裁剪")
    print("=" * 60)

    result = process_mosaic_clip(data_dir)

    if result:
        print(f"\n输出文件: {result}")

    print("=" * 60)


if __name__ == "__main__":
    main()
