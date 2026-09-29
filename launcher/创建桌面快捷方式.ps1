# ========================================================
# 创建桌面快捷方式：青梓小窝
# 目标：D:\qq-companion\venv\Scripts\pythonw.exe（无黑窗启动）
# 图标：launcher/assets/app.ico（标准 Windows 多分辨率 ICO）
# ========================================================

$ErrorActionPreference = "Stop"

$ProjectDir = "D:\qq-companion"
$PythonExe  = "$ProjectDir\venv\Scripts\python.exe"
$PythonwExe = "$ProjectDir\venv\Scripts\pythonw.exe"
$ScriptPath = "$ProjectDir\launcher\qingzi_home.py"
$IcoTarget  = "$ProjectDir\launcher\assets\qingzi_anime.ico"

# 1. 确保 assets 目录存在
New-Item -ItemType Directory -Force -Path "$ProjectDir\launcher\assets" | Out-Null

# 2. 检查青梓主图标
if (-not (Test-Path $IcoTarget)) {
    $fallback = "$ProjectDir\launcher\assets\app.ico"
    if (Test-Path $fallback) {
        Copy-Item $fallback $IcoTarget -Force
    }
}

# 3. 调用 WScript.Shell 生成桌面快捷方式
$WshShell = New-Object -ComObject WScript.Shell
$DesktopPath = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Desktop)
$ShortcutPath = Join-Path $DesktopPath "青梓小窝.lnk"

# 若已存在先删除旧快捷方式，避免缓存残留
if (Test-Path $ShortcutPath) {
    Remove-Item $ShortcutPath -Force
}

$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PythonwExe
$Shortcut.Arguments = "`"$ScriptPath`""
$Shortcut.WorkingDirectory = $ProjectDir
$Shortcut.Description = "QQ伴侣机器人 桌面监控与控制台"

if (Test-Path $IcoTarget) {
    $Shortcut.IconLocation = "$IcoTarget,0"
}

$Shortcut.Save()

# 4. 强制刷新 Windows 桌面图标缓存
try {
    $code = @'
[System.Runtime.InteropServices.DllImport("Shell32.dll")]
public static extern void SHChangeNotify(int eventId, int flags, IntPtr item1, IntPtr item2);
'@
    $type = Add-Type -MemberDefinition $code -Name ShellIconHelper -Namespace Win32 -PassThru
    $type::SHChangeNotify(0x08000000, 0x0000, [IntPtr]::Zero, [IntPtr]::Zero)
} catch {}

try {
    & ie4uinit.exe -ClearIconCache
    & ie4uinit.exe -show
} catch {}

Write-Host "====================================================" -ForegroundColor Green
Write-Host " 桌面快捷方式已成功创建并刷新图标缓存！" -ForegroundColor Green
Write-Host " 快捷方式路径: $ShortcutPath" -ForegroundColor Cyan
Write-Host " 绑定图标文件: $IcoTarget" -ForegroundColor Cyan
Write-Host " 启动程序: $PythonwExe (后台静默无黑窗)" -ForegroundColor Cyan
Write-Host "====================================================" -ForegroundColor Green
