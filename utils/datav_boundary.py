"""
datav_boundary.py - 阿里云 DataV 行政区划边界获取模块

功能：
1. 从阿里云 DataV API 获取行政区划 GeoJSON 数据
2. 根据名称模糊搜索行政区划 adcode
3. 将 GeoJSON 转换为 SHP 文件
4. 本地缓存机制：index.json 索引 + 按需获取 GeoJSON

缓存策略：
- 首次运行：按树形结构（国家→省→市→区县）获取所有行政区划，保存到本地
- 后续运行：直接读取本地 index.json 索引，按需获取 GeoJSON

本地缓存结构：
    data/boundaries/
    ├── index.json       # 行政区划索引（adcode → name → level 映射）
    ├── 100000.json      # 全国 GeoJSON
    ├── 110000.json      # 北京市 GeoJSON
    ├── 110100.json      # 北京市辖区 GeoJSON
    └── ...

DataV API 端点：
- 获取边界：https://geo.datav.aliyun.com/areas_v3/bound/{adcode}.json
- 获取下级（含子区域）：https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import geopandas as gpd
import requests

# DataV API 基础 URL
DATAV_API_BASE = "https://geo.datav.aliyun.com/areas_v3/bound"


def _get_boundaries_dir(output_dir: Path = None) -> Path:
    """获取行政区划缓存目录"""
    if output_dir is None:
        return Path("data") / "boundaries"
    return output_dir


def _fetch_full_boundary(adcode: str) -> dict:
    """
    从 DataV API 获取行政区划 GeoJSON（含下级区域列表）。

    Args:
        adcode: 行政区划代码

    Returns:
        dict: GeoJSON 字典
    """
    url = f"{DATAV_API_BASE}/{adcode}_full.json"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_boundary_geojson(adcode: str) -> dict:
    """
    从 DataV API 获取行政区划边界 GeoJSON（不含下级）。

    Args:
        adcode: 行政区划代码

    Returns:
        dict: GeoJSON 字典
    """
    url = f"{DATAV_API_BASE}/{adcode}.json"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _save_geojson(geojson: dict, path: Path) -> None:
    """保存 GeoJSON 到本地文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False)


