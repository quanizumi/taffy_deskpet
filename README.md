# 永雏塔菲 — Windows 桌面宠物

什么？！塔叠又出新皮肤了，双击 `dist\DeskPet.exe` 即可运行。

![haracte](D:\desk_pet\assets\character.png)

## 使用方式

| 操作 | 效果 |
|------|------|
| 左键拖动 | 移动位置 |
| 左键单击 | 轮流：跳跃 → 压扁回弹 → 左右抖动，并弹出中文对话气泡 |
| 滚轮 | 缩放大小 |
| 右键菜单 | 调整大小 / 置顶开关 / 退出 |

## 环境依赖

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10 / 11 |
| Python | 3.9 及以上 |

Python 包（见 `requirements.txt`）：

| 包名 | 版本 | 用途 |
|------|------|------|
| PySide6 | >= 6.6.0 | Qt GUI（桌面宠物窗口） |
| Pillow | >= 10.0.0 | 图像处理 |
| opencv-python-headless | >= 4.8.0 | 抠图等工具脚本 |
| numpy | >= 1.24.0, \< 2.5 | 数值计算（配合 OpenCV） |
| pyinstaller | >= 6.0.0 | 打包为 EXE（仅开发/打包需要） |

## 开发运行

```bat
pip install -r requirements.txt
python src\desk_pet.py
```

## 打包 EXE

```bat
build.bat
```

生成文件：`dist\DeskPet.exe`
