"""
01_search_and_sign.py - STAC 搜索与 URL 签名脚本

功能：
1. 连接 Microsoft Planetary Computer (MPC) STAC API
2. 按用户指定的区域、日期、云量搜索 Sentinel-2 L2A 影像
3. 对每个搜索结果调用 planetary_computer.sign() 获取带签名的下载 URL
4. 提取 B02(蓝)、B03(绿)、B04(红)、SCL 四个波段的下载链接
5. 生成 ARIA2 输入文件（urls.txt）和元数据 JSON 文件

使用方法：
    python scripts/01_search_and_sign.py \
        --bbox 116.0 39.0 117.0 40.0 \
        --date "2024-01-01/2024-06-30" \
        --cloud-cover 20 \
        --output ./data
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 将项目根目录加入 Python 路径，以便导入 config 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import planetary_computer
import pystac_client

from config.settings import (
    BANDS,
    BOUNDARIES_DIR,
    DEFAULT_AOI_PATH,
    DEFAULT_BBOX,
    DEFAULT_CLOUD_COVER_MAX,
    DEFAULT_DATE_RANGE,
    DOWNLOADS_DIR,
    MIN_COVERAGE_RATIO,
    MPC_STAC_API_URL,
    SENTINEL2_COLLECTION,
)
from utils.coverage import (
    enrich_items_with_coverage,
    load_aoi_geometry,
    print_coverage_report,
    select_optimal_scenes,
)
from utils.datav_boundary import get_admin_boundary


def connect_stac_catalog() -> pystac_client.Client:
    """
    连接 MPC STAC API 目录。

    使用 planetary_computer.sign_inplace 修饰器，自动对返回的
    所有 STAC Item 的资产 URL 进行签名（添加 SAS Token）。

    Returns:
        pystac_client.Client: 已连接的 STAC 客户端
    """
    print(f"[STAC] 正在连接 MPC STAC API: {MPC_STAC_API_URL}")
    catalog = pystac_client.Client.open(
        MPC_STAC_API_URL,
        modifier=planetary_computer.sign_inplace,
    )
    print("[STAC] 连接成功")
    return catalog


def search_sentinel2(
    catalog: pystac_client.Client,
    bbox: list,
    date_range: str,
    cloud_cover_max: int,
    aoi_geom=None,
) -> list:
    """
    搜索 Sentinel-2 L2A 影像。

    Args:
        catalog: STAC 客户端
        bbox: 边界框 [min_lon, min_lat, max_lon, max_lat]
        date_range: 日期范围字符串，如 "2024-01-01/2024-06-30"
        cloud_cover_max: 最大云量百分比
        aoi_geom: 研究区几何对象（Shapely Geometry），用于 intersects 搜索

    Returns:
        list: 搜索到的 STAC Item 列表
    """
    from shapely.geometry import mapping

    search_params = {
        "collections": [SENTINEL2_COLLECTION],
        "datetime": date_range,
        "query": {"eo:cloud_cover": {"lt": cloud_cover_max}},
    }

    if aoi_geom is not None:
        print(f"[STAC] 搜索参数: intersects=研究区几何, 日期={date_range}, 最大云量={cloud_cover_max}%")
        search_params["intersects"] = mapping(aoi_geom)
    else:
        print(f"[STAC] 搜索参数: bbox={bbox}, 日期={date_range}, 最大云量={cloud_cover_max}%")
        search_params["bbox"] = bbox

    search = catalog.search(**search_params)

    items = list(search.items())
    print(f"[STAC] 共找到 {len(items)} 景影像")
    return items


def generate_output_filename(item, band_name: str) -> str:
    """
    根据 STAC Item 信息生成标准化的输出文件名。

    文件名格式: {scene_id}_{date}_{cloud_cover}_{band}.tif
    例: S2A_MSIL2A_20240115T031111_N0510_R075_T50TLK_20240115T061738_20240115_12.5_B04.tif

    Args:
        item: STAC Item 对象
        band_name: 波段名称（如 B02, B03, B04, SCL）

    Returns:
        str: 标准化的文件名
    """
    scene_id = item.id
    # 从 datetime 属性获取日期，格式化为 YYYYMMDD
    date_str = item.datetime.strftime("%Y%m%d")
    # 获取云量百分比
    cloud_cover = item.properties.get("eo:cloud_cover", 0)
    # 格式化云量为一位小数
    cloud_str = f"{cloud_cover:.1f}"

    return f"{scene_id}_{date_str}_{cloud_str}_{band_name}.tif"


def extract_signed_urls(items: list, download_dir: Path, aoi_geom=None) -> dict:
    """
    从搜索结果中提取各波段的签名下载 URL。

    对每个 STAC Item：
    1. 调用 planetary_computer.sign() 获取带签名的资产 URL
    2. 提取 B02、B03、B04、SCL 四个波段
    3. 生成 ARIA2 输入文件和元数据

    Args:
        items: STAC Item 列表
        download_dir: 下载目录路径
        aoi_geom: 研究区几何对象（用于计算覆盖率）

    Returns:
        dict: 元数据字典，包含每个 scene 的详细信息
    """
    from utils.coverage import compute_coverage

    # 确保下载目录存在
    download_dir.mkdir(parents=True, exist_ok=True)

    # 存储所有 URL（用于 ARIA2 输入文件）
    all_urls = []
    # 存储元数据（用于后续处理步骤）
    metadata = {"scenes": [], "generated_at": datetime.now().isoformat()}

    for idx, item in enumerate(items):
        print(f"[STAC] 处理第 {idx + 1}/{len(items)} 景: {item.id}")

        # 重新签名（sign_inplace 已在 Client.open 时设置，此处再次确保）
        signed_item = planetary_computer.sign(item)

        # 计算覆盖率
        coverage_ratio = None
        if aoi_geom is not None:
            coverage_ratio = compute_coverage(item.geometry, aoi_geom)

        scene_meta = {
            "scene_id": item.id,
            "datetime": item.datetime.isoformat(),
            "date": item.datetime.strftime("%Y%m%d"),
            "cloud_cover": item.properties.get("eo:cloud_cover", 0),
            "bbox": list(item.bbox) if item.bbox else None,
            "coverage_ratio": coverage_ratio,
            "geometry": item.geometry,
            "bands": {},
        }

        # 提取每个目标波段的签名 URL
        for band_name in BANDS.keys():
            if band_name in signed_item.assets:
                asset = signed_item.assets[band_name]
                filename = generate_output_filename(item, band_name)
                full_path = download_dir / filename

                scene_meta["bands"][band_name] = {
                    "url": asset.href,
                    "filename": filename,
                    "local_path": str(full_path),
                    "resolution_m": BANDS[band_name]["resolution_m"],
                }

                # 添加到 ARIA2 URL 列表
                # ARIA2 格式: URL\n  out=文件名
                all_urls.append(f"{asset.href}\n  out={filename}")
            else:
                print(f"  [警告] {item.id} 缺少波段 {band_name}")

        metadata["scenes"].append(scene_meta)

    return {"urls": all_urls, "metadata": metadata}


def save_aria2_input_file(urls: list, output_path: Path) -> None:
    """
    保存 ARIA2 输入文件。

    ARIA2 输入文件格式：每行一个 URL，可用 out= 指定输出文件名。

    Args:
        urls: URL 列表
        output_path: 输出文件路径
    """
    with open(output_path, "w", encoding="utf-8") as f:
        for url_line in urls:
            f.write(url_line + "\n")
    print(f"[ARIA2] 输入文件已保存: {output_path} ({len(urls)} 个 URL)")


def save_metadata_file(metadata: dict, output_path: Path) -> None:
    """
    保存元数据 JSON 文件。

    元数据包含每个 scene 的详细信息（ID、日期、云量、各波段 URL 和本地路径），
    供后续的下载、合成、去云步骤使用。

    Args:
        metadata: 元典数据字典
        output_path: 输出文件路径
    """
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    print(f"[元数据] 元数据文件已保存: {output_path} ({len(metadata['scenes'])} 景)")


def main():
    """主函数：解析参数、执行搜索、生成输出文件。"""
    parser = argparse.ArgumentParser(
        description="MPC STAC 搜索与 URL 签名工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 使用 bbox
  python scripts/01_search_and_sign.py \\
      --bbox 116.0 39.0 117.0 40.0 \\
      --date "2024-01-01/2024-06-30" \\
      --cloud-cover 20 \\
      --output ./data

  # 使用 shp 文件
  python scripts/01_search_and_sign.py \\
      --aoi ./data/beijing_boundary.shp \\
      --date "2024-01-01/2024-06-30" \\
      --cloud-cover 20 \\
      --output ./data

  # 使用行政区划 adcode
  python scripts/01_search_and_sign.py \\
      --adcode 110000 \\
      --date "2024-01-01/2024-06-30"

  # 使用行政区划名称（模糊搜索）
  python scripts/01_search_and_sign.py \\
      --admin-name "北京市" \\
      --date "2024-01-01/2024-06-30"
        """,
    )
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        default=DEFAULT_BBOX,
        metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
        help=f"搜索区域边界框 (默认: {DEFAULT_BBOX})",
    )
    parser.add_argument(
        "--aoi",
        type=str,
        default=DEFAULT_AOI_PATH,
        help="研究区 SHP 文件路径",
    )
    parser.add_argument(
        "--adcode",
        type=str,
        default=None,
        help="行政区划代码（如 110000），自动从 DataV 获取边界",
    )
    parser.add_argument(
        "--admin-name",
        type=str,
        default=None,
        help="行政区划名称（如 北京市），模糊搜索并自动获取边界",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=DEFAULT_DATE_RANGE,
        help=f"日期范围 (默认: {DEFAULT_DATE_RANGE})",
    )
    parser.add_argument(
        "--cloud-cover",
        type=int,
        default=DEFAULT_CLOUD_COVER_MAX,
        help=f"最大云量百分比 (默认: {DEFAULT_CLOUD_COVER_MAX})",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=MIN_COVERAGE_RATIO,
        help=f"最低覆盖率阈值 (默认: {MIN_COVERAGE_RATIO})",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="输出目录 (默认: ./data)",
    )
    parser.add_argument(
        "--auto-select",
        action="store_true",
        help="自动选择最优时相（跳过交互选择）",
    )

    args = parser.parse_args()
    output_dir = Path(args.output)

    print("=" * 60)
    print("RS-Platform: STAC 搜索与 URL 签名")
    print("=" * 60)

    # 1. 加载研究区几何（优先级：adcode > admin-name > aoi > bbox）
    aoi_geom = None
    if args.adcode:
        shp_path = get_admin_boundary(adcode=args.adcode, output_dir=BOUNDARIES_DIR)
        aoi_geom = load_aoi_geometry(aoi_path=str(shp_path))
    elif args.admin_name:
        shp_path = get_admin_boundary(name=args.admin_name, output_dir=BOUNDARIES_DIR)
        aoi_geom = load_aoi_geometry(aoi_path=str(shp_path))
    elif args.aoi:
        print(f"[研究区] 使用 SHP 文件: {args.aoi}")
        aoi_geom = load_aoi_geometry(aoi_path=args.aoi)
    else:
        print(f"[研究区] 使用 bbox: {args.bbox}")
        aoi_geom = load_aoi_geometry(bbox=args.bbox)

    # 2. 连接 STAC 目录
    catalog = connect_stac_catalog()

    # 3. 搜索影像（使用 aoi_geom 的 bounds 作为搜索 bbox）
    search_bbox = list(aoi_geom.bounds) if aoi_geom else args.bbox
    items = search_sentinel2(catalog, search_bbox, args.date, args.cloud_cover, aoi_geom)

    if not items:
        print("[STAC] 未找到符合条件的影像，请调整搜索参数")
        return

    # 4. 计算覆盖率并排序
    items = enrich_items_with_coverage(items, aoi_geom)

    # 5. 选择最优场景组合
    selected_items, report = select_optimal_scenes(items, aoi_geom, args.min_coverage, auto_select=args.auto_select)
    print_coverage_report(report)

    if not selected_items:
        print("[STAC] 无法选择满足覆盖率要求的场景组合")
        return

    # 6. 提取签名 URL 和元数据（仅处理选中的场景）
    result = extract_signed_urls(selected_items, output_dir / "downloads", aoi_geom)

    # 7. 保存场景选择报告
    result["metadata"]["coverage_report"] = report

    # 8. 保存 ARIA2 输入文件
    aria2_input_path = output_dir / "urls.txt"
    save_aria2_input_file(result["urls"], aria2_input_path)

    # 9. 保存元数据文件
    metadata_path = output_dir / "metadata.json"
    save_metadata_file(result["metadata"], metadata_path)

    # 10. 保存研究区几何（供后续拼接裁剪使用）
    aoi_geometry_path = output_dir / "aoi_geometry.json"
    from shapely.geometry import mapping
    with open(aoi_geometry_path, "w", encoding="utf-8") as f:
        json.dump(mapping(aoi_geom), f)
    print(f"[研究区] 几何已保存: {aoi_geometry_path}")

    print("=" * 60)
    print(f"完成！共选中 {len(selected_items)} 景影像（原始 {len(items)} 景）")
    print(f"  ARIA2 输入文件: {aria2_input_path}")
    print(f"  元数据文件: {metadata_path}")
    print(f"  研究区几何: {aoi_geometry_path}")
    print(f"  下载目录: {output_dir / 'downloads'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
