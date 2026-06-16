# RS-Platform 遥感影像处理系统

基于 Sentinel-2 卫星数据的遥感影像下载、处理与展示平台。支持从 Microsoft Planetary Computer 批量获取数据，自动完成去云、波段合成、格式转换等处理流程，并通过 Web 服务提供可视化展示。

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    前端 (React + OpenLayers)                 │
│              地图展示 / 参数配置 / 影像浏览                    │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────┐
│                    后端 (FastAPI)                            │
│           REST API / ZARR 数据服务 / 任务调度                 │
└───────┬──────────────────────────────────────┬──────────────┘
        │                                      │
┌───────▼──────────┐                ┌──────────▼──────────────┐
│ PostgreSQL +     │                │  ZARR 数据存储           │
│ PostGIS          │                │  (分块压缩数组)          │
│ (元数据管理)      │                │                          │
└──────────────────┘                └──────────────────────────┘
                                            ▲
┌───────────────────────────────────────────┴──────────────────┐
│                   数据处理管线                                │
│                                                              │
│  MPC STAC API ──→ ARIA2 下载 ──→ 波段合成 ──→ SCL 去云 ──→ ZARR │
│   (搜索+签名)     (批量+Token刷新)  (RGB TIF)   (云掩膜)   (格式转换)│
└──────────────────────────────────────────────────────────────┘
```

## 功能特性

| 功能 | 说明 |
|------|------|
| **STAC 搜索** | 按区域、日期、云量搜索 Sentinel-2 L2A 影像 |
| **批量下载** | 使用 ARIA2 多连接并行下载，支持断点续传 |
| **Token 自动刷新** | 每 25 分钟自动刷新 MPC 签名 URL，避免下载中断 |
| **波段合成** | B02(蓝) + B03(绿) + B04(红) → RGB 真彩色 TIF |
| **SCL 去云** | 使用场景分类层去除云、云阴影、卷云像素 |
| **ZARR 转换** | TIF → ZARR 分块存储，前端按需加载，提升展示性能 |
| **REST API** | FastAPI 后端，提供影像列表、切片、概览图等接口 |

## 目录结构

```
RS-Platform/
├── config/
│   └── settings.py              # 全局配置
├── scripts/
│   ├── 01_search_and_sign.py    # STAC 搜索 + URL 签名
│   ├── 02_aria2_download.py     # ARIA2 批量下载 + Token 刷新
│   ├── 03_band_merge.py         # 波段合成
│   ├── 04_cloud_mask.py         # SCL 去云处理
│   ├── 05_tif_to_zarr.py        # TIF → ZARR 转换
│   └── 06_pipeline.py           # 整合管线
├── backend/
│   ├── main.py                  # FastAPI 入口
│   ├── routers/
│   │   └── imagery.py           # 影像 API 路由
│   ├── services/
│   │   └── zarr_service.py      # ZARR 数据服务
│   └── models/
│       └── imagery.py           # 数据模型
├── frontend/                    # React + OpenLayers 前端（待开发）
├── data/
│   ├── downloads/               # ARIA2 下载的原始波段
│   ├── merged/                  # 合成后的 RGB TIF
│   ├── cloud_masked/            # 去云后的 TIF
│   └── zarr/                    # ZARR 输出
├── aria2-1.37.0-win-64bit-build1/  # ARIA2 工具
├── requirements.txt
└── README.md
```

## 快速开始

### 1. 环境准备

**Python 依赖：**

```bash
pip install -r requirements.txt
```

**主要依赖说明：**

| 包名 | 用途 |
|------|------|
| `pystac-client` | STAC API 客户端，搜索 Sentinel-2 数据 |
| `planetary-computer` | MPC URL 签名，获取带 SAS Token 的下载链接 |
| `rasterio` | GeoTIFF 读写 |
| `rioxarray` | GeoTIFF ↔ xarray 转换 |
| `xarray` | 多维数组处理，ZARR 读写 |
| `zarr` | ZARR 格式支持 |
| `fastapi` | Web API 框架 |
| `uvicorn` | ASGI 服务器 |

### 2. 配置参数

编辑 `config/settings.py` 修改默认配置：

```python
# 默认搜索区域（北京市）
DEFAULT_BBOX = [116.0, 39.0, 117.0, 40.0]

# 默认日期范围
DEFAULT_DATE_RANGE = "2024-01-01/2024-06-30"

# 默认最大云量
DEFAULT_CLOUD_COVER_MAX = 20

# ARIA2 路径
ARIA2_PATH = PROJECT_ROOT / "aria2-1.37.0-win-64bit-build1" / "aria2c.exe"
```

### 3. 运行数据处理管线

**方式一：完整管线（一键执行）**

```bash
python scripts/06_pipeline.py \
    --bbox 116.0 39.0 117.0 40.0 \
    --date "2024-01-01/2024-06-30" \
    --cloud-cover 20 \
    --output ./data
