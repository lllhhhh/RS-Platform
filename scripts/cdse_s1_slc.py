"""
cdse_s1_slc.py - 通过 Copernicus Data Space 搜索和下载 Sentinel-1 SLC 数据

使用 Copernicus Data Space Ecosystem (CDSE) STAC API 直接搜索下载，
无需 eodag 依赖。

功能：
1. OAuth2 认证获取 CDSE Token
2. STAC 搜索 S1 SLC IW 产品
3. 交互式选择升轨/降轨
4. 下载 SAFE 格式产品
5. 从 SAFE 目录提取 VV/VH 极化波段 TIF

依赖：requests（已在 requirements.txt 中）

使用方式：
    from scripts.cdse_s1_slc import search_slc, download_slc, extract_bands
"""

import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import ssl
import urllib3

import requests

# Windows 环境下禁用 SSL 证书吊销检查（解决 CRYPT_E_REVOCATION_OFFLINE 错误）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _create_ssl_context():
    """创建禁用证书吊销检查的 SSL 上下文。"""
    ctx = ssl.create_default_context()
    # 禁用证书吊销检查（Windows schannel 的 CRYPT_E_REVOCATION_OFFLINE 问题）
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# 创建共享 Session
_session = requests.Session()

# 设置 User-Agent 避免被 WAF 拦截
_session.headers.update({
    "User-Agent": "RS-Platform/1.0 (Sentinel-1 SLC Downloader)",
})

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import PROJECT_ROOT

# ============================================================
# CDSE API 端点
# ============================================================
CDSE_AUTH_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CDSE_STAC_URL = "https://catalogue.dataspace.copernicus.eu/stac/search"
CDSE_COLLECTION = "sentinel-1-slc"

# CDSE 配置路径
CDSE_CONFIG_PATH = Path.home() / ".rs_platform" / "cdse_config.json"

_note_lines = []


