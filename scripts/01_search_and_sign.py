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
    DEFAULT_BBOX,
    DEFAULT_CLOUD_COVER_MAX,
    DEFAULT_DATE_RANGE,
    DOWNLOADS_DIR,
    MPC_STAC_API_URL,
    SENTINEL2_COLLECTION,
)


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
) -> list:
    """
    搜索 Sentinel-2 L2A 影像。

    Args:
        catalog: STAC 客户端
        bbox: 边界框 [min_lon, min_lat, max_lon, max_lat]
        date_range: 日期范围字符串，如 "2024-01-01/2024-06-30"
        cloud_cover_max: 最大云量百分比

    Returns:
        list: 搜索到的 STAC Item 列表
    """
    print(f"[STAC] 搜索参数: bbox={bbox}, 日期={date_range}, 最大云量={cloud_cover_max}%")

    search = catalog.search(
        collections=[SENTINEL2_COLLECTION],
        bbox=bbox,
        datetime=date_range,
        query={"eo:cloud_cover": {"lt": cloud_cover_max}},
    )

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


def extract_signed_urls(items: list, download_dir: Path) -> dict:
    """
    从搜索结果中提取各波段的签名下载 URL。

    对每个 STAC Item：
    1. 调用 planetary_computer.sign() 获取带签名的资产 URL
    2. 提取 B02、B03、B04、SCL 四个波段
    3. 生成 ARIA2 输入文件和元数据

    Args:
        items: STAC Item 列表
        download_dir: 下载目录路径

    Returns:
        dict: 元数据字典，包含每个 scene 的详细信息
    """
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

        scene_meta = {
            "scene_id": item.id,
            "datetime": item.datetime.isoformat(),
            "date": item.datetime.strftime("%Y%m%d"),
            "cloud_cover": item.properties.get("eo:cloud_cover", 0),
            "bbox": list(item.bbox) if item.bbox else None,
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
  python scripts/01_search_and_sign.py \\
      --bbox 116.0 39.0 117.0 40.0 \\
      --date "2024-01-01/2024-06-30" \\
      --cloud-cover 20 \\
      --output ./data
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
        "--output",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="输出目录 (默认: ./data)",
    )

    args = parser.parse_args()
    output_dir = Path(args.output)

    print("=" * 60)
    print("RS-Platform: STAC 搜索与 URL 签名")
    print("=" * 60)

    # 1. 连接 STAC 目录
    catalog = connect_stac_catalog()

    # 2. 搜索影像
    items = search_sentinel2(catalog, args.bbox, args.date, args.cloud_cover)

    if not items:
        print("[STAC] 未找到符合条件的影像，请调整搜索参数")
        return

    # 3. 提取签名 URL 和元数据
    result = extract_signed_urls(items, output_dir / "downloads")

    # 4. 保存 ARIA2 输入文件
    aria2_input_path = output_dir / "urls.txt"
    save_aria2_input_file(result["urls"], aria2_input_path)

    # 5. 保存元数据文件
    metadata_path = output_dir / "metadata.json"
    save_metadata_file(result["metadata"], metadata_path)

    print("=" * 60)
    print(f"完成！共处理 {len(items)} 景影像")
    print(f"  ARIA2 输入文件: {aria2_input_path}")
    print(f"  元数据文件: {metadata_path}")
    print(f"  下载目录: {output_dir / 'downloads'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
