"""
RS-Platform 全局配置文件

集中管理所有可配置参数，包括：
- MPC STAC API 地址与认证配置
- ARIA2 下载工具路径
- 数据目录结构
- 默认搜索参数（区域、日期、云量）
- Sentinel-2 波段定义
"""

import os
from pathlib import Path

# ============================================================
# 项目根目录
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ============================================================
# MPC STAC API 配置
# ============================================================
# Microsoft Planetary Computer STAC API 端点
MPC_STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

# ============================================================
# 阿里云 DataV 行政区划 API 配置
# ============================================================
# DataV 行政区划 GeoJSON API 端点
DATAV_API_BASE = "https://geo.datav.aliyun.com/areas_v3/bound"

# Sentinel-2 L2A（Level-2A，大气校正后）集合名称
SENTINEL2_COLLECTION = "sentinel-2-l2a"

# Sentinel-1 集合名称
SENTINEL1_GRD_COLLECTION = "sentinel-1-grd"
SENTINEL1_SLC_COLLECTION = "sentinel-1-slc"

# ============================================================
# Sentinel-2 波段定义
# ============================================================
# 用户只需要 RGB 三波段 + SCL 去云波段
# B02 = 蓝色波段 (490nm, 10m)
# B03 = 绿色波段 (560nm, 10m)
# B04 = 红色波段 (665nm, 10m)
# SCL = 场景分类层 (20m，用于云检测)
BANDS = {
    "B02": {"name": "Blue",  "wavelength_nm": 490, "resolution_m": 10},
    "B03": {"name": "Green", "wavelength_nm": 560, "resolution_m": 10},
    "B04": {"name": "Red",   "wavelength_nm": 665, "resolution_m": 10},
    "SCL": {"name": "Scene Classification Layer", "resolution_m": 20},
}

# 用于波段合成的 RGB 波段顺序（写入 TIF 的通道顺序）
RGB_BAND_ORDER = ["B04", "B03", "B02"]  # Red, Green, Blue

# ============================================================
# Sentinel-1 GRD 波段定义
# ============================================================
# SAR 极化通道
S1_BANDS = {
    "vv": {"name": "Vertical-Vertical", "polarization": "VV"},
    "vh": {"name": "Vertical-Horizontal", "polarization": "VH"},
}

# S1 波段合成顺序（写入 TIF 的通道顺序）
S1_BAND_ORDER = ["vv", "vh"]

# ============================================================
# SCL 去云配置
# ============================================================
# SCL 值定义（Sentinel-2 L2A 场景分类层）
SCL_CLASSES = {
    0:  "NO_DATA",                    # 无数据
    1:  "SATURATED_OR_DEFECTIVE",     # 饱和或缺陷像素
    2:  "DARK_AREA_PIXELS",           # 暗区像素
    3:  "CLOUD_SHADOWS",              # 云阴影
    4:  "VEGETATION",                 # 植被
    5:  "NOT_VEGETATED",              # 非植被（裸土等）
    6:  "WATER",                      # 水体
    7:  "UNCLASSIFIED",               # 未分类
    8:  "CLOUD_MEDIUM_PROBABILITY",   # 云（中等概率）
    9:  "CLOUD_HIGH_PROBABILITY",     # 云（高概率）
    10: "THIN_CIRRUS",                # 薄卷云
    11: "SNOW",                       # 雪/冰
}

# 需要去除的 SCL 类别值（云、云阴影、卷云）
CLOUD_SCL_VALUES = [3, 8, 9, 10]

# 无效数据的 SCL 类别值
INVALID_SCL_VALUES = [0, 1]

# ============================================================
# 数据目录配置
# ============================================================
DATA_DIR = PROJECT_ROOT / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"      # ARIA2 下载的原始波段
MERGED_DIR = DATA_DIR / "merged"            # 波段合成后的 RGB TIF
CLOUD_MASKED_DIR = DATA_DIR / "cloud_masked" # 去云后的 TIF
ZARR_DIR = DATA_DIR / "zarr"                # ZARR 输出目录
BOUNDARIES_DIR = DATA_DIR / "boundaries"    # 行政区划 SHP 缓存目录

# ============================================================
# ARIA2 配置
# ============================================================
# ARIA2 可执行文件路径（Windows 环境）
ARIA2_PATH = PROJECT_ROOT / "aria2-1.37.0-win-64bit-build1" / "aria2c.exe"

# ARIA2 下载参数
ARIA2_PARAMS = {
    "max_concurrent_downloads": 4,        # 最大并发下载数
    "max_connection_per_server": 5,       # 每个服务器的最大连接数
    "timeout": 300,                       # 超时时间（秒）
    "retry_wait": 10,                     # 重试等待时间（秒）
    "continue": True,                     # 断点续传
}

# Token 自动刷新间隔（秒）
# MPC Token 约 1 小时过期，25 分钟刷新一次留有充足余量
TOKEN_REFRESH_INTERVAL_SEC = 25 * 60  # 1500 秒

# ============================================================
# 默认搜索参数
# ============================================================
# 默认边界框 [min_lon, min_lat, max_lon, max_lat]
# 示例：北京市区域
DEFAULT_BBOX = [116.0, 39.0, 117.0, 40.0]

# 默认研究区 SHP 文件路径（None 表示使用 bbox）
DEFAULT_AOI_PATH = None

# 默认日期范围
DEFAULT_DATE_RANGE = "2024-01-01/2024-01-05"

# 默认最大云量百分比
DEFAULT_CLOUD_COVER_MAX = 20

# 最低覆盖率阈值（0.95 = 95%）
# 场景组合对研究区的覆盖率低于此值时发出警告
MIN_COVERAGE_RATIO = 0.95

# ============================================================
# ZARR 输出配置
# ============================================================
# ZARR chunk 大小（像素）
ZARR_CHUNK_SIZE = {"x": 1024, "y": 1024}

# ============================================================
# 数据库配置（PostgreSQL + PostGIS）
# ============================================================
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "rs_platform")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
