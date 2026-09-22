# MCAP 视频查看器 · macOS 版使用说明

适用机型：Intel i7 Mac（也兼容 Apple Silicon）。系统要求见下方「两种安装路径」。

## 一、两种安装路径（按 macOS 版本自动选择）

| macOS 版本 | 脚本自动选择的依赖清单 | Python 要求 |
|---|---|---|
| macOS 12 (Monterey) 及以上 | `requirements-macos.txt`（PySide6 6.11 / Qt 6.11） | 3.12 或 3.13（推荐 3.12） |
| macOS 10.15 (Catalina) ~ 11 (Big Sur) | `requirements-macos-legacy.txt`（PySide6 6.4 / Qt 6.4） | 3.11 |

Python 从 <https://www.python.org/downloads/macos/> 安装（选 64-bit universal2 安装包），
或 `brew install python@3.12`。**不要用系统自带的 /usr/bin/python3**（版本过旧）。

## 二、安装与启动（三步）

```bash
# 1) 解压后进入项目目录
cd ~/Downloads/mcap-viewer

# 2) 首次运行：自动建立独立运行时并安装依赖（约 150 MB，几分钟）
chmod +x bootstrap.sh 启动查看器.command build_mac.sh
./bootstrap.sh            # 也可以直接双击「启动查看器.command」

# 3) 之后每次启动
open 启动查看器.command    # 或双击它
```

首次运行会在项目目录下创建 `runtime/`（项目目录不可写时改用
`~/Library/Application Support/MCAPViewer/runtime/`）。
运行时一旦建好，后续启动只需 1~2 秒。

## 三、打包成 .app（推荐，双击即用、无终端窗口）

**必须在 Mac 上执行**（PyInstaller 不能跨平台打包）：

```bash
./build_mac.sh
```

产物：
- `release/MCAP视频查看器.app` —— 双击运行
- `../MCAP视频查看器-macOS-<日期>.zip` —— 分发给同事

首次打开未签名应用若被 Gatekeeper 拦（提示"无法验证开发者"）：
右键点 app → 打开 → 仍要打开；或执行
`xattr -dr com.apple.quarantine "MCAP视频查看器.app"`。

## 四、macOS 版与 Windows 版的差异（重要）

| 项目 | Windows | macOS |
|---|---|---|
| 音频播放 | MCI (winmm) | QtMultimedia（AVFoundation）；初始化失败自动静音 |
| 变速播放 | MCI 不支持则自动静音 | 由 QMediaPlayer.setPlaybackRate 探测 |
| 缓存目录 | 程序目录 `cache\`，回退 `%LOCALAPPDATA%\MCAPViewer\cache` | 程序目录 `cache/`，回退 `~/Library/Application Support/MCAPViewer/cache` |
| 观看状态 | `%LOCALAPPDATA%\MCAPViewer\state\` | `~/Library/Application Support/MCAPViewer/state/` |
| 跨进程缓存锁 | msvcrt 文件锁 | fcntl.flock 文件锁 |
| 启动失败弹窗 | MessageBoxW | 系统对话框（osascript） |

其余功能（6 路鱼眼解码、去畸变、参考框、三队列缓存、看完自动删缓存、
播放列表/自动连播）在两端完全一致。

## 五、性能预期（Intel i7）

- 桌面端只解码 **camera2 / camera3** 两路，默认 800px 档；
  i7 Mac 跑 1600×1300@30fps 的 H.264 双路软解没问题。
- 如果卡顿：切「解码」档到 560px（流畅档）；高倍速（>2×）会自动降低刷新占用。
- 首次打开一个大文件需要封装缓存（真实 2.7GB / 70s 六路约 2~4 分钟，
  进度条有显示）；下次打开同一文件秒开。

## 六、缓存与清理

- 缓存上限 3 个（三队列管理：未看 / 已缓存 / 已看完），看完自动删缓存。
- 想手动换缓存盘：
  `echo 'export MCAPVIEWER_CACHE=$HOME/Movies/MCAPCache' >> ~/.zshrc`
- 完全卸载：删掉项目目录（或 .app）+
  `~/Library/Application Support/MCAPViewer/`。

## 七、首次验收清单（Mac 上照着点一遍）

1. 双击 app（或 `./bootstrap.sh`）→ 窗口出现，无报错。
2. 「打开文件夹…」选一个装有 .mcap 的目录 → 左侧三个页签（未看/已缓存/已看完）出现。
3. 前 3 个文件自动进入「已缓存」（右下角「已缓存 3/3」）。
4. 双击一个已缓存文件 → 画面出现、可播放、可拖动进度条。
5. **标注不合格**：播放中按一下 X（进度条下方出现黄色起点线），再按一下 X
   → 变红一段；底部显示「不合格 1 段 · x.x 秒」。按 Ctrl+Z 撤销；
   鼠标操作等价按钮是「✗ 标不合格」。
6. 点「✓ 标记已看完」→ 画面立即清空并显示「已看完 / 缓存已释放」，
   左侧该文件出现在「已看完」页，缓存目录里对应文件夹消失。
7. 在「已看完」页点「重新缓存」→ 徽标依次变成 等待 → 缓存中 x% → 已缓存。
8. **全部看完**：该文件夹所有视频看完后 → 自动生成
   `定位合格率报告_设备<设备号>_<日期>.txt`（就在视频文件夹里），
   并弹窗询问「是否清空已看完队列与所有缓存，并选择下一个文件夹？」
   - 选「是」→ 清空队列与缓存 + 打开文件夹选择
   - 选「否」→ 不动；之后可点左侧「清空队列与缓存」按钮（有二次确认）
9. 关掉再打开软件 → 三个队列状态与**已标注的不合格片段**都保持
   （已看完的不会自动重新缓存）。

任何一步失败：把终端里的输出 + `startup-error.log` 发回来。

## 七·附：不合格标注与合格率报告说明

- 文件名约定解析：`DAS-Ego_20260911203440_none_none_689985_65416a8a`
  → 日期 2026-09-11、开机时间 20:34:40、设备号 689985（不符合此约定的名字不会瞎猜）。
- 标注口径：不合格时长 = 所有标注片段的**并集**（重叠只算一次，且截断在视频时长内）；
  合格时长 = 总时长 − 不合格时长；合格率 = 合格 ÷ 总时长。
- 只按一下 X 就点了「标记已看完」时，程序会**自动把起点闭合到当前位置**并提示，
  避免漏标。
- 报告写不进去（只读文件夹等）时会自动落到
  `~/Library/Application Support/MCAPViewer/`（Windows 为 `%LOCALAPPDATA%\MCAPViewer\`）。

## 八、故障速查

| 现象 | 处理 |
|---|---|
| `No supported 64-bit Python 3.10-3.13 found` | 装 python.org 的 3.12，或 `brew install python@3.12`，再跑 bootstrap.sh |
| pip 安装某版本报 "No matching distribution" | 把输出发回来，调整 `requirements-macos*.txt` 里的 pin |
| 启动后窗口一闪而过 | 双击「启动查看器.command」看终端输出，查 `startup-error.log` |
| 没有声音 | 正常降级路径（音频初始化失败自动静音）；确认文件本身有音轨 |
| app 打不开（Gatekeeper） | 见第三节的 `xattr -dr` 命令 |