def _load_geojson(path: Path) -> dict:
    """从本地文件加载 GeoJSON"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_areas_index(boundaries_dir: Path) -> dict:
    """
    按树形结构构建行政区划索引。

    流程：
    1. 获取全国 _full（含省份列表）
    2. 遍历每个省份，获取 _full（含城市列表）
    3. 遍历每个城市，获取 _full（含区县列表）
    4. 保存每个层级的 GeoJSON 为 {adcode}.json
    5. 汇总为 index.json 索引文件

    Args:
        boundaries_dir: 缓存目录

    Returns:
        dict: 索引数据
    """
    print("[DataV] 首次运行，构建行政区划索引...")
    print("[DataV] 这可能需要几分钟时间，请耐心等待...")

    areas = []
    boundaries_dir.mkdir(parents=True, exist_ok=True)

    # 1. 获取全国数据
    print("[DataV] 获取全国行政区划...")
    try:
        national = _fetch_full_boundary("100000")
        _save_geojson(national, boundaries_dir / "100000.json")
        areas.append({"adcode": "100000", "name": "中华人民共和国", "level": "country", "parent": None})
    except Exception as e:
        print(f"[DataV] 获取全国数据失败: {e}")
        return {"generated_at": datetime.now().isoformat(), "areas": []}

    # 2. 遍历省份
    provinces = []
    for feature in national.get("features", []):
        props = feature.get("properties", {})
        adcode = props.get("adcode")
        name = props.get("name")
        if adcode and name:
            provinces.append({"adcode": str(adcode), "name": name})
            areas.append({"adcode": str(adcode), "name": name, "level": "province", "parent": "100000"})

    print(f"[DataV] 找到 {len(provinces)} 个省级行政区")

    # 3. 遍历每个省份，获取城市
    for i, province in enumerate(provinces):
        print(f"[DataV] [{i+1}/{len(provinces)}] 获取 {province['name']} 下辖城市...")
        try:
            province_full = _fetch_full_boundary(province["adcode"])
            _save_geojson(_fetch_boundary_geojson(province["adcode"]), boundaries_dir / f"{province['adcode']}.json")

            cities = []
            for feature in province_full.get("features", []):
                props = feature.get("properties", {})
                city_adcode = props.get("adcode")
                city_name = props.get("name")
                if city_adcode and city_name:
                    cities.append({"adcode": str(city_adcode), "name": city_name})
                    areas.append({
                        "adcode": str(city_adcode),
                        "name": city_name,
                        "level": "city",
                        "parent": province["adcode"],
                    })

            # 4. 遍历每个城市，获取区县
            for j, city in enumerate(cities):
                try:
                    city_full = _fetch_full_boundary(city["adcode"])
                    _save_geojson(_fetch_boundary_geojson(city["adcode"]), boundaries_dir / f"{city['adcode']}.json")

                    for feature in city_full.get("features", []):
                        props = feature.get("properties", {})
                        district_adcode = props.get("adcode")
                        district_name = props.get("name")
                        if district_adcode and district_name:
                            areas.append({
                                "adcode": str(district_adcode),
                                "name": district_name,
                                "level": "district",
                                "parent": city["adcode"],
                            })
                except Exception:
                    pass

        except Exception as e:
            print(f"[DataV] 获取 {province['name']} 数据失败: {e}")

    # 5. 保存索引
    index = {
        "generated_at": datetime.now().isoformat(),
        "areas": areas,
    }
    index_path = boundaries_dir / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"[DataV] 索引构建完成，共 {len(areas)} 个行政区划")
    return index


def load_areas_index(output_dir: Path = None) -> dict:
    """
    加载行政区划索引。

    若本地 index.json 存在则直接读取，否则调用 _build_areas_index() 构建。

    Args:
        output_dir: 缓存目录

    Returns:
        dict: 索引数据，包含 "areas" 列表
    """
    boundaries_dir = _get_boundaries_dir(output_dir)
    index_path = boundaries_dir / "index.json"

    if index_path.exists():
        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f)

    return _build_areas_index(boundaries_dir)


def search_adcode_by_name(name: str, output_dir: Path = None) -> list:
    """
    根据名称模糊搜索行政区划 adcode。

    从本地 index.json 索引中搜索，不发起网络请求。

    Args:
        name: 行政区划名称（支持模糊匹配，如 "北京"、"杭州"、"朝阳"）
        output_dir: 缓存目录

    Returns:
        list: 匹配结果列表，每项包含 adcode、name、level
    """
    index = load_areas_index(output_dir)
    areas = index.get("areas", [])

    # 精确匹配优先
    exact = [a for a in areas if a["name"] == name]
    if exact:
        return exact

    # 模糊匹配（包含）
    fuzzy = [a for a in areas if name in a["name"]]
    return fuzzy


def select_from_matches(matches: list) -> dict:
    """
    从多个匹配结果中选择一个。

    如果只有一个匹配，直接返回。
    如果有多个匹配，打印列表让用户选择。

    Args:
        matches: 匹配结果列表

    Returns:
        dict: 选中的行政区划信息
    """
    if not matches:
        raise ValueError("未找到匹配的行政区划")

    if len(matches) == 1:
        return matches[0]

    print(f"\n找到 {len(matches)} 个匹配的行政区划：")
    print("-" * 50)
    for i, m in enumerate(matches):
        level_cn = {"province": "省", "city": "市", "district": "区县", "country": "国家"}.get(m["level"], m["level"])
        print(f"  [{i+1}] {m['name']} ({level_cn}, adcode={m['adcode']})")
    print("-" * 50)

    while True:
        try:
            choice = input(f"请选择 (1-{len(matches)}): ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(matches):
                return matches[idx]
            print(f"请输入 1-{len(matches)} 之间的数字")
        except ValueError:
            print("请输入有效的数字")
        except (EOFError, KeyboardInterrupt):
            raise ValueError("用户取消选择")


def fetch_boundary_by_adcode(adcode: str, output_dir: Path = None) -> dict:
    """
    获取行政区划 GeoJSON（带本地缓存）。

    先检查本地缓存，有则直接读取，无则从 API 获取并保存。

    Args:
        adcode: 行政区划代码（如 "110000"）
        output_dir: 缓存目录

    Returns:
        dict: GeoJSON 字典
    """
    boundaries_dir = _get_boundaries_dir(output_dir)
    cache_path = boundaries_dir / f"{adcode}.json"

    # 检查缓存
    if cache_path.exists():
        print(f"[DataV] 使用缓存: {cache_path}")
        return _load_geojson(cache_path)

    # 从 API 获取
    print(f"[DataV] 从 API 获取: {adcode}")
    geojson = _fetch_boundary_geojson(adcode)

    if "features" not in geojson:
        raise ValueError(f"DataV API 返回数据格式异常（缺少 features）")

    if not geojson["features"]:
        raise ValueError(f"未找到 adcode={adcode} 的行政区划数据")

    # 保存缓存
    _save_geojson(geojson, cache_path)
    feature_count = len(geojson["features"])
    name = geojson["features"][0].get("properties", {}).get("name", "未知")
    print(f"[DataV] 获取成功: {name}（{adcode}），包含 {feature_count} 个要素")

    return geojson


def save_geojson_as_shp(geojson: dict, output_path: Path) -> Path:
    """
    将 GeoJSON 转换为 SHP 文件。

    Args:
        geojson: GeoJSON 字典
        output_path: 输出 SHP 文件路径

    Returns:
        Path: SHP 文件路径
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    gdf = gpd.GeoDataFrame.from_features(geojson["features"])

    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)

    gdf.to_file(output_path, driver="ESRI Shapefile", encoding="utf-8")

    file_size_kb = output_path.stat().st_size / 1024
    print(f"[DataV] SHP 文件已保存: {output_path} ({file_size_kb:.1f} KB)")

    return output_path


