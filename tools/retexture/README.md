# tools/retexture —— 把照片里的真实细节投影回模型贴图

## 这个工具解决什么问题

单图生 3D（混元、Tripo、Meshy 都一样）的贴图是**模型推理出来的**：石头、布料的质感能猜个八九不离十，但**牌子上的字一定是糊的、错的**——它没见过那几个字，只能瞎画。

这套工具的思路是：**别让模型猜，把照片里的真实像素按几何投影刷回去**。

```
模型 mesh ──正交渲染──> 每个可见像素命中了哪个 UV ──散射──> 把照片对应位置的像素写进贴图
```

因为 AI 生成的 mesh 会把一个面拆成几十个零碎的 UV 岛（实测正面 7,046 个三角面被拆成 58 个岛），**「裁一块矩形贴到 UV 上」是不可行的**；而「渲染一遍、按可见像素散射」天然绕开了岛碎片化的麻烦。

## 依赖

- Python 3.10+、`numpy`、`Pillow`
- 没有 DSH 的话用系统 python 即可；有 DSH 时可以直接用捆绑运行时：

```powershell
$py = "$env:DSH_HOME\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
```

## 目录里的文件

| 文件 | 作用 |
| --- | --- |
| `probe_glb.py` | GLB 解析公共库（读 chunk / accessor / 世界矩阵），被其它脚本 import |
| `retexture.py` | **核心**。`views` 预览、`profile` 量体、`retexture` 重投影出新的 GLB |
| `edge_profile.py` | 数值找照片里的竖直棱线（别用眼睛量，量不准） |
| `surface_check.py` | **关键验收**：不涉及任何相机基的左/右翻转检查 |
| `preview3.py` | 出 front / quarter / left / back / hero 预览图 |
| `check_glb.py` | GLB 结构校验（chunk、bufferView 对齐与越界、accessor） |
| `facecheck.py` | 四个竖直面的接触表 + 各面平均亮度 |
| `make_compare.py` | 前后对比图（自动裁到模型、等高并排） |

## 用法

### 0. 侦察模型

```bash
python probe_glb.py model.glb out/
```

会打印贴图尺寸、UV bbox、各朝向的三角面数，并写出 `atlas_front_overlay.png`（在图集上勾出正面 UV 岛）。**先看清 UV 有多碎，再决定要不要用这套工具。**

### 1. 量照片里的正面四边形

```bash
python edge_profile.py photo.jpg
```

它会逐带打印「最强的竖直边缘 x 坐标」。拿到左右棱线后，柱帽下沿与柱身下沿的 y 自己目视确认（或再扫一遍水平边缘）。得到 `LT RT RB LB` 四个点。

> 正面四边形建议**按照片自身长宽比**取：本工具会用照片的长宽比反推渲染图的柱身矩形高度，两边比例越接近，投影越不会拉伸。

### 2. 重投影

```bash
# 只重投影正面（最常见的用法）
python retexture.py retexture model.glb photo.jpg out.glb out/ front \
    --photo-quad "455,282 746,282 761,1225 468,1225"

# 顺带把侧面/背面的原厂糊字洗成素石（需要一张侧面石带的 quad）
python retexture.py retexture model.glb photo.jpg out.glb out/ all \
    --photo-quad "..." --side-quad "350,282 455,282 468,1225 363,1225"
```

输出：新的 `out.glb`（内在贴图已替换）+ `atlas_retextured.png` + 若干自检渲染图。

### 3. 验收（**别跳过这一步**）

```bash
# 无相机基的翻转检查：direct 必须赢过 flipped
python surface_check.py --glb out.glb --photo photo.jpg \
    --atlas atlas_source.png:source --atlas out/atlas_retextured.png:new --out-dir out

# 视觉预览
python preview3.py --glb out.glb --atlas out/atlas_retextured.png --out-dir out --hero-out preview.png

# 结构校验
python check_glb.py out.glb
```

> `surface_check.py` 会同时测**原始烘焙图集**作为对照：原厂贴图是从同一张照片烤出来的，所以它也必须是 direct 胜出。两边都 direct，才说明约定没错。

## 两个必须记住的坑

1. **自检不能用相机基。** 「采样」和「自检渲染」如果共用同一个 `basis_from_viewdir`，那么基向量左右写反时两边会一起反——自检永远看起来很对，只有真机 three.js 里才暴露。所以验收必须有一步**完全不涉及相机**（`surface_check.py` 就是干这个的：用 mesh 自己的 `TEXCOORD_0` 在表面 (x,y) 坐标里取色）。
2. **自检渲染要用顶点法线。** 用三角面法线着色时，AI mesh 里大量狭长三角面的面法线乱飞，会在预览图上画出一层假的「暗色脉络」，看起来像贴图坏了，其实贴图没事（用纯色贴图渲一遍就能证伪：暗像素占比不变）。`retexture.py` 的 `render_view(..., nrm_tris=...)` 传顶点法线即可。

## 局限

- 只能重投影**照片里看得见的面**。背面/侧面没有参考图时，用 `--side-quad` 指向照片里的素石区域刷成素色，或者自己再拍一张。
- 照片本身的射灯辉光会被一起烘进 albedo（模型会显得自带打光）。想更干净可以先去一遍低频亮度。
- 文字清晰度上限 = 照片分辨率。正面在照片里只有 290×943 px 时，铺到 4096 图集就是放大 3 倍，远看没问题、贴近看会软。要更锐只能拍更大更正的正面照，或者干脆用字体排版代替照片。
