# 永雏塔菲 — Windows 桌面宠物

透明置顶的桌面宠物：大笑立绘、高频抖动，启动后循环播放语音。

**双击 `start.bat` 即可运行**（首次会自动准备环境）。也可以双击已打包的 `dist\DeskPet.exe`，或：

```bat
python src\desk_pet.py
```

![character](assets/character_cutout.png)

## 使用方式

| 操作 | 效果 |
|------|------|
| 启动 | 循环播放语音，立绘按参考 GIF 的节奏上下抖动 |
| 左键拖动 | 移动位置 |
| 左键单击 | 若已关闭音频则重新播放 |
| 滚轮 | 缩放大小 |
| 右键菜单 | 调整大小 / 置顶开关 / 关闭音频 / 退出 |

窗口无边框、背景透明、默认始终置顶。

## 一键启动

双击 `start.bat`。脚本会：

1. 检测本机 Python 3.9+（没有则尝试用 winget 安装）
2. 在项目目录创建 `.venv` 虚拟环境
3. 安装运行依赖（PySide6 Essentials）
4. 启动桌面宠物

首次需要联网，可能要等几分钟；之后再双击会很快。

## 环境依赖

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10 / 11 |
| Python | 3.9 及以上（可由启动脚本自动安装） |

Python 包（见 `requirements.txt`）：

| 包名 | 版本 | 用途 |
|------|------|------|
| PySide6 | >= 6.6.0 | Qt GUI 与音频（启动脚本会安装） |
| pyinstaller | >= 6.0.0 | 打包为 EXE（仅开发/打包需要） |

运行资源：

| 文件 | 用途 |
|------|------|
| `assets/character_cutout.png` | 透明立绘 |
| `assets/9月4日.mp3` | 循环语音 |

## 打包 EXE

```bat
build.bat
```

生成文件：`dist\DeskPet.exe`
