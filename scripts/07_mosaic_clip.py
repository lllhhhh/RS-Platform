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
from shapely.geometry import shape, mapping
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import CLOUD_MASKED_DIR, MERGED_DIR, MIN_COVERAGE_RATIO


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


def _read_scene_coverage(data_dir: Path) -> dict:
    """
    从 metadata.json 读取各场景的覆盖率信息。

    通过场景 ID 前缀匹配 TIF 文件名，返回每个 TIF 对应的覆盖率。

    Args:
        data_dir: 数据目录

    Returns:
        dict: {tif_path: coverage_ratio} 映射，若无 metadata 则返回空字典
    """
    metadata_path = data_dir / "metadata.json"
    if not metadata_path.exists():
        return {}

    with open(metadata_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    # 构建 scene_id → coverage_ratio 映射
    scene_coverage = {}
    for scene in metadata.get("scenes", []):
        scene_id = scene.get("scene_id", "")
        coverage = scene.get("coverage_ratio")
        if scene_id and coverage is not None:
            scene_coverage[scene_id] = coverage

    if not scene_coverage:
        return {}

    # 匹配 TIF 文件名中的 scene_id
    # 文件名格式: {scene_id}_{date}_{cloud}_cloudmasked.tif 或 _RGB.tif 或 _S1_merged.tif
    tif_coverage = {}
    cloud_masked_dir = data_dir / "cloud_masked"
    merged_dir = data_dir / "merged"

    # 扫描所有可能的 TIF 文件
    tif_files = []
    if cloud_masked_dir.exists():
        tif_files.extend(sorted(cloud_masked_dir.glob("*_cloudmasked.tif")))
    if merged_dir.exists():
        tif_files.extend(sorted(merged_dir.glob("*_RGB.tif")))
        tif_files.extend(sorted(merged_dir.glob("*_S1_merged.tif")))

    for tif_file in tif_files:
        fname = tif_file.name
        for scene_id, coverage in scene_coverage.items():
            if fname.startswith(scene_id):
                tif_coverage[tif_file] = coverage
                break

    return tif_coverage


def clip_single_scene(
    scene_path: Path,
    aoi_geom,
    output_path: Path,
) -> Path:
    """
    对单个 TIF 按研究区裁剪（不拼接）。

    Args:
        scene_path: 输入 TIF 文件路径
        aoi_geom: 研究区几何对象（Shapely Geometry，EPSG:4326）
        output_path: 输出文件路径

    Returns:
        Path: 输出文件路径
    """
    print(f"[裁剪] 独立裁剪: {scene_path.name}")

    with rasterio.open(scene_path) as src:
        src_crs = src.crs
        aoi_geom_proj = aoi_geom

        # 将 AOI 几何从 EPSG:4326 转换为影像的 CRS
        if src_crs and src_crs.to_epsg() != 4326:
            import geopandas as gpd
            print(f"[裁剪] 转换 AOI 坐标系: EPSG:4326 → {src_crs}")
            gdf = gpd.GeoDataFrame(geometry=[aoi_geom], crs="EPSG:4326")
            gdf = gdf.to_crs(src_crs)
            aoi_geom_proj = gdf.geometry.iloc[0]

        aoi_geojson = [mapping(aoi_geom_proj)]

        clipped_data, clipped_transform = rasterio_mask(
            src,
            aoi_geojson,
            crop=True,
            nodata=0,
        )
        clipped_profile = src.profile.copy()

    clipped_profile.update(
        driver="GTiff",
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

    return output_path


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


def process_mosaic_clip(
    data_dir: Path,
    min_coverage: float = None,
    scene_ids: set = None,
) -> list:
    """
    对去云后的 TIF 执行拼接和裁剪。

    当多景影像各自覆盖率都 >= min_coverage 时，对每景独立裁剪；
    否则拼接所有影像后统一裁剪。

    Args:
        data_dir: 数据目录
        min_coverage: 最低覆盖率阈值（默认使用配置文件中的值）
        scene_ids: 要处理的 scene_id 集合，为 None 时处理全部

    Returns:
        list[Path]: 输出的 TIF 文件路径列表
    """
    if min_coverage is None:
        min_coverage = MIN_COVERAGE_RATIO

    cloud_masked_dir = data_dir / "cloud_masked"
    merged_dir = data_dir / "merged"

    # 优先从 cloud_masked 目录读取，如果为空则从 merged 目录读取
    tif_files = []
    if cloud_masked_dir.exists():
        tif_files = sorted(cloud_masked_dir.glob("*_cloudmasked.tif"))
    if not tif_files and merged_dir.exists():
        tif_files = sorted(merged_dir.glob("*_cloudmasked.tif"))
    if not tif_files:
        tif_files = sorted(merged_dir.glob("*_RGB.tif")) if merged_dir.exists() else []
    if not tif_files:
        tif_files = sorted(merged_dir.glob("*_S1_merged.tif")) if merged_dir.exists() else []
    if not tif_files:
        print(f"[错误] 未找到 TIF 文件")
        return []

    # 过滤：只处理指定的 scene_id
    if scene_ids is not None:
        tif_files = [f for f in tif_files if any(sid in f.name for sid in scene_ids)]
        print(f"[拼接] 过滤后保留 {len(tif_files)} 个 TIF 文件")
    else:
        print(f"[拼接] 找到 {len(tif_files)} 个 TIF 文件")

    aoi_geom = load_aoi_geometry(data_dir)
    output_dir = data_dir / "mosaicked"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 判断是否可以独立裁剪
    can_clip_independently = False
    if len(tif_files) > 1:
        tif_coverage = _read_scene_coverage(data_dir)
        if tif_coverage:
            all_full = all(
                tif_file in tif_coverage and tif_coverage[tif_file] >= min_coverage
                for tif_file in tif_files
            )
            if all_full:
                can_clip_independently = True
                print(f"[策略] {len(tif_files)} 景各自覆盖率 >= {min_coverage*100:.0f}%，独立裁剪")
        else:
            # 没有覆盖率信息时，对于 S1 影像默认独立裁剪（避免拼接失败）
            # 通过检查文件名判断是否为 S1 影像
            is_s1 = any("_S1_merged.tif" in f.name for f in tif_files)
            if is_s1:
                can_clip_independently = True
                print(f"[策略] S1 影像无覆盖率信息，默认独立裁剪")

    if can_clip_independently:
        results = []
        for tif_file in tif_files:
            # 从文件名生成输出名：去掉后缀，加 _clipped
            out_name = (tif_file.name
                       .replace("_cloudmasked.tif", "_clipped.tif")
                       .replace("_RGB.tif", "_clipped.tif")
                       .replace("_S1_merged.tif", "_clipped.tif"))
            output_path = output_dir / out_name
            result = clip_single_scene(tif_file, aoi_geom, output_path)
            if result:
                results.append(result)
        return results
    else:
        if len(tif_files) > 1:
            print(f"[策略] 多景拼接后裁剪")
        else:
            print(f"[策略] 单景裁剪")
        output_path = output_dir / "mosaicked_clipped.tif"
        result = mosaic_and_clip(tif_files, aoi_geom, output_path)
        return [result] if result else []


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
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=MIN_COVERAGE_RATIO,
        help=f"独立裁剪的最低覆盖率阈值 (默认: {MIN_COVERAGE_RATIO})",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)

    print("=" * 60)
    print("RS-Platform: 多景拼接与研究区裁剪")
    print("=" * 60)

    results = process_mosaic_clip(data_dir, min_coverage=args.min_coverage)

    if results:
        print(f"\n输出文件 ({len(results)} 个):")
        for r in results:
            print(f"  {r}")

    print("=" * 60)


if __name__ == "__main__":
    main()