```

**方式二：分步执行**

```bash
# Step 1: 搜索并生成下载列表
python scripts/01_search_and_sign.py --bbox 116.0 39.0 117.0 40.0 --date "2024-01-01/2024-06-30"

# Step 2: ARIA2 批量下载
python scripts/02_aria2_download.py --data-dir ./data

# Step 3: 波段合成
python scripts/03_band_merge.py --data-dir ./data

# Step 4: SCL 去云
python scripts/04_cloud_mask.py --data-dir ./data

# Step 5: TIF → ZARR
python scripts/05_tif_to_zarr.py --data-dir ./data
```

### 4. 启动后端 API 服务

```bash
# 开发模式（热重载）
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# 生产模式
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 4
```

启动后访问：
- API 文档（Swagger UI）：http://localhost:8000/docs
- API 文档（ReDoc）：http://localhost:8000/redoc

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 服务健康检查 |
| `GET` | `/health` | 详细健康状态 |
| `GET` | `/api/imagery/list` | 列出所有可用影像 |
| `GET` | `/api/imagery/{id}` | 获取影像详情 |
| `GET` | `/api/imagery/{id}/tile?x_min=&x_max=&y_min=&y_max=` | 获取影像切片（PNG） |
| `GET` | `/api/imagery/{id}/overview` | 获取影像概览图 |
| `POST` | `/api/imagery/download` | 触发新影像下载任务 |

**触发下载请求示例：**

```json
POST /api/imagery/download
{
    "bbox": [116.0, 39.0, 117.0, 40.0],
    "date_range": "2024-01-01/2024-06-30",
    "cloud_cover_max": 20
}
```

## 数据来源

- **卫星数据**：Sentinel-2 L2A（Level-2A，大气校正后）
- **数据平台**：[Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/)
- **API 协议**：STAC（SpatioTemporal Asset Catalog）
- **认证方式**：`planetary_computer.sign()` URL 签名（无需 API Key）

## Sentinel-2 波段说明

| 波段 | 名称 | 波长 (nm) | 分辨率 (m) | 用途 |
|------|------|-----------|------------|------|
| B02 | Blue | 490 | 10 | RGB 蓝通道 |
| B03 | Green | 560 | 10 | RGB 绿通道 |
| B04 | Red | 665 | 10 | RGB 红通道 |
| SCL | Scene Classification Layer | - | 20 | 云检测与去云 |

## SCL 去云分类值

| 值 | 类别 | 处理方式 |
|----|------|----------|
| 0 | NO_DATA | 标记为无效 |
| 1 | SATURATED_OR_DEFECTIVE | 标记为无效 |
| 3 | CLOUD_SHADOWS | 去除 |
| 8 | CLOUD_MEDIUM_PROBABILITY | 去除 |
| 9 | CLOUD_HIGH_PROBABILITY | 去除 |
| 10 | THIN_CIRRUS | 去除 |
| 4, 5, 6 | 植被 / 裸土 / 水体 | 保留 |

## 输出文件命名规则

```
{scene_id}_{date}_{cloud_cover}_{类型}.tif

示例：
S2A_MSIL2A_20240115T031111_N0510_R075_T50TLK_20240115T061738_20240115_12.5_B04.tif
S2A_MSIL2A_20240115T031111_N0510_R075_T50TLK_20240115T061738_20240115_12.5_RGB.tif
S2A_MSIL2A_20240115T031111_N0510_R075_T50TLK_20240115T061738_20240115_12.5_RGB_cloudmasked.tif
```

## 技术决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 下载工具 | ARIA2 | 多连接并行、断点续传，适合大文件批量下载 |
| 去云时机 | 波段合成后 | 只需读取一次 SCL 文件 |
| Token 刷新间隔 | 25 分钟 | MPC Token 约 1 小时过期，留充足余量 |
| ZARR chunk 大小 | 1024×1024 | 平衡前端切片加载速度和存储效率 |
| SCL 重采样方法 | 最近邻 | 保持分类值完整性，不产生新值 |

## 后续开发

- [ ] React + OpenLayers 前端界面
- [ ] PostgreSQL + PostGIS 数据库集成
- [ ] GeoServer 影像服务发布
- [ ] 变化检测算法集成
- [ ] 地物分类算法集成
- [ ] 用户认证与权限管理
- [ ] 任务队列（Celery）异步处理

## 许可证

本项目仅供学习和研究使用。Sentinel-2 数据由 ESA 提供，遵循 [Copernicus Open Access Hub](https://scihub.copernicus.eu/) 的数据政策。
