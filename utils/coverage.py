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


def analyze_coverage_by_date(
    items: list,
    aoi_geom,
    min_coverage: float = 0.95,
) -> Tuple[list, dict]:
    """
    按日期分析各时相的覆盖率情况（不做选择）。

    对每个日期，使用贪心算法找到能覆盖研究区的最小场景集合，
    并计算该日期的联合覆盖率和平均云量。

    Args:
        items: STAC Item 列表（已按覆盖率排序）
        aoi_geom: 研究区几何对象
        min_coverage: 最低覆盖率阈值

    Returns:
        Tuple[list, dict]: (按日期分组的分析结果列表, 报告)
    """
    if not items:
        return [], {"status": "no_items", "coverage": 0.0}

    grouped = group_scenes_by_date(items)

    date_analysis = []
    for date_str, date_items in grouped.items():
        min_items, coverage = _find_minimum_scenes(date_items, aoi_geom, min_coverage)
        avg_cloud = sum(
            item.properties.get("eo:cloud_cover", 0) for item in min_items
        ) / len(min_items)
        date_analysis.append({
            "date": date_str,
            "items": min_items,
            "all_items_count": len(date_items),
            "coverage": coverage,
            "avg_cloud_cover": avg_cloud,
            "scene_count": len(min_items),
            "qualified": coverage >= min_coverage,
        })

    date_analysis.sort(key=lambda x: x["date"], reverse=True)

    qualified_count = sum(1 for d in date_analysis if d["qualified"])
    report = {
        "status": "analyzed",
        "total_dates": len(date_analysis),
        "qualified_dates": qualified_count,
        "min_coverage": min_coverage,
        "all_dates": [
            {
                "date": d["date"],
                "coverage": d["coverage"],
                "avg_cloud": d["avg_cloud_cover"],
                "scene_count": d["scene_count"],
                "all_items_count": d["all_items_count"],
                "qualified": d["qualified"],
            }
            for d in date_analysis
        ],
    }

    return date_analysis, report