def _load_credentials() -> dict:
    """加载 CDSE 认证信息。优先从配置文件读取，否则使用默认值。"""
    if CDSE_CONFIG_PATH.exists():
        try:
            with open(CDSE_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_credentials(data: dict):
    """保存 CDSE 认证信息到配置文件。"""
    CDSE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CDSE_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _get_cdse_token(credentials: dict = None) -> str:
    """
    获取 CDSE OAuth2 Access Token。

    Args:
        credentials: 可选的认证信息字典，支持：
            - username / password
            - access_token / refresh_token（已有 token）
        为 None 时从配置文件读取。

    Returns:
        str: Access Token
    """
    if credentials is None:
        credentials = _load_credentials()

    # 如果已有 token 且未过期，直接使用
    access_token = credentials.get("access_token", "")
    expires_at = credentials.get("expires_at", 0)
    if access_token and time.time() < expires_at - 60:
        return access_token

    # 尝试用 refresh_token 刷新
    refresh_token = credentials.get("refresh_token", "")
    if refresh_token:
        r = _session.post(
            CDSE_AUTH_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": "cdse-public",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            credentials["access_token"] = data["access_token"]
            credentials["refresh_token"] = data.get("refresh_token", refresh_token)
            credentials["expires_at"] = time.time() + data.get("expires_in", 3000) - 60
            _save_credentials(credentials)
            return credentials["access_token"]

    # 用户名密码认证
    username = credentials.get("username") or os.getenv("CDSE_USERNAME")
    password = credentials.get("password") or os.getenv("CDSE_PASSWORD")

    if not username or not password:
        raise RuntimeError(
            "CDSE 认证信息未配置。请设置环境变量 CDSE_USERNAME 和 CDSE_PASSWORD，"
            "或在 ~/.rs_platform/cdse_config.json 中配置。"
        )

    r = _session.post(
        CDSE_AUTH_URL,
        data={
            "grant_type": "password",
            "client_id": "cdse-public",
            "username": username,
            "password": password,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"CDSE 认证失败: {r.status_code} - {r.text[:200]}")

    data = r.json()
    credentials["access_token"] = data["access_token"]
    credentials["refresh_token"] = data.get("refresh_token", "")
    credentials["expires_at"] = time.time() + data.get("expires_in", 3000) - 60
    _save_credentials(credentials)
    print("[CDSE] 认证成功，Token 已缓存")
    return credentials["access_token"]


def search_slc(
    bbox: list = None,
    date_range: str = None,
    aoi_geom=None,
) -> list:
    """
    搜索 Sentinel-1 SLC 产品（VV+VH 双极化，IW 模式）。

    Args:
        bbox: 搜索区域 [min_lon, min_lat, max_lon, max_lat]
        date_range: 日期范围 "YYYY-MM-DD/YYYY-MM-DD"
        aoi_geom: 研究区几何（Shapely Geometry，可选）

    Returns:
        list: 产品信息字典列表：
            - scene_id: 场景 ID
            - date: 采集日期 YYYYMMDD
            - date_display: 显示日期
            - orbit_direction: "ASCENDING" 或 "DESCENDING"
            - polarization: 极化列表
            - platform: 平台（S1A/S1B）
            - bbox: 边界框
            - geometry: GeoJSON 几何
            - assets: STAC assets
    """
    import os

    from shapely.geometry import mapping as geom_mapping

    token = _get_cdse_token()
    headers = {"Authorization": f"Bearer {token}"}

    # 构建搜索参数
    params = {
        "collections": CDSE_COLLECTION,
        "limit": 50,
    }

    # 日期范围
    if date_range:
        parts = date_range.split("/")
        if len(parts) == 2:
            params["datetime"] = f"{parts[0]}T00:00:00Z/{parts[1]}T23:59:59Z"

    # 几何搜索
    if aoi_geom is not None:
        geom_bounds = aoi_geom.bounds
        params["bbox"] = f"{geom_bounds[0]},{geom_bounds[1]},{geom_bounds[2]},{geom_bounds[3]}"
    elif bbox:
        params["bbox"] = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"

    print(f"[CDSE] 搜索 S1 SLC IW （VV+VH 双极化）...")

    all_features = []
    # 分页搜索
    while True:
        r = _session.get(CDSE_STAC_URL, headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            print(f"[CDSE] 搜索错误: HTTP {r.status_code}")
            break

        data = r.json()
        features = data.get("features", [])
        all_features.extend(features)

        # 检查是否有下一页
        next_link = None
        for link in data.get("links", []):
            if link.get("rel") == "next":
                if isinstance(link, dict):
                    next_link = link.get("href")
                break

        if not next_link or len(features) < params.get("limit", 50):
            break

        # 提取下一页的查询参数
        params = dict(requests.utils.urlparse(next_link).query)
        params = {k: v for k, v in params.items() if k not in ("limit",)}
        params["limit"] = 50

    if not all_features:
        print("[CDSE] 未找到符合条件的 SLC 产品")
        return []

    # 筛选 IW 模式 + VV+VH 双极化
    filtered = []
    for f in all_features:
        props = f.get("properties", {})
        mode = props.get("sar:instrument_mode", "")
        pols = props.get("sar:polarizations", [])
        orbit = props.get("sat:orbit_state", props.get("orbit_state", "UNKNOWN"))

        # IW 模式检查
        if mode and mode.upper() != "IW":
            continue
        # 双极化检查（VV+VH）
        if "VV" not in pols or "VH" not in pols:
            continue

        date_str = props.get("datetime", "")
        date_display = date_str[:10] if date_str else ""
        date_compact = date_display.replace("-", "") if date_display else ""

        filtered.append({
            "feature": f,
            "scene_id": f["id"],
            "date": date_compact,
            "date_display": date_display,
            "orbit_direction": orbit.upper() if orbit else "UNKNOWN",
            "polarization": pols,
            "platform": props.get("platform", "?"),
            "bbox": f.get("bbox", []),
            "geometry": f.get("geometry"),
            "assets": f.get("assets", {}),
        })

    print(f"[CDSE] 找到 {len(filtered)} 景 IW 双极化 SLC（共搜索到 {len(all_features)} 景）")

    # 获取详细属性（需要请求每个 feature 的详情）
    for item in filtered:
        feature = item["feature"]
        # 尝试从 feature 自身提取更多信息
        if "productType" not in feature.get("properties", {}):
            try:
                detail_url = None
                for link in feature.get("links", []):
                    if link.get("rel") == "self":
                        detail_url = link.get("href")
                        break
                if detail_url:
                    dr = _session.get(detail_url, headers=headers, timeout=30, allow_redirects=True)
                    if dr.status_code == 200:
                        dprops = dr.json().get("properties", {})
                        feature["properties"] = {**dprops, **feature.get("properties", {})}
            except Exception:
                pass  # 详情获取失败不影响主流程

    return filtered


def _get_product_footprint(product: dict):
    """
    从产品信息中提取足迹几何对象。

    Args:
        product: search_slc 返回的产品字典

    Returns:
        Shapely Geometry 或 None
    """
    from shapely.geometry import shape

    geom = product.get("geometry")
    if geom:
        try:
            return shape(geom)
        except Exception:
            pass

    bbox = product.get("bbox")
    if bbox and len(bbox) == 4:
        from shapely.geometry import box
        return box(bbox[0], bbox[1], bbox[2], bbox[3])

    return None


def filter_slc_by_overlap(products: list, aoi_geom, min_overlap_ratio: float = 0.3) -> list:
    """
    过滤 SLC 产品，只保留与 AOI 有充分重叠的产品。

    SLC IW 产品足迹很长（沿轨道数百公里），搜索时只检查是否与 bbox 相交，
    但实际可能只在边缘相交。此函数过滤掉与 AOI 重叠不足的产品。

    Args:
        products: search_slc 返回的产品列表
        aoi_geom: 研究区几何对象（Shapely Geometry）
        min_overlap_ratio: 最小重叠比例（相对于 AOI 面积）

    Returns:
        list: 过滤后的产品列表
    """
    if not aoi_geom:
        return products

    aoi_area = aoi_geom.area
    if aoi_area == 0:
        return products

    filtered = []
    for p in products:
        footprint = _get_product_footprint(p)
        if footprint is None:
            continue

        try:
            intersection = footprint.intersection(aoi_geom)
            overlap_ratio = intersection.area / aoi_area
            p["aoi_overlap_ratio"] = overlap_ratio

            if overlap_ratio >= min_overlap_ratio:
                filtered.append(p)
        except Exception:
            # 几何计算失败，保留产品
            filtered.append(p)

    # 按重叠比例排序（高的在前）
    filtered.sort(key=lambda x: x.get("aoi_overlap_ratio", 0), reverse=True)

    removed = len(products) - len(filtered)
    if removed > 0:
        print(f"[CDSE] 过滤掉 {removed} 个与 AOI 重叠不足的产品（保留 {len(filtered)} 个）")

    return filtered


def group_slc_by_overlap(products: list, min_mutual_overlap: float = 0.5) -> list:
    """
    将 SLC 产品按相互重叠程度分组。

    只有相互之间有充分重叠的产品才能用于 InSAR。

    Args:
        products: 过滤后的 SLC 产品列表
        min_mutual_overlap: 产品间最小重叠比例（相对于较小产品的面积）

    Returns:
        list[list]: 分组后的产品列表，每组内的产品相互重叠
    """
    if len(products) <= 1:
        return [products]

    footprints = []
    for p in products:
        fp = _get_product_footprint(p)
        footprints.append(fp)

    # 使用并查集分组
    parent = list(range(len(products)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i in range(len(products)):
        if footprints[i] is None:
            continue
        for j in range(i + 1, len(products)):
            if footprints[j] is None:
                continue
            try:
                intersection = footprints[i].intersection(footprints[j])
                min_area = min(footprints[i].area, footprints[j].area)
                if min_area > 0 and intersection.area / min_area >= min_mutual_overlap:
                    union(i, j)
            except Exception:
                pass

    # 收集分组
    groups = defaultdict(list)
    for i in range(len(products)):
        groups[find(i)].append(products[i])

    result = list(groups.values())
    # 按组大小排序（大的在前）
    result.sort(key=len, reverse=True)

    return result


def select_orbit_direction_interactive(products: list) -> list:
    """
    交互式选择升降轨和具体场景。

    先按轨道方向分组展示，用户选择轨道方向后下载该方向所有场景。

    Args:
        products: search_slc 返回的产品列表

    Returns:
        list: 用户选中的产品列表
    """
    if not products:
        return []

    # 按轨道方向分组
    grouped = defaultdict(list)
    for p in products:
        grouped[p["orbit_direction"]].append(p)

    print("\n" + "=" * 80)
    print("可用 SLC 产品（按轨道方向 + 日期排序）")
    print("=" * 80)

    orbit_dirs = sorted(grouped.keys())
    for i, orbit_dir in enumerate(orbit_dirs):
        items = grouped[orbit_dir]
        items.sort(key=lambda x: x["date"])
        platforms = set(p["platform"] for p in items)
        print(f"\n  [{i + 1}] {orbit_dir} — {len(items)} 景（{', '.join(sorted(platforms))}）")
        print(f"  {'序号':>4}  {'日期':<12}  {'场景 ID'}")
        print(f"  {'----':>4}  {'----------':<12}  {'--------'}")
        for j, item in enumerate(items):
            print(f"  {j + 1:>4}  {item['date_display']:<12}  {item['scene_id']}")

    print("\n" + "-" * 80)
    print("提示: 输入轨道方向序号（如 1=ASCENDING, 2=DESCENDING）下载该方向所有场景")
    print("      或输入具体场景序号（如 1,3,5 按全局序号），或输入 all 下载全部")

    flat_items = []
    for orbit_dir in orbit_dirs:
        grouped[orbit_dir].sort(key=lambda x: x["date"])
        flat_items.extend(grouped[orbit_dir])

    while True:
        try:
            user_input = input("\n请选择: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return []

        if not user_input:
            continue

        if user_input == "all":
            print(f"\n已选择全部 {len(flat_items)} 景")
            return flat_items

        try:
            indices = [int(x.strip()) for x in user_input.split(",")]
        except ValueError:
            print("请输入数字序号（多个用逗号分隔）或 all/轨道方向序号")
            continue

        # 判断是轨道方向序号还是场景序号
        if len(indices) == 1 and 1 <= indices[0] <= len(orbit_dirs):
            selected_orbit = orbit_dirs[indices[0] - 1]
            selected = grouped[selected_orbit]
            print(f"\n已选择 {selected_orbit}，共 {len(selected)} 景")
            return selected

        # 场景序号
        if any(i < 1 or i > len(flat_items) for i in indices):
            print(f"序号超出范围，请输入 1~{len(flat_items)} 或 1~{len(orbit_dirs)}（轨道方向）")
            continue

        selected = [flat_items[i - 1] for i in indices]
        print(f"\n已选择 {len(selected)} 景")
        return selected


def download_slc(products: list, output_dir: Path) -> list:
    """
    下载 SLC SAFE 产品。

    从 CDSE 获取下载链接（带签名），使用 requests 流式下载。

    Args:
        products: 选中的产品列表
        output_dir: 下载目录

    Returns:
        list: 下载后的产品信息列表（含 path: SAFE 目录）
    """
    import os

    token = _get_cdse_token()
    headers = {"Authorization": f"Bearer {token}"}

    output_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    for i, item in enumerate(products):
        scene_id = item["scene_id"]
        safe_name = f"{scene_id}.SAFE"

        print(f"\n[CDSE] 下载 {i + 1}/{len(products)}: {scene_id}")

        # 检查是否已存在
        existing = list(output_dir.glob(f"*{scene_id[:25]}*"))
        if existing:
            safe_path = existing[0]
            print(f"  [跳过] 已存在: {safe_path.name}")
            item["path"] = safe_path if safe_path.is_dir() else safe_path
            downloaded.append(item)
            continue

        # 获取下载 URL
        # CDSE 的下载通过 feature self link 或专门的下载端点
        feature = item.get("feature", {})
        download_url = None

        # 方式1: 通过 feature 的 assets 查找下载链接
        assets = feature.get("assets", {})
        for key, asset in assets.items():
            if "download" in key.lower() or "product" in key.lower() or "SAFE" in key.upper():
                href = asset.get("href", "")
                if href:
                    download_url = href
                    break

        # 方式2: 通过 feature 的 self link 获取详情
        if not download_url:
            links = feature.get("links", [])
            for link in links:
                if link.get("rel") == "self":
                    detail_url = link.get("href", "")
                    if detail_url:
                        dr = _session.get(detail_url, headers=headers, timeout=30, allow_redirects=True)
                        if dr.status_code == 200:
                            ddata = dr.json()
                            assets = ddata.get("assets", {})
                            for key, asset in assets.items():
                                if "download" in key.lower() or "SAFE" in key.upper():
                                    href = asset.get("href", "")
                                    if href:
                                        download_url = href
                                        break
                    break

        # 方式3: 构造下载 URL
        if not download_url:
            # CDSE 下载格式：https://zipper.dataspace.copernicus.eu/...
            download_url = f"https://catalogue.dataspace.copernicus.eu/stac/collections/{CDSE_COLLECTION}/items/{scene_id}"

        print(f"  开始下载: {safe_name}")
        print(f"  URL: {download_url[:80]}...")

        try:
            # 流式下载
            dr = _session.get(download_url, headers=headers, stream=True, timeout=300, allow_redirects=True)
            if dr.status_code == 200:
                # 下载 zip 文件
                zip_path = output_dir / f"{safe_name}.zip"
                total_size = int(dr.headers.get("content-length", 0))
                downloaded_size = 0

                with open(zip_path, "wb") as f:
                    for chunk in dr.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            progress = downloaded_size / total_size * 100
                            print(f"\r  进度: {progress:.0f}% ({downloaded_size // (1024*1024)} MB / {total_size // (1024*1024)} MB)", end="")
                print()

                # 解压
                import zipfile
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(output_dir)
                zip_path.unlink()  # 删除 zip

                # 查找解压后的 SAFE 目录
                safe_dirs = sorted(output_dir.glob("*.SAFE"))
                if safe_dirs:
                    safe_path = safe_dirs[0]
                    item["path"] = safe_path
                    print(f"  [完成] {safe_path.name}")
                else:
                    print(f"  [警告] 解压后未找到 SAFE 目录")
                    continue

                downloaded.append(item)
            else:
                print(f"  [错误] 下载失败: HTTP {dr.status_code}")
        except Exception as e:
            print(f"  [错误] 下载失败: {e}")

    print(f"\n[CDSE] 下载完成: {len(downloaded)}/{len(products)}")
    return downloaded


def extract_bands(safe_path: Path) -> dict:
    """
    从 S1 SAFE 目录提取 VV/VH 极化波段 TIF 路径。

    SAFE 目录结构：
    S1A_IW_SLC__*.SAFE/
    ├── measurement/
    │   └── s1a-iw*-slc-vv-*.tiff
    │   └── s1a-iw*-slc-vh-*.tiff
    ├── annotation/
    └── manifest.safe

    Args:
        safe_path: SAFE 目录路径

    Returns:
        dict: {"vv": Path, "vh": Path} 或空字典
    """
    measurement_dir = safe_path / "measurement"
    if not measurement_dir.exists():
        # 某些 SAFE 结构中 measurement 可能在子目录
        measurement_dir = safe_path
        if not any(safe_path.rglob("*.tiff")):
            print(f"  [警告] 未找到 measurement 文件: {safe_path}")
            return {}

    bands = {}

    # 搜索 VV 和 VH TIF 文件
    for tif_file in sorted(measurement_dir.rglob("*")):
        if not tif_file.is_file():
            continue
        fname = tif_file.name.lower()
        if (fname.endswith(".tiff") or fname.endswith(".tif")):
            if "-vv-" in fname or "_vv_" in fname or "-slc-vv-" in fname:
                bands["vv"] = tif_file
            elif "-vh-" in fname or "_vh_" in fname or "-slc-vh-" in fname:
                bands["vh"] = tif_file

    if "vv" in bands and "vh" in bands:
        file_sizes = f"VV={bands['vv'].stat().st_size // (1024*1024)}MB, VH={bands['vh'].stat().st_size // (1024*1024)}MB"
        print(f"  提取波段: {file_sizes}")
    else:
        found_names = ", ".join(list(bands.keys()))
        print(f"  [警告] 波段提取不完整: {found_names}（需要 VV+VH）")

    return bands


def main():
    """独立运行：搜索并下载 S1 SLC 产品。"""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="CDSE S1 SLC 搜索与下载工具")
    parser.add_argument("--bbox", type=float, nargs=4, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"))
    parser.add_argument("--date", type=str, default="2024-01-01/2024-01-05", help="日期范围 YYYY-MM-DD/YYYY-MM-DD")
    parser.add_argument("--output", type=str)
    args = parser.parse_args()

    if args.output is None:
        args.output = str(Path(__file__).resolve().parent.parent / "data" / "downloads" / "s1_slc")

    products = search_slc(bbox=args.bbox, date_range=args.date)
    if not products:
        print("未找到符合条件的 SLC 产品。")
        return

    selected = select_orbit_direction_interactive(products)
    if not selected:
        return

    downloaded = download_slc(selected, Path(args.output))
    if downloaded:
        for item in downloaded:
            if "path" in item:
                extract_bands(item["path"])


if __name__ == "__main__":
    main()