def get_admin_boundary(
    adcode: str = None,
    name: str = None,
    output_dir: Path = None,
) -> Path:
    """
    一体化函数：获取行政区划边界并保存为 SHP。

    流程：
    1. 若提供 adcode → 直接获取
    2. 若提供 name → 从本地索引搜索 → 选择 → 获取
    3. 检查本地缓存（{adcode}.json）
    4. 若无缓存 → 从 API 获取 → 保存 GeoJSON → 转 SHP

    Args:
        adcode: 行政区划代码
        name: 行政区划名称（模糊搜索）
        output_dir: 输出目录

    Returns:
        Path: SHP 文件路径
    """
    if not adcode and not name:
        raise ValueError("必须提供 adcode 或 name 之一")

    boundaries_dir = _get_boundaries_dir(output_dir)

    # 若提供 name，先搜索 adcode
    if not adcode and name:
        matches = search_adcode_by_name(name, output_dir)
        selected = select_from_matches(matches)
        adcode = str(selected["adcode"])
        print(f"[DataV] 选择: {selected['name']}（{adcode}）")

    # 检查 SHP 缓存
    shp_path = boundaries_dir / f"{adcode}.shp"
    if shp_path.exists():
        print(f"[DataV] 使用 SHP 缓存: {shp_path}")
        return shp_path

    # 获取 GeoJSON（带缓存）
    geojson = fetch_boundary_by_adcode(adcode, output_dir)

    # 转换为 SHP
    return save_geojson_as_shp(geojson, shp_path)


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(
        description="从阿里云 DataV 获取行政区划边界 SHP 文件",
    )
    parser.add_argument(
        "--adcode",
        type=str,
        help="行政区划代码（如 110000）",
    )
    parser.add_argument(
        "--name",
        type=str,
        help="行政区划名称（模糊搜索，如 北京市）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(Path("data") / "boundaries"),
        help="输出目录",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="强制重建行政区划索引",
    )

    args = parser.parse_args()

    if not args.adcode and not args.name:
        parser.error("必须提供 --adcode 或 --name 之一")

    output_dir = Path(args.output)

    print("=" * 60)
    print("RS-Platform: DataV 行政区划边界获取")
    print("=" * 60)

    # 强制重建索引
    if args.rebuild_index:
        index_path = output_dir / "index.json"
        if index_path.exists():
            index_path.unlink()
            print("[DataV] 已删除旧索引，将重新构建")

    shp_path = get_admin_boundary(
        adcode=args.adcode,
        name=args.name,
        output_dir=output_dir,
    )

    print(f"\n输出文件: {shp_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
