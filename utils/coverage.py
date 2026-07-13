"""
coverage.py - 覆盖率计算与场景选择模块

功能：
1. 计算单景/多景影像对研究区的覆盖率
2. 按日期分组场景
3. 选择最优场景组合（单日优先，跨日补充）
4. 支持 bbox 和 shp 两种研究区输入
"""

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Union

import geopandas as gpd
from shapely.geometry import Polygon, shape, mapping
from shapely.ops import unary_union


def bbox_to_polygon(bbox: list) -> Polygon:
    """
    将 bbox 转换为 Shapely Polygon。

    Args:
        bbox: [min_lon, min_lat, max_lon, max_lat]

    Returns:
        Polygon: bbox 对应的矩形多边形
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return Polygon([
        (min_lon, min_lat),
        (max_lon, min_lat),
        (max_lon, max_lat),
        (min_lon, max_lat),
        (min_lon, min_lat),
    ])


def load_aoi_geometry(aoi_path: Union[str, Path] = None, bbox: list = None) -> Polygon:
    """
    加载研究区几何对象。

    支持两种输入方式：
    - shp 文件路径
    - bbox 坐标列表

    Args:
        aoi_path: shp 文件路径
        bbox: [min_lon, min_lat, max_lon, max_lat]

    Returns:
        Polygon: 研究区几何对象
    """
    if aoi_path:
        gdf = gpd.read_file(aoi_path)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        return unary_union(gdf.geometry)
    elif bbox:
        return bbox_to_polygon(bbox)
    else:
        raise ValueError("必须提供 aoi_path 或 bbox 之一")


def compute_coverage(scene_geometry: dict, aoi_geom) -> float:
    """
    计算单景影像对研究区的覆盖率。

    覆盖率 = 交集面积 / 研究区面积

    Args:
        scene_geometry: STAC Item 的 geometry 字典（GeoJSON 格式）
        aoi_geom: 研究区几何对象（Shapely Geometry）

    Returns:
        float: 覆盖率（0.0 ~ 1.0）
    """
    scene_geom = shape(scene_geometry)
    aoi_area = aoi_geom.area
    if aoi_area == 0:
        return 0.0
    intersection = scene_geom.intersection(aoi_geom)
    return intersection.area / aoi_area


def compute_union_coverage(scene_geometries: list, aoi_geom) -> float:
    """
    计算多景影像 union 后对研究区的覆盖率。

    Args:
        scene_geometries: STAC Item 的 geometry 列表
        aoi_geom: 研究区几何对象

    Returns:
        float: 联合覆盖率（0.0 ~ 1.0）
    """
    if not scene_geometries:
        return 0.0
    scene_shapes = [shape(g) for g in scene_geometries]
    union_geom = unary_union(scene_shapes)
    aoi_area = aoi_geom.area
    if aoi_area == 0:
        return 0.0
    intersection = union_geom.intersection(aoi_geom)
    return intersection.area / aoi_area


def group_scenes_by_date(items: list) -> dict:
    """
    按日期分组 STAC Item。

    Args:
        items: STAC Item 列表

    Returns:
        dict: {date_str: [item, ...], ...}
    """
    grouped = defaultdict(list)
    for item in items:
        date_str = item.datetime.strftime("%Y-%m-%d")
        grouped[date_str].append(item)
    return dict(grouped)


def _find_minimum_scenes(items: list, aoi_geom, min_coverage: float) -> Tuple[list, float]:
    """
    在同一日期的场景中，找到能覆盖研究区的最小场景集合。

    贪心算法：
    1. 按单景覆盖率降序排列
    2. 依次添加覆盖率最高的场景，直到达到阈值

    Args:
        items: 同日期的 STAC Item 列表
        aoi_geom: 研究区几何对象
        min_coverage: 最低覆盖率阈值

    Returns:
        Tuple[list, float]: (选中的 Item 列表, 覆盖率)
    """
    # 按单景覆盖率降序排列
    items_with_coverage = []
    for item in items:
        cov = compute_coverage(item.geometry, aoi_geom)
        items_with_coverage.append((item, cov))
    items_with_coverage.sort(key=lambda x: x[1], reverse=True)

    selected = []
    selected_geoms = []
    for item, cov in items_with_coverage:
        selected.append(item)
        selected_geoms.append(item.geometry)
        coverage = compute_union_coverage(selected_geoms, aoi_geom)
        if coverage >= min_coverage:
            return selected, coverage

    return selected, compute_union_coverage(selected_geoms, aoi_geom)


def select_optimal_scenes(
    items: list,
    aoi_geom,
    min_coverage: float = 0.95,
) -> Tuple[list, dict]:
    """
    选择最优场景组合。

    策略：
    1. 按日期分组场景
    2. 对每个日期，使用贪心算法找到最小场景集合
    3. 若有单日覆盖率 >= 阈值 → 选该日（平均云量最低的日期）
    4. 若无单日达标 → 按日期从近到远累积场景，直到覆盖率达标

    Args:
        items: STAC Item 列表（已按覆盖率排序）
        aoi_geom: 研究区几何对象
        min_coverage: 最低覆盖率阈值

    Returns:
        Tuple[list, dict]: (选中的 Item 列表, 分析报告)
    """
    if not items:
        return [], {"status": "no_items", "coverage": 0.0}

    grouped = group_scenes_by_date(items)

    date_analysis = []
    for date_str, date_items in grouped.items():
        # 使用贪心算法找到最小场景集合
        min_items, coverage = _find_minimum_scenes(date_items, aoi_geom, min_coverage)
        avg_cloud = sum(
            item.properties.get("eo:cloud_cover", 0) for item in min_items
        ) / len(min_items)
        date_analysis.append({
            "date": date_str,
            "items": min_items,  # 只包含最小必要场景
            "all_items_count": len(date_items),  # 该日期总场景数
            "coverage": coverage,
            "avg_cloud_cover": avg_cloud,
            "scene_count": len(min_items),
        })

    date_analysis.sort(key=lambda x: x["date"], reverse=True)

    qualified_dates = [d for d in date_analysis if d["coverage"] >= min_coverage]
    if qualified_dates:
        qualified_dates.sort(key=lambda x: x["avg_cloud_cover"])
        best = qualified_dates[0]
        selected = best["items"]
        report = {
            "strategy": "single_date",
            "selected_date": best["date"],
            "coverage": best["coverage"],
            "avg_cloud_cover": best["avg_cloud_cover"],
            "scene_count": len(selected),
            "all_items_count": best["all_items_count"],
            "all_dates": [
                {"date": d["date"], "coverage": d["coverage"], "avg_cloud": d["avg_cloud_cover"], "scene_count": d["scene_count"], "all_items_count": d["all_items_count"]}
                for d in date_analysis
            ],
        }
        return selected, report

    selected = []
    selected_geoms = []
    used_dates = []
    for date_info in date_analysis:
        selected.extend(date_info["items"])
        selected_geoms.extend([item.geometry for item in date_info["items"]])
        used_dates.append(date_info["date"])
        coverage = compute_union_coverage(selected_geoms, aoi_geom)
        if coverage >= min_coverage:
            report = {
                "strategy": "multi_date",
                "selected_dates": used_dates,
                "coverage": coverage,
                "scene_count": len(selected),
                "all_dates": [
                    {"date": d["date"], "coverage": d["coverage"], "avg_cloud": d["avg_cloud_cover"], "scene_count": d["scene_count"], "all_items_count": d["all_items_count"]}
                    for d in date_analysis
                ],
            }
            return selected, report

    report = {
        "strategy": "partial",
        "selected_dates": used_dates,
        "coverage": compute_union_coverage(selected_geoms, aoi_geom),
        "scene_count": len(selected),
        "warning": f"无法达到 {min_coverage*100:.0f}% 覆盖率阈值",
        "all_dates": [
            {"date": d["date"], "coverage": d["coverage"], "avg_cloud": d["avg_cloud_cover"], "scene_count": d["scene_count"], "all_items_count": d["all_items_count"]}
            for d in date_analysis
        ],
    }
    return selected, report


def enrich_items_with_coverage(items: list, aoi_geom) -> list:
    """
    为搜索结果添加覆盖率信息，并按覆盖率降序排序。

    Args:
        items: STAC Item 列表
        aoi_geom: 研究区几何对象

    Returns:
        list: 添加了 coverage_ratio 的 Item 列表
    """
    enriched = []
    for item in items:
        coverage = compute_coverage(item.geometry, aoi_geom)
        enriched.append((item, coverage))
    enriched.sort(key=lambda x: x[1], reverse=True)
    return [item for item, _ in enriched]


def print_coverage_report(report: dict) -> None:
    """打印场景选择报告。"""
    print("\n" + "=" * 50)
    print("场景选择报告")
    print("=" * 50)

    strategy = report.get("strategy", "unknown")
    if strategy == "single_date":
        print(f"策略: 单日覆盖")
        print(f"选中日期: {report['selected_date']}")
        print(f"覆盖率: {report['coverage']*100:.1f}%")
        print(f"平均云量: {report['avg_cloud_cover']:.1f}%")
        print(f"选中场景数: {report['scene_count']}")
        if report.get('all_items_count'):
            print(f"该日期总场景数: {report['all_items_count']}")
    elif strategy == "multi_date":
        print(f"策略: 多日拼接")
        print(f"选中日期: {', '.join(report['selected_dates'])}")
        print(f"覆盖率: {report['coverage']*100:.1f}%")
        print(f"场景数: {report['scene_count']}")
    elif strategy == "partial":
        print(f"策略: 部分覆盖（警告）")
        print(f"选中日期: {', '.join(report['selected_dates'])}")
        print(f"覆盖率: {report['coverage']*100:.1f}%")
        print(f"场景数: {report['scene_count']}")
        print(f"警告: {report.get('warning', '')}")

    print("\n各日期覆盖情况:")
    for d in report.get("all_dates", []):
        scene_info = f"场景 {d['scene_count']}/{d['all_items_count']}" if d.get('all_items_count') else f"场景 {d['scene_count']}"
        print(f"  {d['date']}: 覆盖率 {d['coverage']*100:.1f}%, 平均云量 {d['avg_cloud']:.1f}%, {scene_info}")

    print("=" * 50)