def interactive_select_dates(
    date_analysis: list,
    min_coverage: float = 0.95,
) -> Tuple[list, dict]:
    """
    交互式让用户选择要下载的时相。

    显示所有达标日期的覆盖率和云量信息，用户输入序号选择。
    不达标日期也会展示，但标记为不达标。

    Args:
        date_analysis: analyze_coverage_by_date 返回的分析结果
        min_coverage: 最低覆盖率阈值（用于提示）

    Returns:
        Tuple[list, dict]: (选中的 Item 列表, 报告)
    """
    if not date_analysis:
        return [], {"status": "no_dates"}

    print("\n" + "=" * 60)
    print("可选时相列表")
    print("=" * 60)
    print(f"{'序号':>4}  {'日期':<12}  {'覆盖率':>8}  {'平均云量':>8}  {'场景数':>6}  {'状态'}")
    print("-" * 60)

    for i, d in enumerate(date_analysis):
        status = "达标" if d["qualified"] else "不达标"
        scene_info = f"{d['scene_count']}/{d['all_items_count']}"
        print(
            f"  {i+1:<3}  {d['date']:<12}  {d['coverage']*100:>7.1f}%  "
            f"{d['avg_cloud_cover']:>7.1f}%  {scene_info:>6}  {status}"
        )

    print("-" * 60)
    qualified_dates = [d for d in date_analysis if d["qualified"]]
    if qualified_dates:
        best = min(qualified_dates, key=lambda x: x["avg_cloud_cover"])
        print(f"推荐: 序号 {date_analysis.index(best)+1}（{best['date']}，云量最低的达标时相）")
    print(f"提示: 输入序号选择（多个用逗号分隔），输入 all 选择所有达标时相")

    while True:
        try:
            user_input = input("\n请选择要下载的时相序号: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return [], {"status": "cancelled"}

        if not user_input:
            print("请输入序号")
            continue

        if user_input.lower() == "all":
            selected_dates = [d for d in date_analysis if d["qualified"]]
            if not selected_dates:
                print("没有达标时相可选")
                continue
            break

        try:
            indices = [int(x.strip()) for x in user_input.split(",")]
        except ValueError:
            print("输入格式错误，请输入数字序号（多个用逗号分隔）")
            continue

        if any(i < 1 or i > len(date_analysis) for i in indices):
            print(f"序号超出范围，请输入 1~{len(date_analysis)}")
            continue

        selected_dates = [date_analysis[i - 1] for i in indices]
        unqualified = [d for d in selected_dates if not d["qualified"]]
        if unqualified:
            names = ", ".join(d["date"] for d in unqualified)
            print(f"注意: {names} 未达标，是否继续？")
            try:
                confirm = input("输入 y 继续，其他取消: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n已取消")
                return [], {"status": "cancelled"}
            if confirm != "y":
                continue
        break

    selected_items = []
    selected_dates_info = []
    for d in selected_dates:
        selected_items.extend(d["items"])
        selected_dates_info.append(d["date"])

    print(f"\n已选择 {len(selected_dates)} 个时相，共 {len(selected_items)} 景影像")

    report = {
        "strategy": "user_selected",
        "selected_dates": selected_dates_info,
        "scene_count": len(selected_items),
        "all_dates": [
            {
                "date": d["date"],
                "coverage": d["coverage"],
                "avg_cloud": d["avg_cloud_cover"],
                "scene_count": d["scene_count"],
                "all_items_count": d["all_items_count"],
                "qualified": d["qualified"],
            }
            for d in date_analysis
        ],
    }

    return selected_items, report


def select_optimal_scenes(
    items: list,
    aoi_geom,
    min_coverage: float = 0.95,
    auto_select: bool = False,
) -> Tuple[list, dict]:
    """
    选择最优场景组合。

    策略：
    1. 按日期分组场景
    2. 对每个日期，使用贪心算法找到最小场景集合
    3. auto_select=False（默认）: 展示各日期覆盖率，让用户选择
    4. auto_select=True: 若有单日覆盖率 >= 阈值 → 选该日（平均云量最低的日期）
       若无单日达标 → 按日期从近到远累积场景，直到覆盖率达标

    Args:
        items: STAC Item 列表（已按覆盖率排序）
        aoi_geom: 研究区几何对象
        min_coverage: 最低覆盖率阈值
        auto_select: 是否自动选择最优（跳过交互）

    Returns:
        Tuple[list, dict]: (选中的 Item 列表, 分析报告)
    """
    if not items:
        return [], {"status": "no_items", "coverage": 0.0}

    date_analysis, report = analyze_coverage_by_date(items, aoi_geom, min_coverage)

    if not auto_select:
        return interactive_select_dates(date_analysis, min_coverage)

    qualified_dates = [d for d in date_analysis if d["qualified"]]
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
        print(f"策略: 单日覆盖（自动选择）")
        print(f"选中日期: {report['selected_date']}")
        print(f"覆盖率: {report['coverage']*100:.1f}%")
        print(f"平均云量: {report['avg_cloud_cover']:.1f}%")
        print(f"选中场景数: {report['scene_count']}")
        if report.get('all_items_count'):
            print(f"该日期总场景数: {report['all_items_count']}")
    elif strategy == "user_selected":
        print(f"策略: 用户选择")
        print(f"选中日期: {', '.join(report['selected_dates'])}")
        print(f"场景数: {report['scene_count']}")
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
        qualified_mark = " *" if d.get("qualified") else ""
        print(f"  {d['date']}: 覆盖率 {d['coverage']*100:.1f}%, 平均云量 {d['avg_cloud']:.1f}%, {scene_info}{qualified_mark}")

    if any(d.get("qualified") for d in report.get("all_dates", [])):
        print("\n  * = 达标时相")

    print("=" * 50)
